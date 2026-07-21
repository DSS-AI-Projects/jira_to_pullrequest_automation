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

from app.auth.models import Session, User, UserRole
from app.jobs.models import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data TEXT NOT NULL
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
            self._conn.execute(_USERS_SCHEMA)
            self._conn.execute(_SESSIONS_SCHEMA)
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

    def upsert_user(
        self,
        *,
        email: str,
        display_name: str,
        auth_provider: str,
        provider_subject: str,
        role: UserRole = UserRole.USER,
    ) -> User:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM users WHERE email = ?",
                (email.lower(),),
            ).fetchone()
            if row:
                user = User.model_validate_json(row[0])
                user.display_name = display_name
                user.auth_provider = auth_provider
                user.provider_subject = provider_subject
                user.role = role
                user.last_login_at = datetime.now(UTC)
            else:
                user = User.new(
                    email=email.lower(),
                    display_name=display_name,
                    auth_provider=auth_provider,
                    provider_subject=provider_subject,
                    role=role,
                )
            self._conn.execute(
                """
                INSERT INTO users (id, email, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    user.id,
                    user.email,
                    user.model_dump_json(),
                    user.last_login_at.isoformat(),
                ),
            )
            self._conn.commit()
        return user

    def get_user(self, user_id: str) -> User | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.model_validate_json(row[0]) if row else None

    def create_session(self, user_id: str, ttl_hours: int) -> Session:
        session = Session.new(user_id=user_id, ttl_hours=ttl_hours)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, user_id, expires_at, data) VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    session.user_id,
                    session.expires_at.isoformat(),
                    session.model_dump_json(),
                ),
            )
            self._conn.commit()
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            session = Session.model_validate_json(row[0])
            if session.is_expired:
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self._conn.commit()
                return None
        return session

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
