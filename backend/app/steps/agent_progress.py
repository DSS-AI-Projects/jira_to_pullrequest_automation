"""Cross-cutting progress reporting from the agent execution loop back to
the job record — without threading a callback through every `JobSteps`
signature (`generate_plan`/`implement_plan` are called polymorphically by
runner.py against either the real agent steps or a test fake, so widening
those signatures would mean updating every fake across the test suite for a
concern only the real steps care about).

A `ContextVar` instead: the real agent execution (`execute_agent()` in
plan_agent.py / implement_agent.py) runs inside a background thread via
`asyncio.to_thread` — Python propagates the *current* context (a copy) into
that thread, so a sink set once per job run in runner.py is visible deep
inside the SDK message loop with no parameter threading, and each
concurrent job's asyncio task has its own isolated copy (contextvars are
per-task, not global mutable state) — see the propagation check in this
module's own test file for the empirical basis.

Deliberately reports only tool calls (Read/Grep/Glob/Edit/Write — the fixed
set both agents are restricted to; see CLAUDE.md's harness section), not the
model's own free-text narration: reliable and low-noise, not a curated but
occasionally-off-topic transcript.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

ProgressSink = Callable[[str], None]

_sink: ContextVar[ProgressSink | None] = ContextVar("agent_progress_sink", default=None)


@contextmanager
def report_progress_to(callback: ProgressSink | None) -> Generator[None]:
    """Install `callback` as the active progress sink for the duration of
    the `with` block (and anything it calls, including across
    `asyncio.to_thread`). Always restores the previous sink afterward, so
    nesting (e.g. a correction pass reusing the same agent step) is safe."""
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


def report(line: str) -> None:
    """Best-effort: report one progress line to the active sink, if any.
    A no-op outside of `report_progress_to` (e.g. under a test fake that
    never installs one) — callers never need to check first."""
    sink = _sink.get()
    if sink is not None:
        sink(line)


_VERB_BY_TOOL = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Writing",
}


def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> str | None:
    """A short, human-readable line for one tool call, or None for a tool
    (or a call missing the field it needs) not worth surfacing."""
    verb = _VERB_BY_TOOL.get(name)
    if verb is not None:
        path = tool_input.get("file_path")
        return f"{verb} {path}" if path else None
    if name == "Grep":
        pattern = tool_input.get("pattern")
        if not pattern:
            return None
        path = tool_input.get("path")
        return f'Searching for "{pattern}" in {path}' if path else f'Searching for "{pattern}"'
    if name == "Glob":
        pattern = tool_input.get("pattern")
        return f'Listing files matching "{pattern}"' if pattern else None
    return None
