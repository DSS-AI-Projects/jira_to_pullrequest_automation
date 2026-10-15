"""Security invariant 6, canary half: untrusted repo code never runs with
server secrets, and its output never carries them.

No mocks around subprocess: the real `validate_workspace` runs a workspace
built to attack it — a test that dumps its environment, prints secret values
it got hold of some other way, plants a git hook where a postinstall script or
husky would, and a `ruff.py` that tries to shadow ruff. Then every field of
every result is checked for the secrets.

Phase 1 limits (docs/validation-isolation-proposal.md §3.5): the child still
runs as the backend's uid, so a *deliberate* absolute-path read of the DB or
another workspace isn't blocked here — that needs the phase 2 sandbox.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import secrets
from app.core.config import Settings
from app.jobs.models import ValidationResult, ValidationStatus
from app.steps import validation_runner
from app.steps.validation_runner import validate_workspace

# One canary per secret core/secrets.py knows, token-shaped where a real one is.
_KNOWN_CANARIES = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-canaryCanaryCanary0123456789",
    "JIRA_API_TOKEN": "ATATT3xCanaryJiraToken0123456789",
    "JIRA_OAUTH_CLIENT_SECRET": "canary-jira-oauth-client-secret-0123",
    "JIRA_OAUTH_ENCRYPTION_KEY": "canary-jira-fernet-key-0123456789abcdef=",
    "GITHUB_OAUTH_CLIENT_SECRET": "canary-github-oauth-client-secret-0123",
    "GITHUB_OAUTH_ENCRYPTION_KEY": "canary-github-fernet-key-0123456789abcd=",
    "GITLAB_OAUTH_CLIENT_SECRET": "canary-gitlab-oauth-client-secret-0123",
    "GITLAB_OAUTH_ENCRYPTION_KEY": "canary-gitlab-fernet-key-0123456789abcd=",
    "BITBUCKET_OAUTH_CLIENT_SECRET": "canary-bitbucket-oauth-secret-0123456",
    "BITBUCKET_OAUTH_ENCRYPTION_KEY": "canary-bitbucket-fernet-key-0123456789=",
}
# A secret the app doesn't know about: it can't be redacted, so it must never
# reach the child at all.
_UNKNOWN_CANARY = ("ACME_DEPLOY_TOKEN", "acme-unknown-canary-0123456789")

_CANARY_TEST = r"""
import json
import os
import pathlib
import subprocess

def test_canary():
    here = pathlib.Path.cwd()
    (here / "seen-env-keys.json").write_text(json.dumps(sorted(os.environ)))
    # What a husky `prepare` or a hostile postinstall would do from the cwd:
    # point git at its own hooks (git searches upward for a repository)...
    subprocess.run(["git", "config", "core.hooksPath", ".evil-hooks"], cwd=here)
    # ...and write a hook into ./.git.
    hooks = here / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho pwned\n")
    print("ENV DUMP:", dict(os.environ))
    # Secret values obtained some other way (here: committed to the repo).
    print("LEAKED:", json.loads((here / "leaked.json").read_text()))
    assert False, "fail so the output is kept and quoted to the correction agent"
