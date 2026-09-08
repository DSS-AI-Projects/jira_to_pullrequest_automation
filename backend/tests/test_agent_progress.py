"""agent_progress: the ContextVar-based progress sink and tool-call
summarizer, plus an empirical check of the exact propagation path
execute_agent() relies on (a sink set in the calling task must survive two
nested layers of asyncio.to_thread, matching
execute_agent_with_subprocess_support -> _run_execute_agent_sync ->
asyncio.run(execute_agent(...)))."""

from __future__ import annotations

import asyncio

from app.steps.agent_progress import report, report_progress_to, summarize_tool_use


def test_summarize_tool_use_covers_read_edit_write() -> None:
    assert summarize_tool_use("Read", {"file_path": "a.py"}) == "Reading a.py"
    assert summarize_tool_use("Edit", {"file_path": "a.py"}) == "Editing a.py"
    assert summarize_tool_use("Write", {"file_path": "a.py"}) == "Writing a.py"


def test_summarize_tool_use_covers_grep_with_and_without_path() -> None:
    assert (
        summarize_tool_use("Grep", {"pattern": "foo", "path": "src"})
        == 'Searching for "foo" in src'
    )
    assert summarize_tool_use("Grep", {"pattern": "foo"}) == 'Searching for "foo"'


def test_summarize_tool_use_covers_glob() -> None:
    assert summarize_tool_use("Glob", {"pattern": "**/*.py"}) == 'Listing files matching "**/*.py"'


def test_summarize_tool_use_returns_none_for_unknown_tool_or_missing_field() -> None:
    assert summarize_tool_use("Bash", {"command": "ls"}) is None
    assert summarize_tool_use("Read", {}) is None
    assert summarize_tool_use("Grep", {}) is None
    assert summarize_tool_use("Glob", {}) is None


def test_report_progress_to_installs_and_restores_the_sink() -> None:
    received: list[str] = []
    with report_progress_to(received.append):
        report("inside")
    report("outside")  # no sink installed anymore — silently dropped

    assert received == ["inside"]


def test_report_is_a_silent_no_op_without_an_active_sink() -> None:
    report("nobody is listening")  # must not raise


def test_report_progress_to_restores_the_previous_sink_on_nesting() -> None:
    outer_received: list[str] = []
    inner_received: list[str] = []
    with report_progress_to(outer_received.append):
        report("before nested")
        with report_progress_to(inner_received.append):
            report("during nested")
        report("after nested")

    assert outer_received == ["before nested", "after nested"]
    assert inner_received == ["during nested"]


async def test_sink_propagates_through_nested_asyncio_to_thread() -> None:
    """Empirical basis for agent_progress.py's whole design: execute_agent()
    runs inside a background thread, itself running its own nested event
    loop (see _run_execute_agent_sync in plan_agent.py/implement_agent.py) —
    exactly this shape."""

    def read_in_thread() -> None:
        report("from worker thread")

    async def nested() -> None:
        await asyncio.to_thread(read_in_thread)

    def run_nested_sync() -> None:
        asyncio.run(nested())

    async def outer_to_thread() -> None:
        await asyncio.to_thread(run_nested_sync)

    received: list[str] = []
    with report_progress_to(received.append):
        await outer_to_thread()

    assert received == ["from worker thread"]
