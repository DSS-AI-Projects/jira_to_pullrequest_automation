"""SQLite-backed job store.

Jobs are stored as JSON documents keyed by id (with the state mirrored into a
column for inspection). Writes are sub-millisecond on a local file, so methods
are synchronous and safe to call from the event loop for this single-user app.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.auth.models import (
    JiraConnection,
    JiraOAuthState,
    ProviderOAuthState,
    RepoHostingConnection,
    RepoHostingProvider,
    Session,
    User,
    UserRole,
)
from app.jobs.models import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner_user_id TEXT
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

_JIRA_CONNECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jira_connections (
    user_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_JIRA_OAUTH_STATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS jira_oauth_states (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

_REPO_HOSTING_CONNECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_hosting_connections (
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, provider)
)
"""

_PROVIDER_OAUTH_STATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_oauth_states (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class OwnerCostRow:
    owner_user_id: str | None
    job_count: int
    planning_cost_usd: float
    implementation_cost_usd: float


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
            self._conn.execute(_JIRA_CONNECTIONS_SCHEMA)
            self._conn.execute(_JIRA_OAUTH_STATES_SCHEMA)
            self._conn.execute(_REPO_HOSTING_CONNECTIONS_SCHEMA)
            self._conn.execute(_PROVIDER_OAUTH_STATES_SCHEMA)
            self._migrate_owner_user_id()
            self._conn.commit()

    def _migrate_owner_user_id(self) -> None:
        """Additive migration for DBs created before owner_user_id existed.

        A no-op on a fresh DB (the column is already in _SCHEMA, so ADD COLUMN
        fails harmlessly with "duplicate column"). On an existing DB missing
        the column, adds it and backfills every row from its JSON blob so
        jobs created before this migration stay listable/attributable.
        """
        with contextlib.suppress(sqlite3.OperationalError):
            self._conn.execute("ALTER TABLE jobs ADD COLUMN owner_user_id TEXT")
        self._conn.execute(
            "UPDATE jobs SET owner_user_id = json_extract(data, '$.owner_user_id') "
            "WHERE owner_user_id IS NULL"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_owner_created ON jobs (owner_user_id, created_at)"
        )

    def create(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, state, data, created_at, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.state.value,
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.owner_user_id,
                ),
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

    def list_jobs(
        self, *, owner_user_id: str | None, limit: int, before: str | None = None
    ) -> tuple[list[Job], str | None]:
        """Most-recent-first keyset pagination. `owner_user_id=None` means no
        ownership filter (admin view, or auth-disabled single-tenant mode)."""
        conditions: list[str] = []
        params: list[str | int] = []
        if owner_user_id is not None:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        if before is not None:
            conditions.append("created_at < ?")
            params.append(before)

        query = "SELECT data, created_at FROM jobs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit + 1)  # fetch one extra to detect a next page

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        jobs = [Job.model_validate_json(row[0]) for row in page_rows]
        next_cursor = page_rows[-1][1] if has_more and page_rows else None
        return jobs, next_cursor

    def cost_summary_by_owner(self) -> list[OwnerCostRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    owner_user_id,
                    COUNT(*),
                    SUM(COALESCE(json_extract(data, '$.usage.total_cost_usd'), 0)),
                    SUM(COALESCE(json_extract(data, '$.implementation_usage.total_cost_usd'), 0))
                FROM jobs
                GROUP BY owner_user_id
                """
            ).fetchall()
        return [
            OwnerCostRow(
                owner_user_id=row[0],
                job_count=row[1],
                planning_cost_usd=row[2] or 0.0,
                implementation_cost_usd=row[3] or 0.0,
            )
            for row in rows
        ]

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

    def save_jira_connection(self, connection: JiraConnection) -> JiraConnection:
        connection.updated_at = datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jira_connections (user_id, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    connection.user_id,
                    connection.model_dump_json(),
                    connection.updated_at.isoformat(),
                ),
            )
            self._conn.commit()
        return connection

    def get_jira_connection(self, user_id: str) -> JiraConnection | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM jira_connections WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return JiraConnection.model_validate_json(row[0]) if row else None

    def delete_jira_connection(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jira_connections WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def create_jira_oauth_state(self, user_id: str, ttl_minutes: int) -> JiraOAuthState:
        oauth_state = JiraOAuthState.new(user_id=user_id, ttl_minutes=ttl_minutes)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jira_oauth_states (state, user_id, expires_at, data)
                VALUES (?, ?, ?, ?)
                """,
                (
                    oauth_state.state,
                    oauth_state.user_id,
                    oauth_state.expires_at.isoformat(),
                    oauth_state.model_dump_json(),
                ),
            )
            self._conn.commit()
        return oauth_state

    def consume_jira_oauth_state(self, state: str, user_id: str) -> JiraOAuthState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM jira_oauth_states WHERE state = ?",
                (state,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM jira_oauth_states WHERE state = ?", (state,))
            self._conn.commit()
        oauth_state = JiraOAuthState.model_validate_json(row[0])
        if oauth_state.user_id != user_id or oauth_state.is_expired:
            return None
        return oauth_state

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save_repo_hosting_connection(
        self, connection: RepoHostingConnection
    ) -> RepoHostingConnection:
        connection.updated_at = datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO repo_hosting_connections (user_id, provider, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    connection.user_id,
                    connection.provider.value,
                    connection.model_dump_json(),
                    connection.updated_at.isoformat(),
                ),
            )
            self._conn.commit()
        return connection

    def get_repo_hosting_connection(
        self, user_id: str, provider: RepoHostingProvider
    ) -> RepoHostingConnection | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM repo_hosting_connections WHERE user_id = ? AND provider = ?",
                (user_id, provider.value),
            ).fetchone()
        return RepoHostingConnection.model_validate_json(row[0]) if row else None

    def list_repo_hosting_connections(self, user_id: str) -> list[RepoHostingConnection]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM repo_hosting_connections WHERE user_id = ? ORDER BY provider",
                (user_id,),
            ).fetchall()
        return [RepoHostingConnection.model_validate_json(row[0]) for row in rows]

    def delete_repo_hosting_connection(self, user_id: str, provider: RepoHostingProvider) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM repo_hosting_connections WHERE user_id = ? AND provider = ?",
                (user_id, provider.value),
            )
            self._conn.commit()

    def create_provider_oauth_state(
        self, user_id: str, provider: RepoHostingProvider, ttl_minutes: int
    ) -> ProviderOAuthState:
        oauth_state = ProviderOAuthState.new(
            user_id=user_id, provider=provider, ttl_minutes=ttl_minutes
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_oauth_states (state, user_id, provider, expires_at, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    oauth_state.state,
                    oauth_state.user_id,
                    oauth_state.provider.value,
                    oauth_state.expires_at.isoformat(),
                    oauth_state.model_dump_json(),
                ),
            )
            self._conn.commit()
        return oauth_state

    def consume_provider_oauth_state(
        self, state: str, user_id: str, provider: RepoHostingProvider
    ) -> ProviderOAuthState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM provider_oauth_states WHERE state = ?",
                (state,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM provider_oauth_states WHERE state = ?", (state,))
            self._conn.commit()
        oauth_state = ProviderOAuthState.model_validate_json(row[0])
        if (
            oauth_state.user_id != user_id
            or oauth_state.provider != provider
            or oauth_state.is_expired
        ):
            return None
        return oauth_state
