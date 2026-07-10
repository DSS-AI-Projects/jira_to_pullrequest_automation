"""SQLite-backed job store.

Jobs are stored as JSON documents keyed by id (with the state mirrored into a
column for inspection). Writes are sub-millisecond on a local file, so methods
are synchronous and safe to call from the event loop for this single-user app.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.jobs.models import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class JobStore:
    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def create(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, state, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.state.value, job.model_dump_json(), job.created_at.isoformat()),
            )
            self._conn.commit()

    def save(self, job: Job) -> Job:
        job.updated_at = datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state = ?, data = ? WHERE id = ?",
                (job.state.value, job.model_dump_json(), job.id),
            )
            self._conn.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
