"""Typed, user-safe error catalog — the failure-behavior contract.

Every failure surfaced to the client (HTTP response or job record) is an
ErrorCode plus a user-safe message. Internal detail is logged (through the
redactor) but never returned to the client.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    AUTH_NOT_AVAILABLE = "AUTH_NOT_AVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JIRA_CONFIG_MISSING = "JIRA_CONFIG_MISSING"
    JIRA_AUTH_FAILED = "JIRA_AUTH_FAILED"
    JIRA_UNREACHABLE = "JIRA_UNREACHABLE"
    JIRA_OAUTH_NOT_AVAILABLE = "JIRA_OAUTH_NOT_AVAILABLE"
    JIRA_OAUTH_STATE_INVALID = "JIRA_OAUTH_STATE_INVALID"
    JIRA_OAUTH_CALLBACK_FAILED = "JIRA_OAUTH_CALLBACK_FAILED"
    JIRA_SITE_NOT_ACCESSIBLE = "JIRA_SITE_NOT_ACCESSIBLE"
    REPO_PROVIDER_NOT_AVAILABLE = "REPO_PROVIDER_NOT_AVAILABLE"
    REPO_PROVIDER_STATE_INVALID = "REPO_PROVIDER_STATE_INVALID"
    REPO_PROVIDER_CALLBACK_FAILED = "REPO_PROVIDER_CALLBACK_FAILED"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    TICKET_EMPTY = "TICKET_EMPTY"
    REPO_HOST_NOT_ALLOWED = "REPO_HOST_NOT_ALLOWED"
    LOCAL_REPO_NOT_ALLOWED = "LOCAL_REPO_NOT_ALLOWED"
    LOCAL_REPO_NOT_FOUND = "LOCAL_REPO_NOT_FOUND"
    LOCAL_REPO_NOT_DIRECTORY = "LOCAL_REPO_NOT_DIRECTORY"
    LOCAL_REPO_OUTSIDE_ALLOWED_ROOT = "LOCAL_REPO_OUTSIDE_ALLOWED_ROOT"
    LOCAL_REPO_NOT_GIT = "LOCAL_REPO_NOT_GIT"
    LOCAL_REPO_DIRTY = "LOCAL_REPO_DIRTY"
    LOCAL_REPO_BRANCH_MISMATCH = "LOCAL_REPO_BRANCH_MISMATCH"
    CLONE_FAILED = "CLONE_FAILED"
    REPO_MAP_FAILED = "REPO_MAP_FAILED"
    AGENT_CONFIG_MISSING = "AGENT_CONFIG_MISSING"
    AGENT_REQUEST_FAILED = "AGENT_REQUEST_FAILED"
    PLAN_INVALID = "PLAN_INVALID"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    IMPLEMENTATION_NOT_READY = "IMPLEMENTATION_NOT_READY"
    IMPLEMENTATION_NOT_SUPPORTED = "IMPLEMENTATION_NOT_SUPPORTED"
    IMPLEMENTATION_WORKSPACE_MISSING = "IMPLEMENTATION_WORKSPACE_MISSING"
    INTERNAL = "INTERNAL"


DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INPUT_INVALID: "The submitted input is invalid.",
    ErrorCode.UNAUTHENTICATED: "Sign in is required before using this application.",
    ErrorCode.FORBIDDEN: "You do not have access to that resource.",
    ErrorCode.AUTH_NOT_AVAILABLE: (
        "Interactive sign-in is not available on this server. Use the configured "
        "upstream identity provider or trusted proxy."
    ),
    ErrorCode.JOB_NOT_FOUND: "No job exists with that id.",
    ErrorCode.JIRA_CONFIG_MISSING: (
        "Jira is not configured on the server. "
        "Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in backend/.env."
    ),
    ErrorCode.JIRA_AUTH_FAILED: (
        "Jira rejected the server's credentials. Check JIRA_EMAIL and JIRA_API_TOKEN."
    ),
    ErrorCode.JIRA_UNREACHABLE: "Could not reach Jira. Check JIRA_BASE_URL and your network.",
    ErrorCode.JIRA_OAUTH_NOT_AVAILABLE: (
        "Per-user Jira sign-in is not configured on this server."
    ),
    ErrorCode.JIRA_OAUTH_STATE_INVALID: (
        "That Jira sign-in attempt is missing or expired. Start the connect flow again."
    ),
    ErrorCode.JIRA_OAUTH_CALLBACK_FAILED: (
        "Jira sign-in could not be completed. Try connecting again."
    ),
    ErrorCode.JIRA_SITE_NOT_ACCESSIBLE: (
        "Your Jira account cannot access the Jira site configured on this server."
    ),
    ErrorCode.REPO_PROVIDER_NOT_AVAILABLE: (
        "That repository provider sign-in is not configured on this server."
    ),
    ErrorCode.REPO_PROVIDER_STATE_INVALID: (
        "That repository provider sign-in attempt is missing or expired. Start the connect flow again."
    ),
    ErrorCode.REPO_PROVIDER_CALLBACK_FAILED: (
        "Repository provider sign-in could not be completed. Try connecting again."
    ),
    ErrorCode.TICKET_NOT_FOUND: "That Jira ticket could not be found (or is not visible).",
    ErrorCode.TICKET_EMPTY: "The ticket has no usable content (empty summary and description).",
    ErrorCode.REPO_HOST_NOT_ALLOWED: "That repository host is not on the allowed list.",
    ErrorCode.LOCAL_REPO_NOT_ALLOWED: (
        "Local repository paths are not enabled in this environment."
    ),
    ErrorCode.LOCAL_REPO_NOT_FOUND: "The local repository path does not exist.",
    ErrorCode.LOCAL_REPO_NOT_DIRECTORY: (
        "The local repository path must point to a directory."
    ),
    ErrorCode.LOCAL_REPO_OUTSIDE_ALLOWED_ROOT: (
        "That local repository path is outside the allowed local roots."
    ),
    ErrorCode.LOCAL_REPO_NOT_GIT: "The local repository path must point to a Git working tree.",
    ErrorCode.LOCAL_REPO_DIRTY: (
        "The local repository has uncommitted changes. Commit or stash them first."
    ),
    ErrorCode.LOCAL_REPO_BRANCH_MISMATCH: (
        "The local repository branch does not match the Jira ticket key."
    ),
    ErrorCode.CLONE_FAILED: (
        "Cloning the repository failed. Check the URL and that your machine's "
        "git credentials (SSH key / credential helper) can access it."
    ),
    ErrorCode.REPO_MAP_FAILED: "Analyzing the repository structure failed.",
    ErrorCode.AGENT_CONFIG_MISSING: (
        "The planning agent is not configured on the server. Set ANTHROPIC_API_KEY in backend/.env."
    ),
    ErrorCode.AGENT_REQUEST_FAILED: (
        "The planning agent request failed. Check the Anthropic API key and "
        "available credits or quota for this environment."
    ),
    ErrorCode.PLAN_INVALID: (
        "The planning agent could not produce a valid plan for this ticket. "
        "Try again, or refine the ticket description."
    ),
    ErrorCode.BUDGET_EXCEEDED: (
        "The planning agent exceeded its run budget before finishing a plan."
    ),
    ErrorCode.VALIDATION_FAILED: (
        "Running post-implementation validation failed unexpectedly."
    ),
    ErrorCode.IMPLEMENTATION_NOT_READY: (
        "This job is not ready for implementation approval yet."
    ),
    ErrorCode.IMPLEMENTATION_NOT_SUPPORTED: (
        "Implementation is currently available only for jobs created from "
        "approved local repositories."
    ),
    ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING: (
        "The isolated workspace for this job is unavailable. Regenerate the plan and try again."
    ),
    ErrorCode.INTERNAL: "An internal error occurred.",
}

HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INPUT_INVALID: 400,
    ErrorCode.AUTH_NOT_AVAILABLE: 400,
    ErrorCode.JIRA_OAUTH_NOT_AVAILABLE: 400,
    ErrorCode.JIRA_OAUTH_STATE_INVALID: 400,
    ErrorCode.JIRA_OAUTH_CALLBACK_FAILED: 400,
    ErrorCode.JIRA_SITE_NOT_ACCESSIBLE: 400,
    ErrorCode.REPO_PROVIDER_NOT_AVAILABLE: 400,
    ErrorCode.REPO_PROVIDER_STATE_INVALID: 400,
    ErrorCode.REPO_PROVIDER_CALLBACK_FAILED: 400,
    ErrorCode.REPO_HOST_NOT_ALLOWED: 400,
    ErrorCode.LOCAL_REPO_NOT_ALLOWED: 400,
    ErrorCode.LOCAL_REPO_NOT_FOUND: 400,
    ErrorCode.LOCAL_REPO_NOT_DIRECTORY: 400,
    ErrorCode.LOCAL_REPO_OUTSIDE_ALLOWED_ROOT: 400,
    ErrorCode.LOCAL_REPO_NOT_GIT: 400,
    ErrorCode.LOCAL_REPO_DIRTY: 400,
    ErrorCode.LOCAL_REPO_BRANCH_MISMATCH: 400,
    ErrorCode.IMPLEMENTATION_NOT_READY: 400,
    ErrorCode.IMPLEMENTATION_NOT_SUPPORTED: 400,
    ErrorCode.IMPLEMENTATION_WORKSPACE_MISSING: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
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
