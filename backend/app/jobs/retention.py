"""Workspace retention: delete finished jobs' clones and uploads after a while.

Every job gets a full clone under `workdir/<job_id>/` and, for a PDF-sourced
job, an upload under `document_upload_dir/<job_id>/`. Nothing else ever
deletes them, so on a shared server they fill the disk and keep client source
code around indefinitely. This sweep removes both directories once a job has
sat in a finished state (TERMINAL_STATES) untouched for
`workspace_retention_days`, and stamps `Job.workspace_removed_at`; the job
record itself (plan, diff, validation, cost) is kept. Jobs in any other state
are never touched.

Deletion is confined to the two configured roots: the target is always
`<root>/<job_id>` with `job_id` a single path component, and is refused if it
resolves (through a symlink or junction) anywhere but directly under the root.

Runs once at startup and then every `workspace_retention_interval_minutes`,
from the app lifespan via `workspace_retention()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.jobs.models import TERMINAL_STATES
from app.jobs.store import JobStore

logger = get_logger(__name__)

_RETAINED_STATES = tuple(state.value for state in TERMINAL_STATES)


def confined_job_dir(root: Path, job_id: str) -> Path | None:
    """`<root>/<job_id>`, resolved — or None if that would land anywhere but
    directly inside `root` (a separator or `..` in the id, or a symlink /
    junction pointing out of the root)."""
    if not job_id or job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
        return None
    if Path(job_id).name != job_id:  # e.g. a Windows drive like "C:"
        return None
    root_resolved = root.resolve()
    candidate = root_resolved / job_id
    if candidate.is_symlink() or candidate.is_junction():
        return None
    resolved = candidate.resolve()
    if os.path.normcase(str(resolved.parent)) != os.path.normcase(str(root_resolved)):
        return None
    return resolved


def _make_writable_and_retry(func: Callable[[str], object], path: str, exc: BaseException) -> None:
    # Git marks its object files read-only; on Windows that blocks deletion.
    if not isinstance(exc, PermissionError):
        raise exc
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def _delete_job_dirs(job_id: str, settings: Settings) -> None:
    """Delete the job's workspace and upload directories if present."""
    for root in (settings.workdir, settings.document_upload_dir):
        target = confined_job_dir(root, job_id)
        if target is None:
            logger.warning("retention: refusing to delete outside %s for job %s", root, job_id)
            continue
        if not target.exists():
            continue
        try:
            shutil.rmtree(target, onexc=_make_writable_and_retry)
        except OSError as exc:
            # Left for the next pass, which revisits already-marked jobs.
            logger.warning("retention: could not fully delete %s: %s", target, exc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def sweep_expired_workspaces(
    store: JobStore, settings: Settings, now: datetime | None = None
) -> list[str]:
    """One retention pass. Returns the ids of jobs newly marked as removed.

    A job is due once it is in a finished state and its last change
    (`updated_at`) is older than the retention period — so creating a branch
    or pushing restarts the clock. The mark is applied first, with a
    conditional update that fails if the job changed since it was listed, so
    a job someone just moved on is skipped; the directories are deleted
    after. Already-marked jobs are revisited so a deletion that failed half
    way (e.g. a file locked on Windows) is retried on the next pass.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.workspace_retention_days)
    newly_removed: list[str] = []
    for candidate in store.list_retention_candidates(_RETAINED_STATES):
        if not candidate.workspace_removed:
            updated_at = _parse_timestamp(candidate.updated_at)
            if updated_at is None or updated_at > cutoff:
                continue
            marked = store.mark_workspace_removed(
                candidate.job_id,
                states=_RETAINED_STATES,
                expected_updated_at=candidate.updated_at,
                removed_at=now,
            )
            if not marked:
                continue
            newly_removed.append(candidate.job_id)
        _delete_job_dirs(candidate.job_id, settings)
    if newly_removed:
        logger.info(
            "retention: removed workspaces of %d finished job(s) older than %s days",
            len(newly_removed),
            settings.workspace_retention_days,
        )
    return newly_removed


async def _run_periodically(store: JobStore, settings: Settings) -> None:
    interval = settings.workspace_retention_interval_minutes * 60
    while True:
        try:
            await asyncio.to_thread(sweep_expired_workspaces, store, settings)
        except Exception:
            # A failed pass must never kill the loop; the next one retries.
            logger.exception("retention: sweep failed")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def workspace_retention(store: JobStore, settings: Settings) -> AsyncGenerator[None]:
    """Run the retention sweep in the background for the app's lifetime (a
    first pass immediately, then on the configured interval). A no-op when
    WORKSPACE_RETENTION_ENABLED is false."""
    if not settings.workspace_retention_enabled:
        yield
        return
    task = asyncio.create_task(_run_periodically(store, settings))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
