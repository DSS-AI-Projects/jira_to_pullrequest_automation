"""The MANTIS requirement source: fetch an issue from a MantisBT server.
Deterministic, no LLM — like the Jira fetch step, and producing the same
TicketData shape, so planning never knows which source was used.

Owner-approved access model: ONE shared, read-only Mantis account whose
credentials live in server config only, read lazily here and nowhere else.
Mantis has no OAuth, and the app never takes a credential typed into its
forms, so per-user Mantis access isn't possible. Every signed-in user can
therefore fetch any issue that account can see — limit the account's
projects in Mantis.

Two transports (`MANTIS_API`), both returning the issue as the REST API's
JSON shape so the mapping below is shared:

- `rest` (default): `GET /api/rest/issues/{id}` with the account's API token
  (`MANTIS_API_TOKEN`) as the bare Authorization value.
- `soap`: MantisConnect's `mc_issue_get` / `mc_issue_attachment_get` at
  `/api/soap/mantisconnect.php`, signed in with `MANTIS_USERNAME` +
  `MANTIS_PASSWORD` (owner-approved for a Mantis whose customization breaks
  REST API tokens; a dedicated, local, viewer-level account). The password
  travels in the request body, so https is required here too, and request
  bodies are never logged.

Issue content is untrusted data, quoted into the planning prompt inside the
same <ticket_data> block as Jira content (invariant 5).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any, Protocol, cast
from urllib.parse import urlparse
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx

from app.core import secrets
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import Job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData
from app.steps.document_fetch import extract_pdf_text_from_bytes

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 30
_PDF_MAGIC = b"%PDF-"
_SOAP_NAMESPACE = "http://futureware.biz/mantisconnect"

_NOT_FOUND_MESSAGE = (
    "That Mantis issue could not be found, or the server's Mantis account can't see it."
)
_EMPTY_MESSAGE = "The Mantis issue has no usable content (empty summary and description)."


def require_mantis_base_url(settings: Settings) -> str:
    """The configured Mantis base URL, or MANTIS_CONFIG_MISSING. Must be
    https: the shared account's credentials travel to it. SOAP mode also
    needs MANTIS_USERNAME (non-secret, so checkable here); the secrets
    themselves are only read inside the fetch step."""
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
    if settings.mantis_api == "soap" and not settings.mantis_username:
        raise AppError(
            ErrorCode.MANTIS_CONFIG_MISSING,
            internal_detail="MANTIS_API=soap needs MANTIS_USERNAME",
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


# --- transports ---


class _MantisTransport(Protocol):
    async def get_issue(self, issue_id: int) -> dict[str, Any]:
        """The issue in the REST API's JSON shape, or a typed AppError."""
        ...

    async def get_attachment(self, issue_id: int, file_id: Any) -> bytes | None:
        """An attachment's bytes, or None if it can't be had (best-effort)."""
        ...


