"""The TEXT requirement source: a requirement pasted into the job form.

For teams whose tracker the app doesn't integrate with (Mantis, Azure DevOps,
email, Word, ...). Deterministic and LLM-free like the Jira and PDF steps, and
it produces the same TicketData shape, so planning never knows which source
was used. The text is untrusted data, quoted into the planning prompt inside
the same <ticket_data> block as Jira content (invariant 5).
"""

from __future__ import annotations

import hashlib

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.jobs.models import Job
from app.jobs.store import JobStore
from app.schemas.ticket import TicketData

logger = get_logger(__name__)

_SUMMARY_FROM_TEXT_MAX_CHARS = 120


def synthetic_text_key(title: str | None, text: str) -> str:
    """`TXT-` + 8 hex characters of a hash of the requirement, used when no
    real ticket key was typed. Deterministic, like the PDF source's `DOC-`
    fallback: the planning prompt embeds the key and the plan cache hashes the
    whole prompt, so a random key would make every resubmission a cache miss."""
    digest = hashlib.sha256(f"{title or ''}\n{text}".encode()).hexdigest()
    return f"TXT-{digest[:8].upper()}"


def _summary_from_text(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first_line) <= _SUMMARY_FROM_TEXT_MAX_CHARS:
        return first_line
    return first_line[: _SUMMARY_FROM_TEXT_MAX_CHARS - 1].rstrip() + "…"


async def fetch_requirement_text(job: Job, store: JobStore) -> TicketData:
    del store
    text = (job.requirement_text or "").strip()
    if not text:
        # The route refuses blank text, so this means a corrupted record.
        raise AppError(ErrorCode.TICKET_EMPTY, internal_detail="TEXT job has no requirement text")
    summary = job.requirement_title or _summary_from_text(text)
    logger.info("using pasted requirement for job %s (%d chars)", job.id, len(text))
    return TicketData(
        key=job.ticket_key,
        summary=summary,
        description=text,
        acceptance_criteria=None,
        comments=[],
    )
