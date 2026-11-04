"""The CLI's reasons for rejecting an agent's structured result are kept for the log."""

from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from app.steps.structured_output_rejections import StructuredOutputRejections

MISSING_FIELD = (
    "Output does not match required schema: root: must have required property 'changed_files'"
)


def attempt(tool_use_id: str, name: str = "StructuredOutput") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_use_id, name=name, input={"summary": "Done."})],
        model="claude-test",
    )


def reply(tool_use_id: str, text: str, is_error: bool = True) -> UserMessage:
    return UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=[{"type": "text", "text": text}],
                is_error=is_error,
            )
        ]
    )


def test_keeps_the_reason_for_each_rejected_result() -> None:
    rejections = StructuredOutputRejections()
    for message in (attempt("t1"), reply("t1", MISSING_FIELD), attempt("t2"), reply("t2", "x")):
        rejections.observe(message)
    assert rejections.errors == [MISSING_FIELD, "x"]


def test_ignores_errors_from_other_tools_and_accepted_results() -> None:
    rejections = StructuredOutputRejections()
    for message in (
        attempt("e1", name="Edit"),
        reply("e1", "String to replace not found in file."),
        attempt("s1"),
        reply("s1", "Structured output provided successfully", is_error=False),
        AssistantMessage(content=[TextBlock(text="All done.")], model="claude-test"),
        UserMessage(content="a plain text user message"),
    ):
        rejections.observe(message)
    assert rejections.errors == []


def test_keeps_only_the_last_five_and_caps_each_reason() -> None:
    rejections = StructuredOutputRejections()
    for index in range(7):
        rejections.observe(attempt(f"t{index}"))
        rejections.observe(reply(f"t{index}", f"{index}:" + "y" * 1000))
    assert [error.split(":")[0] for error in rejections.errors] == ["2", "3", "4", "5", "6"]
    assert all(len(error) == 500 for error in rejections.errors)


def test_accepts_a_plain_string_tool_result() -> None:
    rejections = StructuredOutputRejections()
    rejections.observe(attempt("t1"))
    rejections.observe(
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content=MISSING_FIELD, is_error=True)]
        )
    )
    assert rejections.errors == [MISSING_FIELD]
