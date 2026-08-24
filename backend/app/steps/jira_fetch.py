"""Job step (a): fetch the ticket from Jira Cloud REST API v3. Deterministic, no LLM.

Ticket content comes back as Atlassian Document Format (ADF); it is flattened
to plain text here and treated as untrusted data from then on.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any, cast

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData
from app.steps.document_fetch import extract_pdf_text
from app.steps.jira_auth import JiraAuth, get_jira_auth

logger = get_logger(__name__)

_ACCEPTANCE_HEADING_RE = re.compile(r"(?im)^\s*acceptance criteria\s*:?\s*$")
_PDF_MAGIC = b"%PDF-"


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


async def _fetch_one_pdf_attachment(
    client: httpx.AsyncClient,
    attachment: dict[str, Any],
    auth: JiraAuth,
    settings: Settings,
    max_chars: int,
) -> str | None:
    """Fetch + extract one attachment's text, or None if it isn't usable.

    Best-effort: any failure here (network, non-PDF, corrupt, timeout) is
    logged and treated as "skip this attachment" — never raised, since an
    attachment is supplementary context, not required input.
    """
    filename = str(attachment.get("filename") or "attachment")
    mime_type = str(attachment.get("mimeType") or "")
    size = int(attachment.get("size") or 0)
    content_url = attachment.get("content")

    if mime_type != "application/pdf" or not content_url:
        logger.info(
            "skipping non-PDF Jira attachment %s (%s)", filename, mime_type or "unknown type"
        )
        return None
    if size > settings.jira_attachment_max_bytes_per_file:
        logger.info("skipping oversized Jira attachment %s (%d bytes)", filename, size)
        return None

    try:
        response = await client.get(
            str(content_url),
            headers={"Authorization": auth.auth_header},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.warning("failed to download Jira attachment %s: %s", filename, exc)
        return None
    if response.status_code != 200:
        logger.warning(
            "failed to download Jira attachment %s: status %d", filename, response.status_code
        )
        return None

    content = response.content
    if content[: len(_PDF_MAGIC)] != _PDF_MAGIC:
        logger.warning("Jira attachment %s claimed PDF but failed the magic-byte check", filename)
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf_text, tmp_path, max_chars),
                timeout=settings.jira_attachment_parse_timeout_seconds,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:  # pypdf raises assorted errors on malformed PDFs
        logger.warning("failed to parse Jira attachment %s: %s", filename, exc)
        return None

    if not text.strip():
        return None
    return f"{filename}:\n{text}"


async def fetch_attachment_texts(
    fields: dict[str, Any], auth: JiraAuth, settings: Settings
) -> list[str]:
    """Fetch and extract text from up to jira_attachment_max_count PDF
    attachments, capped at jira_attachment_max_total_chars combined."""
    if not settings.jira_attachment_fetch_enabled:
        return []
    attachment_list: list[dict[str, Any]] = fields.get("attachment") or []
    if not attachment_list:
        return []

    results: list[str] = []
    remaining_chars = settings.jira_attachment_max_total_chars
    async with httpx.AsyncClient(timeout=30) as client:
        for attachment in attachment_list[: settings.jira_attachment_max_count]:
            if remaining_chars <= 0:
                break
            text = await _fetch_one_pdf_attachment(
                client, attachment, auth, settings, remaining_chars
            )
            if text:
                results.append(text)
                remaining_chars -= len(text)
    return results


async def fetch_ticket(job: Job, store: JobStore) -> TicketData:
    settings = get_settings()
    auth = await get_jira_auth(job, store, settings)

    url = f"{auth.base_url}/rest/api/3/issue/{job.ticket_key}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params={"fields": "summary,description,comment,attachment"},
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

    try:
        attachments = await fetch_attachment_texts(fields, auth, settings)
    except Exception as exc:  # attachments are supplementary, never fail the fetch
        logger.warning("attachment fetch failed for %s: %s", job.ticket_key, exc)
        attachments = []

    logger.info(
        "fetched ticket %s (%d comments, %d attachments)",
        job.ticket_key,
        len(comments),
        len(attachments),
    )
    return TicketData(
        key=str(payload.get("key") or job.ticket_key),
        summary=summary,
        description=description,
        acceptance_criteria=acceptance,
        comments=comments,
        attachments=attachments,
    )
