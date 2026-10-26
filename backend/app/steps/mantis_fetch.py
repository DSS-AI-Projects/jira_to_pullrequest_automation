"""The MANTIS requirement source: fetch an issue from a MantisBT server's REST
API. Deterministic, no LLM — like the Jira fetch step, and producing the same
TicketData shape, so planning never knows which source was used.

Owner-approved access model: ONE shared, read-only Mantis account whose API
token lives in server config (`MANTIS_API_TOKEN`), read lazily here and
nowhere else. Mantis has no OAuth, and the app never takes a token typed into
its forms, so per-user Mantis access isn't possible. Every signed-in user can
therefore fetch any issue that account can see — limit the account's projects
in Mantis.

Issue content is untrusted data, quoted into the planning prompt inside the
same <ticket_data> block as Jira content (invariant 5).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from app.core import secrets
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData
from app.steps.document_fetch import extract_pdf_text_from_bytes

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 30
_PDF_MAGIC = b"%PDF-"

_NOT_FOUND_MESSAGE = (
    "That Mantis issue could not be found, or the server's Mantis account can't see it."
)
_EMPTY_MESSAGE = "The Mantis issue has no usable content (empty summary and description)."


def require_mantis_base_url(settings: Settings) -> str:
    """The configured Mantis base URL, or MANTIS_CONFIG_MISSING. Must be
    https: the shared account's token travels in a header to it."""
    base_url = settings.mantis_base_url
    if not settings.mantis_enabled or not base_url:
        raise AppError(
            ErrorCode.MANTIS_CONFIG_MISSING,
            internal_detail="MANTIS_ENABLED is off or MANTIS_BASE_URL is unset",
        )
    if urlparse(base_url).scheme != "https":
        raise AppError(
            ErrorCode.MANTIS_CONFIG_MISSING,
            internal_detail="MANTIS_BASE_URL must be https",
        )
    return base_url


def mantis_issue_url(base_url: str, issue_id: int) -> str:
    return f"{base_url}/view.php?id={issue_id}"


def mantis_ticket_key(settings: Settings, issue_id: int) -> str:
    return f"{settings.mantis_key_prefix}-{issue_id}"


def issue_id_from_key(ticket_key: str) -> int:
    """`MANTIS-1234` -> 1234. The prefix is configurable, the id never is."""
    return int(ticket_key.rsplit("-", 1)[1])


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _note_texts(issue: dict[str, Any]) -> list[str]:
    comments: list[str] = []
    notes = issue.get("notes")
    if not isinstance(notes, list):
        return comments
    for raw_note in cast(list[Any], notes):
        if not isinstance(raw_note, dict):
            continue
        note = cast(dict[str, Any], raw_note)
        body = _text(note.get("text"))
        if not body:
            continue
        reporter = note.get("reporter")
        author = "unknown"
        if isinstance(reporter, dict):
            reporter_fields = cast(dict[str, Any], reporter)
            author = (
                _text(reporter_fields.get("real_name"))
                or _text(reporter_fields.get("name"))
                or author
            )
        comments.append(f"{author}: {body}")
    return comments


async def _fetch_one_pdf_attachment(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    issue_id: int,
    attachment: dict[str, Any],
    settings: Settings,
    max_chars: int,
) -> str | None:
    """Download + extract one PDF attachment, or None if it isn't usable.
    Best-effort like Jira attachments: nothing here ever fails the job."""
    filename = _text(attachment.get("filename")) or "attachment"
    content_type = _text(attachment.get("content_type")).lower()
    file_id = attachment.get("id")
    size = int(attachment.get("size") or 0)
    if file_id is None or not (
        content_type == "application/pdf" or filename.lower().endswith(".pdf")
    ):
        logger.info("skipping non-PDF Mantis attachment %s", filename)
        return None
    if size > settings.jira_attachment_max_bytes_per_file:
        logger.info("skipping oversized Mantis attachment %s (%d bytes)", filename, size)
        return None

    try:
        response = await client.get(
            f"{base_url}/api/rest/issues/{issue_id}/files/{file_id}",
            headers={"Authorization": token, "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.warning("failed to download Mantis attachment %s: %s", filename, exc)
        return None
    if response.status_code != 200:
        logger.warning(
            "failed to download Mantis attachment %s: status %d", filename, response.status_code
        )
        return None

    try:
        files = cast(list[Any], cast(dict[str, Any], response.json()).get("files") or [])
        encoded = _text(cast(dict[str, Any], files[0]).get("content"))
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, IndexError, TypeError, AttributeError, binascii.Error) as exc:
        logger.warning("unexpected Mantis attachment response for %s: %s", filename, exc)
        return None
    if len(content) > settings.jira_attachment_max_bytes_per_file:
        logger.info("skipping oversized Mantis attachment %s", filename)
        return None
    if content[: len(_PDF_MAGIC)] != _PDF_MAGIC:
        logger.warning("Mantis attachment %s failed the PDF magic-byte check", filename)
        return None

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(extract_pdf_text_from_bytes, content, max_chars),
            timeout=settings.jira_attachment_parse_timeout_seconds,
        )
    except Exception as exc:  # pypdf raises assorted errors on malformed PDFs
        logger.warning("failed to parse Mantis attachment %s: %s", filename, exc)
        return None
    if not text.strip():
        return None
    return f"{filename}:\n{text}"


