"""Mechanical confinement of the planning/implementation agents to their
job workspace (security invariant 3).

The agents' tools (Read/Grep/Glob, plus Edit/Write for implementation) accept
absolute paths, and `allowed_tools` pre-approves them for *any* path — `cwd`
only sets where relative paths start, it is not a boundary. Before this
guard, "stay inside the workspace" was prompt wording only, and a real job
proved that isn't enough: an implementation agent read and wrote this app's
own repo root (`D:\\work\\...\\jira2pullreq\\README.md`, `pom.xml`, `src\\...`)
instead of `var/workdir/<job>/repo`, and a planning agent could just as well
have read `backend/.env`.

`workspace_guard_hooks()` returns a PreToolUse hook that runs before every
tool call — hooks run ahead of the permission system, so they apply even to
pre-approved tools — and denies any call whose path arguments resolve
(after `..`, `~`, and symlink resolution) outside the workspace. The denial
reason goes back to the agent, so it can retry with a workspace path instead
of failing the job outright.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher
from claude_agent_sdk.types import HookEvent

from app.core.logging import get_logger, redact
from app.steps.agent_progress import report

logger = get_logger(__name__)

# Tool-input keys that name a file or directory to act on. Checked for every
# tool, not just the known file tools, so a tool added to an agent later is
# covered by default rather than silently exempt.
_PATH_KEYS = ("file_path", "notebook_path", "path")
# Glob/Grep filters: relative to the search directory, but an absolute
# pattern or one climbing out via ".." reaches elsewhere.
_PATTERN_KEYS_BY_TOOL = {"Glob": ("pattern",), "Grep": ("glob",)}
_GLOB_CHARS_RE = re.compile(r"[*?\[{]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(candidate: Path, root: Path) -> bool:
    candidate_text, root_text = _normalized(candidate), _normalized(root)
    try:
        return os.path.commonpath([candidate_text, root_text]) == root_text
    except ValueError:  # different drives on Windows
        return False


def resolve_tool_path(raw: str, base: Path) -> Path:
    """Resolve a tool path argument the way the harness would: `~` expanded,
    relative paths taken from `base`, `..` and symlinks resolved. A
    drive-less rooted path (`\\foo` on Windows) resolves against `base`'s
    drive, i.e. outside any workspace."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _pattern_base(pattern: str) -> str | None:
    """The literal directory part of a glob pattern that could leave the
    search directory, or None when the pattern stays relative and never
    climbs (`**/*.py`, `src/*.ts`) — the common, always-safe case."""
    literal = pattern[: m.start()] if (m := _GLOB_CHARS_RE.search(pattern)) else pattern
    anchored = literal.startswith(("/", "\\", "~")) or bool(_DRIVE_RE.match(literal))
    climbs = ".." in re.split(r"[\\/]", pattern)
    if not anchored and not climbs:
        return None
    if climbs and not anchored:
        # A ".." after the first wildcard can't be resolved literally —
        # treat the whole pattern's non-wildcard segments as the base.
        return "/".join(
            part for part in re.split(r"[\\/]", pattern) if not _GLOB_CHARS_RE.search(part)
        )
    return literal if literal.endswith(("/", "\\")) else str(Path(literal).parent)


def find_outside_path(tool_name: str, tool_input: dict[str, Any], root: Path) -> str | None:
    """The first path argument of this tool call that resolves outside
    `root`, or None if every path it names is inside."""
    search_dir = root
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        resolved = resolve_tool_path(value, root)
        if not is_within(resolved, root):
            return value
        if key == "path":
            search_dir = resolved
    for key in _PATTERN_KEYS_BY_TOOL.get(tool_name, ()):
        value = tool_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        base = _pattern_base(value)
        if base is not None and not is_within(resolve_tool_path(base or ".", search_dir), root):
            return value
    return None


def workspace_guard_hooks(workspace: Path) -> dict[HookEvent, list[HookMatcher]]:
    root = workspace.resolve()

    async def guard(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        del tool_use_id, context
        data = cast(dict[str, Any], input_data)
        if data.get("hook_event_name") != "PreToolUse":
            return {}
        tool_name = str(data.get("tool_name") or "")
        tool_input = cast(dict[str, Any], data.get("tool_input") or {})
        outside = find_outside_path(tool_name, tool_input, root)
        if outside is None:
            return {}
        logger.warning("Blocked %s outside the job workspace: %s", tool_name, redact(outside))
        report(f"Blocked {tool_name} outside the workspace: {outside}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Access denied: {outside} is outside the job workspace. "
                    "Only files inside the current working directory may be "
                    "read or changed — use a path relative to it."
                ),
            }
        }

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}
