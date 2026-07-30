"""Form input validation — security invariants 1 and 5.

The submit payload has exactly two non-secret fields; anything extra is
rejected by extra="forbid" before it reaches application code. Field values
that look like credentials are rejected without ever being logged or stored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.secrets import looks_token_shaped

TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}-\d{1,10}$")
_BROWSE_PATH_RE = re.compile(r"/browse/([A-Za-z][A-Za-z0-9_]{1,63}-\d{1,10})(?:$|/)")
_SELECTED_ISSUE_RE = re.compile(r"selectedIssue=([A-Za-z][A-Za-z0-9_]{1,63}-\d{1,10})")
_SCP_LIKE_RE = re.compile(r"^git@([A-Za-z0-9.-]+):([A-Za-z0-9._/~-]+?)(?:\.git)?/?$")
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class JobCreateRequest(BaseModel):
    # extra="forbid": a request smuggling any additional field (a token, a
    # password, anything) fails validation and is never processed (invariant 1).
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=1, max_length=2000, description="Jira ticket key or URL")
    repo: str = Field(
        min_length=1,
        max_length=2000,
        description="Repo URL, pre-configured repo name, or approved local path",
    )


class RepoChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str


class ImplementRequest(BaseModel):
    # extra="forbid": same invariant-1 treatment as JobCreateRequest — this is
    # another free-text field a user could paste a credential into.
    model_config = ConfigDict(extra="forbid")

    clarifications: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional guidance to consider during implementation",
    )


def _reject_credential_shaped(value: str, field: str) -> None:
    if looks_token_shaped(value):
        # Do not log or echo the value anywhere.
        raise AppError(
            ErrorCode.INPUT_INVALID,
            user_message=(
                f"The {field} field looks like it contains a credential. "
                "This app never accepts secrets; submit only an identifier."
            ),
        )


def normalize_ticket(raw: str, settings: Settings) -> str:
    """Return the canonical ticket key (e.g. PROJ-123) or raise INPUT_INVALID."""
    value = raw.strip()
    _reject_credential_shaped(value, "ticket")

    if TICKET_KEY_RE.match(value.upper()):
        return value.upper()

    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        if settings.jira_base_url:
            configured_host = urlparse(settings.jira_base_url).hostname
            if configured_host and parsed.hostname.lower() != configured_host.lower():
                raise AppError(
                    ErrorCode.INPUT_INVALID,
                    user_message="That ticket URL does not match the configured Jira host.",
                )
        match = _BROWSE_PATH_RE.search(parsed.path) or _SELECTED_ISSUE_RE.search(parsed.query or "")
        if match:
            return match.group(1).upper()

    raise AppError(
        ErrorCode.INPUT_INVALID,
        user_message="Enter a Jira ticket key like PROJ-123, or a ticket URL from your Jira.",
    )


def normalize_clarifications(raw: str | None) -> str | None:
    """Trim and validate free-text implementation guidance.

    Blank input normalizes to None so a no-op submission never leaves an empty
    <user_clarifications> block in the implement prompt. Token-shaped input is
    rejected the same way ticket/repo fields are (invariant 1) — never logged
    or echoed.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    _reject_credential_shaped(value, "clarifications")
    return value


def load_preconfigured_repos(settings: Settings) -> list[RepoChoice]:
    path = settings.preconfigured_repos_file
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [RepoChoice.model_validate(entry) for entry in data.get("repos", [])]


def _looks_like_local_path(value: str) -> bool:
    return bool(_WINDOWS_ABS_PATH_RE.match(value))


def _validate_local_repo_path(value: str, settings: Settings) -> str:
    if not settings.allow_local_repos:
        raise AppError(ErrorCode.LOCAL_REPO_NOT_ALLOWED)

    candidate = Path(value).expanduser().resolve()
    if not candidate.exists():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_FOUND)
    if not candidate.is_dir():
        raise AppError(ErrorCode.LOCAL_REPO_NOT_DIRECTORY)

    allowed_roots = [root.expanduser().resolve() for root in settings.allowed_local_repo_roots]
    if not any(root == candidate or root in candidate.parents for root in allowed_roots):
        raise AppError(ErrorCode.LOCAL_REPO_OUTSIDE_ALLOWED_ROOT)

    return str(candidate)


def _validate_repo_url(value: str, settings: Settings) -> str:
    """Validate shape + host allowlist. Never accepts embedded credentials."""
    scp = _SCP_LIKE_RE.match(value)
    if scp:
        host = scp.group(1).lower()
        _require_allowed_host(host, settings)
        return value

    parsed = urlparse(value)
    if parsed.scheme not in ("https", "ssh") or not parsed.hostname:
        raise AppError(
            ErrorCode.INPUT_INVALID,
            user_message=(
                "Enter an https:// or ssh:// repository URL, git@host:owner/repo, "
                "or the name of a pre-configured repo."
            ),
        )
    if parsed.password or (parsed.username and parsed.username != "git"):
        # Credentials embedded in a URL are never accepted (invariant 4).
        raise AppError(
            ErrorCode.INPUT_INVALID,
            user_message=(
                "Repository URLs must not contain credentials. "
                "Cloning uses your machine's own git auth."
            ),
        )
    _require_allowed_host(parsed.hostname.lower(), settings)
    return value


def _require_allowed_host(host: str, settings: Settings) -> None:
    if host not in {h.lower() for h in settings.allowed_git_hosts}:
        raise AppError(
            ErrorCode.REPO_HOST_NOT_ALLOWED,
            user_message=(
                f"Host '{host}' is not on the allowed list "
                f"({', '.join(settings.allowed_git_hosts)}). "
                "Add it via ALLOWED_GIT_HOSTS if intended."
            ),
        )


def normalize_repo(raw: str, settings: Settings) -> str:
    """Resolve a pre-configured repo name or validate a repo identifier."""
    value = raw.strip()
    _reject_credential_shaped(value, "repo")

    for choice in load_preconfigured_repos(settings):
        if value == choice.name:
            return _validate_repo_url(choice.url, settings)

    if _looks_like_local_path(value):
        return _validate_local_repo_path(value, settings)

    return _validate_repo_url(value, settings)