async def _fetch_attachment_texts(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    issue_id: int,
    issue: dict[str, Any],
    settings: Settings,
) -> list[str]:
    if not settings.mantis_attachment_fetch_enabled:
        return []
    raw_attachments = issue.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    results: list[str] = []
    remaining_chars = settings.jira_attachment_max_total_chars
    attachments = [
        cast(dict[str, Any], item)
        for item in cast(list[Any], raw_attachments)
        if isinstance(item, dict)
    ]
    for attachment in attachments[: settings.jira_attachment_max_count]:
        if remaining_chars <= 0:
            break
        text = await _fetch_one_pdf_attachment(
            client, base_url, token, issue_id, attachment, settings, remaining_chars
        )
        if text:
            results.append(text)
            remaining_chars -= len(text)
    return results


async def fetch_mantis_issue(job: Job, store: JobStore) -> TicketData:
    del store
    settings = get_settings()
    base_url = require_mantis_base_url(settings)
    token = secrets.get_mantis_api_token()
    if not token:
        raise AppError(ErrorCode.MANTIS_CONFIG_MISSING, internal_detail="MANTIS_API_TOKEN is unset")
    issue_id = issue_id_from_key(job.ticket_key)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(
                f"{base_url}/api/rest/issues/{issue_id}",
                # MantisBT takes the API token as the bare Authorization value.
                headers={"Authorization": token, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE, internal_detail=f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise AppError(
                ErrorCode.MANTIS_AUTH_FAILED,
                internal_detail=f"mantis returned {response.status_code}",
            )
        if response.status_code == 404:
            raise AppError(ErrorCode.TICKET_NOT_FOUND, user_message=_NOT_FOUND_MESSAGE)
        if response.status_code != 200:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE,
                internal_detail=f"mantis returned {response.status_code}",
            )

        try:
            payload = cast(dict[str, Any], response.json())
            issues = payload.get("issues")
            issue = cast(dict[str, Any], issues[0]) if isinstance(issues, list) and issues else None
        except (ValueError, AttributeError) as exc:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE,
                internal_detail=f"mantis returned a non-JSON or unexpected body: {exc}",
            ) from exc
        if not isinstance(issue, dict):
            raise AppError(ErrorCode.TICKET_NOT_FOUND, user_message=_NOT_FOUND_MESSAGE)

        summary = _text(issue.get("summary"))
        description = _text(issue.get("description"))
        steps = _text(issue.get("steps_to_reproduce"))
        if steps:
            description = f"{description}\n\nSteps to reproduce:\n{steps}".strip()
        additional = _text(issue.get("additional_information"))
        if not summary and not description:
            raise AppError(ErrorCode.TICKET_EMPTY, user_message=_EMPTY_MESSAGE)
        comments = _note_texts(issue)

        try:
            attachments = await _fetch_attachment_texts(
                client, base_url, token, issue_id, issue, settings
            )
        except Exception as exc:  # attachments are supplementary, never fail the fetch
            logger.warning("attachment fetch failed for %s: %s", job.ticket_key, exc)
            attachments = []

    logger.info(
        "fetched Mantis issue %s (%d notes, %d attachments)",
        job.ticket_key,
        len(comments),
        len(attachments),
    )
    return TicketData(
        key=job.ticket_key,
        summary=summary,
        description=description,
        acceptance_criteria=additional or None,
        comments=comments,
        attachments=attachments,
    )
