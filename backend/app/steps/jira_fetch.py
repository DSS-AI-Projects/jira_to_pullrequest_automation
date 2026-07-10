"""Job step (a): fetch the ticket from Jira Cloud REST API v3. Deterministic, no LLM.

Ticket content comes back as Atlassian Document Format (ADF); it is flattened
to plain text here and treated as untrusted data from then on.
"""

from __future__ import annotations

import re
from typing import Any, cast

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.schemas.ticket import TicketData
from app.steps.jira_auth import get_jira_auth

logger = get_logger(__name__)

_ACCEPTANCE_HEADING_RE = re.compile(r"(?im)^\s*acceptance criteria\s*:?\s*$")


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document to plain text (best effort, loses formatting)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    doc = cast(dict[str, Any], node)
    node_type = str(doc.get("type", ""))
    if node_type == "text":
        return str(doc.get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("mention", "emoji", "status"):
        attrs = doc.get("attrs")
        if isinstance(attrs, dict):
            return str(cast(dict[str, Any], attrs).get("text", ""))
        return ""

    content = doc.get("content")
    parts: list[str] = (
        [adf_to_text(child) for child in cast(list[Any], content)]
        if isinstance(content, list)
        else []
    )
    text = "".join(parts)
    block_types = {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "listItem",
        "tableRow",
        "rule",
    }
    if node_type in block_types:
        text += "\n"
    return text


def split_acceptance_criteria(description: str) -> tuple[str, str | None]:
    """If the description has an 'Acceptance Criteria' heading, split it out."""
    match = _ACCEPTANCE_HEADING_RE.search(description)
    if not match:
        return description, None
    before = description[: match.start()].strip()
    after = description[match.end() :].strip()
    return before, after or None


async def fetch_ticket(ticket_key: str) -> TicketData:
    settings = get_settings()
    auth = get_jira_auth(settings)  # lazy: the token lives only in this call stack

    url = f"{auth.base_url}/rest/api/3/issue/{ticket_key}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params={"fields": "summary,description,comment"},
                headers={"Authorization": auth.auth_header, "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.JIRA_UNREACHABLE, internal_detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise AppError(
            ErrorCode.JIRA_AUTH_FAILED, internal_detail=f"jira returned {response.status_code}"
        )
    if response.status_code == 404:
        raise AppError(ErrorCode.TICKET_NOT_FOUND)
    if response.status_code != 200:
        raise AppError(ErrorCode.INTERNAL, internal_detail=f"jira returned {response.status_code}")

    payload: dict[str, Any] = response.json()
    fields: dict[str, Any] = payload.get("fields") or {}
    summary = str(fields.get("summary") or "").strip()
    description_text = adf_to_text(fields.get("description")).strip()
    description, acceptance = split_acceptance_criteria(description_text)

    comments: list[str] = []
    comment_field: dict[str, Any] = fields.get("comment") or {}
    comment_list: list[dict[str, Any]] = comment_field.get("comments") or []
    for comment in comment_list:
        author_field: dict[str, Any] = comment.get("author") or {}
        author = str(author_field.get("displayName") or "unknown")
        body = adf_to_text(comment.get("body")).strip()
        if body:
            comments.append(f"{author}: {body}")

    if not summary and not description:
        raise AppError(ErrorCode.TICKET_EMPTY)

    logger.info("fetched ticket %s (%d comments)", ticket_key, len(comments))
    return TicketData(
        key=str(payload.get("key") or ticket_key),
        summary=summary,
        description=description,
        acceptance_criteria=acceptance,
        comments=comments,
    )