"""

_SHADOW_RUFF = """
import pathlib
pathlib.Path("shadow-ruff-ran").write_text("ran as code")
"""


@pytest.fixture
def canary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _KNOWN_CANARIES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(*_UNKNOWN_CANARY)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(validation_runner, "get_settings", lambda: settings)


def _all_text(results: list[ValidationResult]) -> str:
    return "\n".join(result.model_dump_json() for result in results)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=None
    )
    return completed.stdout


def _app_checkout(tmp_path: Path) -> Path:
    """A stand-in for this app's own checkout, which in local dev encloses
    the workdir (backend/var/workdir/<job>/repo)."""
    app = tmp_path / "app"
    app.mkdir()
    _git(app, "init", "-q")
    return app


def _committed_workspace(app: Path) -> Path:
    workspace = app / "var" / "workdir" / "job-1" / "repo"
    workspace.mkdir(parents=True)
    _git(workspace, "init", "-q")
    return workspace


def _commit_all(workspace: Path) -> None:
    _git(workspace, "add", "-A")
    _git(
        workspace,
        "-c",
        "user.name=canary",
        "-c",
        "user.email=canary@example.invalid",
        "commit",
        "-q",
        "-m",
        "canary",
    )


@pytest.mark.usefixtures("canary_env")
async def test_python_canary_sees_no_secrets_and_leaks_none(tmp_path: Path) -> None:
    app = _app_checkout(tmp_path)
    app_config_before = (app / ".git" / "config").read_text(encoding="utf-8")
    workspace = _committed_workspace(app)
    (workspace / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-p no:cacheprovider'\n", encoding="utf-8"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_canary.py").write_text(_CANARY_TEST, encoding="utf-8")
    (workspace / "leaked.json").write_text(json.dumps(_KNOWN_CANARIES), encoding="utf-8")
    _commit_all(workspace)
    config_before = (workspace / ".git" / "config").read_text(encoding="utf-8")

    results = await validate_workspace(workspace)

    [pytest_result] = [result for result in results if result.name == "pytest"]
    assert pytest_result.status == ValidationStatus.FAILED
    text = _all_text(results)
    # The canary ran and its output was captured...
    assert "ENV DUMP" in text
    # ...but no secret, known or unknown, appears anywhere in the results.
    for value in [*_KNOWN_CANARIES.values(), _UNKNOWN_CANARY[1]]:
        assert value not in text

    run_dir = workspace.parent / validation_runner.VALIDATION_DIR_NAME
    seen_keys = set(json.loads((run_dir / "seen-env-keys.json").read_text(encoding="utf-8")))
    assert not seen_keys & {*_KNOWN_CANARIES, _UNKNOWN_CANARY[0]}
    # Exactly the allowlisted environment, plus what the child's own pytest set.
    # Windows variable names are case-insensitive, and Python there reports them
    # upper-cased (the runner's npm_config_* arrive as NPM_CONFIG_*).
    expected = set(validation_runner.validation_env(tmp_path / "home", git_ceiling=tmp_path))
    if os.name == "nt":
        seen_keys = {key.upper() for key in seen_keys}
        expected = {key.upper() for key in expected}
    assert seen_keys - {"PYTEST_CURRENT_TEST", "PYTEST_VERSION"} == expected

    # The hook and the hooksPath landed in the copy, not in the workspace that
    # create-branch and push later run git in.
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == config_before
    assert _git(workspace, "status", "--porcelain") == ""
    # git run from the .git-less copy didn't climb into the enclosing checkout.
    assert (app / ".git" / "config").read_text(encoding="utf-8") == app_config_before


@pytest.mark.usefixtures("canary_env")
async def test_repo_ruff_py_cannot_shadow_ruff(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ruff.toml").write_text("", encoding="utf-8")
    (workspace / "ruff.py").write_text(_SHADOW_RUFF, encoding="utf-8")

    results = await validate_workspace(workspace)

    assert [result.name for result in results] == ["ruff"]
    run_dir = workspace.parent / validation_runner.VALIDATION_DIR_NAME
    assert not (run_dir / "shadow-ruff-ran").exists()


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
@pytest.mark.usefixtures("canary_env")
async def test_node_canary_lifecycle_script_sees_no_secrets(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    dump_env = (
        "node -e \"require('fs').writeFileSync('seen-env-keys.json', "
        "JSON.stringify(Object.keys(process.env))); console.log('ENV DUMP', process.env)\""
    )
    leak = (
        "node -e \"console.log('LEAKED', require('fs').readFileSync('leaked.json', 'utf8')); "
        'process.exit(1)"'
    )
    package = {
        "name": "canary",
        "version": "1.0.0",
        "private": True,
        "scripts": {"postinstall": dump_env, "test": leak},
    }
    (workspace / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (workspace / "leaked.json").write_text(json.dumps(_KNOWN_CANARIES), encoding="utf-8")

    results = await validate_workspace(workspace)

    assert [result.name for result in results] == ["npm install", "npm test"]
    assert results[1].status == ValidationStatus.FAILED
    text = _all_text(results)
    assert "LEAKED" in text
    for value in [*_KNOWN_CANARIES.values(), _UNKNOWN_CANARY[1]]:
        assert value not in text
    run_dir = workspace.parent / validation_runner.VALIDATION_DIR_NAME
    seen_keys = {
        key.upper()
        for key in json.loads((run_dir / "seen-env-keys.json").read_text(encoding="utf-8"))
    }
    assert not seen_keys & {*_KNOWN_CANARIES, _UNKNOWN_CANARY[0]}


def test_register_configured_secrets_covers_every_secret_reader() -> None:
    readers = {
        name
        for name, function in inspect.getmembers(secrets, inspect.isfunction)
        if name.startswith("get_") and function.__module__ == secrets.__name__
    }
    registered_by_startup = {
        reader.__name__
        for reader in secrets._SECRET_READERS  # pyright: ignore[reportPrivateUsage]
    }
    assert readers == registered_by_startup


def test_register_configured_secrets_registers_values(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "startup-registered-canary-0123456789"
    monkeypatch.setenv("GITLAB_OAUTH_ENCRYPTION_KEY", value)

    secrets.register_configured_secrets()

    assert value in secrets.registered_secrets()


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_process_can_be_made_non_dumpable() -> None:
    # Run in a child so the test process itself stays debuggable.
    code = (
        "from app.core.process_hardening import make_process_non_dumpable; "
        "import sys; sys.exit(0 if make_process_non_dumpable() else 1)"
    )
    backend_root = Path(__file__).resolve().parent.parent
    completed = subprocess.run([sys.executable, "-c", code], cwd=backend_root, env=None)
    assert completed.returncode == 0


@pytest.mark.skipif(sys.platform == "linux", reason="covered by the Linux test above")
def test_non_dumpable_is_a_no_op_off_linux() -> None:
    from app.core.process_hardening import make_process_non_dumpable

    assert make_process_non_dumpable() is False
