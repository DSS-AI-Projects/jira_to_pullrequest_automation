"""Why the harness rejected an agent's structured result, for the backend log.

Both agents end by calling the CLI's `StructuredOutput` tool with their result.
When that result doesn't match the schema, the CLI answers the call with an
error tool result naming the problem ("Output does not match required schema:
root: must have required property 'changed_files'") and the model tries again,
up to five times. The final `ResultMessage` only says "Failed to provide valid
structured output after 5 attempts", so without these messages the reason
survives only in the CLI's own transcript inside the container. Observing the
message stream keeps the last few reasons for the failure's internal_detail
(logged, redacted; never shown to the client).
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import AssistantMessage, Message, ToolResultBlock, ToolUseBlock, UserMessage

STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
_MAX_KEPT = 5
_MAX_CHARS = 500


def _text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(str(part.get("text", "")) for part in content)


class StructuredOutputRejections:
    """Collects the CLI's error replies to the agent's result attempts."""

    def __init__(self) -> None:
        self._attempt_ids: set[str] = set()
        self.errors: list[str] = []

    def observe(self, message: Message) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name == STRUCTURED_OUTPUT_TOOL:
                    self._attempt_ids.add(block.id)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and block.is_error
                    and block.tool_use_id in self._attempt_ids
                ):
                    self.errors.append(_text(block.content)[:_MAX_CHARS])
                    del self.errors[:-_MAX_KEPT]
