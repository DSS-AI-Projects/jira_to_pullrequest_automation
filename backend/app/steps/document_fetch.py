"""Job step (a-alt): extract requirement text from an uploaded PDF instead of
fetching a Jira ticket. Deterministic, no LLM.

The PDF's own text is untrusted DATA, exactly like Jira ticket content — it is
flattened to plain text here and quoted into the planning prompt as data,
never treated as instructions (security invariant 5). The uploaded file is
saved by the create-job route to a per-job path before this step runs; this
module never receives raw upload bytes over the network itself.
"""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

from pypdf import PdfReader

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData

logger = get_logger(__name__)

_PDF_MAGIC = b"%PDF-"


def requirement_document_path(job_id: str, upload_dir: Path) -> Path:
    return upload_dir / job_id / "requirement.pdf"


def _extract_text_from_reader(reader: PdfReader, max_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[... document truncated ...]"
    return combined


def extract_pdf_text(path: Path, max_chars: int) -> str:
    """Best-effort text extraction, capped to keep planning prompts bounded."""
    return _extract_text_from_reader(PdfReader(str(path)), max_chars)


def extract_pdf_text_from_bytes(content: bytes, max_chars: int) -> str:
    """Same extraction as extract_pdf_text, but from in-memory bytes rather
    than a saved path — used by the create-job route for best-effort ticket-
    key auto-detection (see detect_ticket_key below) before the upload is
    written to disk, since the background fetch_requirement_document step
    (the authoritative extraction) only runs later, in the job pipeline."""
    return _extract_text_from_reader(PdfReader(io.BytesIO(content)), max_chars)


# Matches a bare Jira issue key (e.g. KAN-31) inside a blob of free text —
# distinct from app.schemas.inputs.TICKET_KEY_RE, which anchors the *whole*
# value of the Jira-ticket form field rather than searching within text.
_TICKET_KEY_IN_TEXT_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
# Jira's own "Export to PDF" puts the issue key in the document's title/
# header, right at the top — restricting the scan to that region avoids
# picking up an unrelated key mentioned later in the description or a
# comment (e.g. "see also KAN-12" three paragraphs in).
_DETECT_TICKET_KEY_SCAN_CHARS = 500


def detect_ticket_key(text: str) -> str | None:
    """Best-effort: find a Jira-key-shaped token near the top of PDF text —
    most uploaded requirement PDFs are themselves exported from a Jira
    ticket. Returns None rather than guessing when nothing matches; the
    caller falls back to a deterministic hash-based synthetic key."""
    match = _TICKET_KEY_IN_TEXT_RE.search(text[:_DETECT_TICKET_KEY_SCAN_CHARS])
    return match.group(0) if match else None


async def fetch_requirement_document(job: Job, store: JobStore) -> TicketData:
    del store
    settings = get_settings()
    path = requirement_document_path(job.id, settings.document_upload_dir)
    if not path.is_file():
        raise AppError(
            ErrorCode.DOCUMENT_UNREADABLE, internal_detail="uploaded file missing on disk"
        )

    header = path.read_bytes()[: len(_PDF_MAGIC)]
    if header != _PDF_MAGIC:
        raise AppError(ErrorCode.DOCUMENT_NOT_PDF)

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(extract_pdf_text, path, settings.document_max_extracted_chars),
            timeout=settings.document_parse_timeout_seconds,
        )
    except TimeoutError as exc:
        raise AppError(
            ErrorCode.DOCUMENT_UNREADABLE,
            internal_detail=f"pdf parse timed out after {settings.document_parse_timeout_seconds}s",
        ) from exc
    except Exception as exc:  # pypdf raises assorted errors on malformed PDFs
        raise AppError(
            ErrorCode.DOCUMENT_UNREADABLE, internal_detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    if not text.strip():
        raise AppError(ErrorCode.DOCUMENT_EMPTY)

    name = job.requirement_document_name or "requirement.pdf"
    logger.info("extracted requirement document for job %s (%d chars)", job.id, len(text))
    return TicketData(
        key=job.ticket_key,
        summary=f"Requirement document: {name}",
        description=text,
        acceptance_criteria=None,
        comments=[],
    )
