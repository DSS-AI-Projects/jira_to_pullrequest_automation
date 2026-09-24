"""Security invariant 3: the planning/implementation agents are mechanically
confined to their job workspace, not just asked to stay in it.

Regression for a real incident: with the workspace nested inside this app's
own checkout (`<app>/backend/var/workdir/<job>/repo`), an implementation
agent wrote `<app>/README.md`, `<app>/pom.xml`, and `<app>/src/...` by
absolute path, and nothing stopped it. The layout below mirrors that.
"""

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import HookContext, HookInput

from app.core.config import get_settings
from app.steps import implement_agent, plan_agent
from app.steps.agent_progress import report_progress_to
from app.steps.workspace_guard import find_outside_path, workspace_guard_hooks

FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-" + "e" * 32


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    app_root = tmp_path / "jira2pullreq"
    workspace = app_root / "backend" / "var" / "workdir" / "job1" / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "README.md").write_text("workspace readme", encoding="utf-8")
    (app_root / "README.md").write_text("app readme", encoding="utf-8")
    (app_root / "backend" / ".env").write_text("SECRET=1", encoding="utf-8")
    return app_root, workspace


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "README.md"}),
        ("Read", {"file_path": "src/main.py"}),
        ("Write", {"file_path": "src/new_module.py", "content": "x"}),
        ("Edit", {"file_path": "./README.md", "old_string": "a", "new_string": "b"}),
        ("Read", {"file_path": "src/../README.md"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Glob", {"pattern": "*.md", "path": "src"}),
        ("Grep", {"pattern": "TODO", "path": "src", "glob": "*.py"}),
        ("Grep", {"pattern": "TODO"}),
    ],
)
def test_paths_inside_the_workspace_are_allowed(
    layout: tuple[Path, Path], tool: str, tool_input: dict[str, Any]
) -> None:
    _app_root, workspace = layout
    assert find_outside_path(tool, tool_input, workspace.resolve()) is None


def test_absolute_paths_inside_the_workspace_are_allowed(layout: tuple[Path, Path]) -> None:
    _app_root, workspace = layout
    inside = str(workspace / "README.md")
    assert find_outside_path("Read", {"file_path": inside}, workspace.resolve()) is None


def test_the_incident_paths_are_denied(layout: tuple[Path, Path]) -> None:
    """The exact shape of the real incident: absolute paths into the app
    checkout that contains the workspace."""
    app_root, workspace = layout
    root = workspace.resolve()
    for tool, key in [("Read", "file_path"), ("Write", "file_path"), ("Edit", "file_path")]:
        for target in ("README.md", "pom.xml", "src/main/java/HelloAIWorld.java", ".gitignore"):
            raw = str(app_root / target)
            assert find_outside_path(tool, {key: raw}, root) == raw


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "../../../../../backend/.env"}),
        ("Read", {"file_path": "src/../../README.md"}),
        ("Write", {"file_path": "../escape.txt", "content": "x"}),
        ("Glob", {"pattern": "*", "path": ".."}),
        ("Glob", {"pattern": "../../**/*"}),
        ("Grep", {"pattern": "SECRET", "path": "../../../../.."}),
        ("Grep", {"pattern": "SECRET", "glob": "../../**/.env"}),
    ],
)
def test_relative_escapes_are_denied(
    layout: tuple[Path, Path], tool: str, tool_input: dict[str, Any]
) -> None:
    _app_root, workspace = layout
    assert find_outside_path(tool, tool_input, workspace.resolve()) is not None


def test_absolute_glob_patterns_outside_are_denied(layout: tuple[Path, Path]) -> None:
    app_root, workspace = layout
    pattern = str(app_root / "**" / "*.env")
    assert find_outside_path("Glob", {"pattern": pattern}, workspace.resolve()) == pattern


def test_a_sibling_directory_sharing_a_name_prefix_is_denied(layout: tuple[Path, Path]) -> None:
    """`.../repo-other` must not pass a naive string-prefix check for `.../repo`."""
    _app_root, workspace = layout
    sibling = workspace.parent / (workspace.name + "-other") / "file.txt"
    assert find_outside_path("Read", {"file_path": str(sibling)}, workspace.resolve()) is not None


def test_home_directory_paths_are_denied(layout: tuple[Path, Path]) -> None:
    _app_root, workspace = layout
    assert find_outside_path("Read", {"file_path": "~/.ssh/id_rsa"}, workspace.resolve())


def test_a_symlink_pointing_out_of_the_workspace_is_denied(layout: tuple[Path, Path]) -> None:
    app_root, workspace = layout
    link = workspace / "escape"
    try:
        link.symlink_to(app_root, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this machine")
    assert find_outside_path("Read", {"file_path": "escape/README.md"}, workspace.resolve())


def test_tools_without_path_arguments_are_untouched(layout: tuple[Path, Path]) -> None:
    """The harness's own structured-output tool must never be blocked."""
    _app_root, workspace = layout
    assert find_outside_path("StructuredOutput", {"summary": "x"}, workspace.resolve()) is None


def _run_hook(workspace: Path, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    hooks = workspace_guard_hooks(workspace)
    guard = hooks["PreToolUse"][0].hooks[0]
    payload = cast(
        HookInput,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "t1",
            "session_id": "s1",
            "transcript_path": "",
            "cwd": str(workspace),
        },
    )
    return cast(
        dict[str, Any], asyncio.run(guard(payload, "t1", cast(HookContext, {"signal": None})))
    )


def test_hook_denies_with_a_reason_the_agent_can_act_on(layout: tuple[Path, Path]) -> None:
    app_root, workspace = layout
    reported: list[str] = []
    target = str(app_root / "README.md")

    with report_progress_to(reported.append):
        result = _run_hook(workspace, "Write", {"file_path": target, "content": "x"})

    output = result["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "outside the job workspace" in output["permissionDecisionReason"]
    # Surfaced in the job's activity log, so a blocked attempt is visible.
    assert reported == [f"Blocked Write outside the workspace: {target}"]
    # And the file really was not touched (the hook ran before any write).
    assert (app_root / "README.md").read_text(encoding="utf-8") == "app readme"


def test_hook_allows_workspace_paths(layout: tuple[Path, Path]) -> None:
    _app_root, workspace = layout
    assert _run_hook(workspace, "Read", {"file_path": "README.md"}) == {}


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)
    get_settings.cache_clear()


def test_both_agents_install_the_guard_and_load_no_setting_sources(
    agent_env: None, layout: tuple[Path, Path]
) -> None:
    """`setting_sources=[]` matters on its own: unset, the CLI loads every
    CLAUDE.md found walking up from the workspace — i.e. this app's own —
    plus the host user's ~/.claude settings."""
    _app_root, workspace = layout
    for options in (
        plan_agent.build_options(workspace, FAKE_ANTHROPIC_KEY, get_settings()),
        implement_agent.build_options(workspace, FAKE_ANTHROPIC_KEY, get_settings()),
        implement_agent.build_options(
            workspace, FAKE_ANTHROPIC_KEY, get_settings(), is_correction=True
        ),
    ):
        assert options.setting_sources == []
        # Nor the host user's Claude Code auto-memory, which setting_sources
        # alone does not cover (verified against the live CLI).
        assert isinstance(options.env, dict)
        assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert options.hooks is not None
        matchers = options.hooks["PreToolUse"]
        assert len(matchers) == 1
        assert matchers[0].matcher is None  # every tool, not a named subset
