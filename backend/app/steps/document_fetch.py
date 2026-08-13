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


def extract_pdf_text(path: Path, max_chars: int) -> str:
    """Best-effort text extraction, capped to keep planning prompts bounded."""
    reader = PdfReader(str(path))
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