class _RestTransport:
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._client = client
        self._base_url = base_url
        # MantisBT takes the API token as the bare Authorization value.
        self._headers = {"Authorization": token, "Accept": "application/json"}

    async def get_issue(self, issue_id: int) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base_url}/api/rest/issues/{issue_id}", headers=self._headers
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
        return issue

    async def get_attachment(self, issue_id: int, file_id: Any) -> bytes | None:
        try:
            response = await self._client.get(
                f"{self._base_url}/api/rest/issues/{issue_id}/files/{file_id}",
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("failed to download Mantis attachment %s: %s", file_id, exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "failed to download Mantis attachment %s: status %d",
                file_id,
                response.status_code,
            )
            return None
        try:
            files = cast(list[Any], cast(dict[str, Any], response.json()).get("files") or [])
            encoded = _text(cast(dict[str, Any], files[0]).get("content"))
            return base64.b64decode(encoded, validate=True)
        except (ValueError, IndexError, TypeError, AttributeError, binascii.Error) as exc:
            logger.warning("unexpected Mantis attachment response for %s: %s", file_id, exc)
            return None


def _local(tag: str) -> str:
    """An element's name without its namespace: SOAP replies mix several."""
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((child for child in element if _local(child.tag) == name), None)


def _child_text(element: ElementTree.Element, name: str) -> str:
    child = _child(element, name)
    return (child.text or "").strip() if child is not None else ""


def _items(element: ElementTree.Element | None) -> list[ElementTree.Element]:
    return [] if element is None else [child for child in element if _local(child.tag) == "item"]


def _parse_soap(body: bytes) -> ElementTree.Element:
    """Parse a SOAP reply, refusing any DTD. SOAP forbids them, and DTDs are
    what XML entity-expansion and external-entity attacks need — this is the
    one guard defusedxml would add for this use, without a new dependency."""
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        raise ValueError("SOAP reply contains a DTD")
    return ElementTree.fromstring(body)


def _soap_issue_to_rest_shape(issue: ElementTree.Element) -> dict[str, Any]:
    notes: list[dict[str, Any]] = []
    for item in _items(_child(issue, "notes")):
        reporter = _child(item, "reporter")
        notes.append(
            {
                "text": _child_text(item, "text"),
                "reporter": {
                    "name": _child_text(reporter, "name") if reporter is not None else "",
                    "real_name": _child_text(reporter, "real_name") if reporter is not None else "",
                },
            }
        )
    attachments: list[dict[str, Any]] = []
    for item in _items(_child(issue, "attachments")):
        size = _child_text(item, "size")
        attachments.append(
            {
                "id": _child_text(item, "id") or None,
                "filename": _child_text(item, "filename"),
                "size": int(size) if size.isdigit() else 0,
                "content_type": _child_text(item, "content_type"),
            }
        )
    return {
        "summary": _child_text(issue, "summary"),
        "description": _child_text(issue, "description"),
        "steps_to_reproduce": _child_text(issue, "steps_to_reproduce"),
        "additional_information": _child_text(issue, "additional_information"),
        "notes": notes,
        "attachments": attachments,
    }


class _SoapTransport:
    def __init__(
        self, client: httpx.AsyncClient, base_url: str, username: str, password: str
    ) -> None:
        self._client = client
        self._url = f"{base_url}/api/soap/mantisconnect.php"
        self._username = username
        self._password = password

    def _envelope(self, operation: str, arguments: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            f' xmlns:man="{_SOAP_NAMESPACE}"><soapenv:Body><man:{operation}>'
            f"<username>{escape(self._username)}</username>"
            f"<password>{escape(self._password)}</password>"
            f"{arguments}</man:{operation}></soapenv:Body></soapenv:Envelope>"
        )

    async def _call(self, operation: str, arguments: str) -> ElementTree.Element:
        """The operation's <return> element, or a typed AppError. Faults come
        back as HTTP 500 with a <faultstring>, so the body is read first."""
        try:
            response = await self._client.post(
                self._url,
                content=self._envelope(operation, arguments).encode(),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": operation},
            )
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE, internal_detail=f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise AppError(
                ErrorCode.MANTIS_AUTH_FAILED,
                internal_detail=f"mantis soap returned {response.status_code}",
            )
        try:
            root = _parse_soap(response.content)
        except (ValueError, ElementTree.ParseError) as exc:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE,
                internal_detail=(
                    f"mantis soap returned {response.status_code} with an unreadable body: {exc}"
                ),
            ) from exc

        fault = next((el for el in root.iter() if _local(el.tag) == "faultstring"), None)
        if fault is not None:
            message = (fault.text or "").strip()
            lowered = message.lower()
            if "access denied" in lowered or "not authorized" in lowered:
                raise AppError(
                    ErrorCode.MANTIS_AUTH_FAILED, internal_detail=redact(f"soap fault: {message}")
                )
            if "not found" in lowered or "does not exist" in lowered:
                raise AppError(ErrorCode.TICKET_NOT_FOUND, user_message=_NOT_FOUND_MESSAGE)
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE, internal_detail=redact(f"soap fault: {message}")
            )
        result = next((el for el in root.iter() if _local(el.tag) == "return"), None)
        if response.status_code != 200 or result is None:
            raise AppError(
                ErrorCode.MANTIS_UNREACHABLE,
                internal_detail=f"mantis soap returned {response.status_code} without a result",
            )
        return result

    async def get_issue(self, issue_id: int) -> dict[str, Any]:
        result = await self._call("mc_issue_get", f"<issue_id>{issue_id}</issue_id>")
        return _soap_issue_to_rest_shape(result)

    async def get_attachment(self, issue_id: int, file_id: Any) -> bytes | None:
        del issue_id  # attachment ids are global in MantisConnect
        try:
            result = await self._call(
                "mc_issue_attachment_get",
                f"<issue_attachment_id>{escape(_text(file_id))}</issue_attachment_id>",
            )
            return base64.b64decode((result.text or "").strip(), validate=True)
        except (AppError, ValueError, binascii.Error) as exc:
            logger.warning("failed to download Mantis attachment %s: %s", file_id, exc)
            return None


# --- mapping (shared by both transports) ---


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
    transport: _MantisTransport,
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

    content = await transport.get_attachment(issue_id, file_id)
    if content is None:
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
    transport: _MantisTransport,
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
            transport, issue_id, attachment, settings, remaining_chars
        )
        if text:
            results.append(text)
            remaining_chars -= len(text)
    return results


def _transport(client: httpx.AsyncClient, base_url: str, settings: Settings) -> _MantisTransport:
    """The configured transport, with its credentials read now (lazily, in
    the fetch step only) — or MANTIS_CONFIG_MISSING before any request."""
    if settings.mantis_api == "soap":
        password = secrets.get_mantis_password()
        if not settings.mantis_username or not password:
            raise AppError(
                ErrorCode.MANTIS_CONFIG_MISSING,
                internal_detail="MANTIS_API=soap needs MANTIS_USERNAME and MANTIS_PASSWORD",
            )
        return _SoapTransport(client, base_url, settings.mantis_username, password)
    token = secrets.get_mantis_api_token()
    if not token:
        raise AppError(ErrorCode.MANTIS_CONFIG_MISSING, internal_detail="MANTIS_API_TOKEN is unset")
    return _RestTransport(client, base_url, token)


async def fetch_mantis_issue(job: Job, store: JobStore) -> TicketData:
    del store
    settings = get_settings()
    base_url = require_mantis_base_url(settings)
    issue_id = issue_id_from_key(job.ticket_key)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        transport = _transport(client, base_url, settings)
        issue = await transport.get_issue(issue_id)

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
            attachments = await _fetch_attachment_texts(transport, issue_id, issue, settings)
        except Exception as exc:  # attachments are supplementary, never fail the fetch
            logger.warning("attachment fetch failed for %s: %s", job.ticket_key, exc)
            attachments = []

    logger.info(
        "fetched Mantis issue %s via %s (%d notes, %d attachments)",
        job.ticket_key,
        settings.mantis_api,
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
