"""Typed, user-safe error catalog — the failure-behavior contract.

Every failure surfaced to the client (HTTP response or job record) is an
ErrorCode plus a user-safe message. Internal detail is logged (through the
redactor) but never returned to the client.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JIRA_CONFIG_MISSING = "JIRA_CONFIG_MISSING"
    JIRA_AUTH_FAILED = "JIRA_AUTH_FAILED"
    JIRA_UNREACHABLE = "JIRA_UNREACHABLE"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    TICKET_EMPTY = "TICKET_EMPTY"
    REPO_HOST_NOT_ALLOWED = "REPO_HOST_NOT_ALLOWED"
    CLONE_FAILED = "CLONE_FAILED"
    REPO_MAP_FAILED = "REPO_MAP_FAILED"
    PLAN_INVALID = "PLAN_INVALID"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL = "INTERNAL"


DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INPUT_INVALID: "The submitted input is invalid.",
    ErrorCode.JOB_NOT_FOUND: "No job exists with that id.",
    ErrorCode.JIRA_CONFIG_MISSING: (
        "Jira is not configured on the server. "
        "Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in backend/.env."
    ),
    ErrorCode.JIRA_AUTH_FAILED: (
        "Jira rejected the server's credentials. Check JIRA_EMAIL and JIRA_API_TOKEN."
    ),
    ErrorCode.JIRA_UNREACHABLE: "Could not reach Jira. Check JIRA_BASE_URL and your network.",
    ErrorCode.TICKET_NOT_FOUND: "That Jira ticket could not be found (or is not visible).",
    ErrorCode.TICKET_EMPTY: "The ticket has no usable content (empty summary and description).",
    ErrorCode.REPO_HOST_NOT_ALLOWED: "That repository host is not on the allowed list.",
    ErrorCode.CLONE_FAILED: (
        "Cloning the repository failed. Check the URL and that your machine's "
        "git credentials (SSH key / credential helper) can access it."
    ),
    ErrorCode.REPO_MAP_FAILED: "Analyzing the repository structure failed.",
    ErrorCode.PLAN_INVALID: (
        "The planning agent could not produce a valid plan for this ticket. "
        "Try again, or refine the ticket description."
    ),
    ErrorCode.BUDGET_EXCEEDED: (
        "The planning agent exceeded its run budget before finishing a plan."
    ),
    ErrorCode.INTERNAL: "An internal error occurred.",
}

HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INPUT_INVALID: 400,
    ErrorCode.REPO_HOST_NOT_ALLOWED: 400,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.INTERNAL: 500,
}


class AppError(Exception):
    """A typed error safe to surface to the client.

    `user_message` is shown to the user; `internal_detail` is only logged
    (through the redactor) and must never be returned in a response.
    """

    def __init__(
        self,
        code: ErrorCode,
        user_message: str | None = None,
        internal_detail: str | None = None,
    ) -> None:
        self.code = code
        self.user_message = user_message or DEFAULT_MESSAGES[code]
        self.internal_detail = internal_detail
        super().__init__(f"{code}: {self.user_message}")

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code.value, "message": self.user_message}}
