"""SQLite-backed job store.

Jobs are stored as JSON documents keyed by id (with the state mirrored into a
column for inspection). Writes are sub-millisecond on a local file, so methods
are synchronous and safe to call from the event loop for this single-user app.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterable
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
    hash_session_token,
    new_session_token,
)
from app.jobs.models import TERMINAL_STATES, Job

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
    # The optional "Attempt automatic fix" pass — a separate agent run with
    # its own recorded cost (Job.implementation_correction_usage).
    correction_cost_usd: float = 0.0


@dataclass(frozen=True)
class RetentionCandidate:
    job_id: str
    state: str
    updated_at: str  # as stored (ISO 8601); the optimistic-concurrency token
    workspace_removed: bool


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
            self._migrate_plaintext_sessions()
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

    def _migrate_plaintext_sessions(self) -> None:
        """One-time sign-out for DBs from before session tokens were hashed.

        Old rows are keyed by the cookie value itself (a 32-char uuid hex);
        new rows by its SHA-256 hex digest (64 chars). Old rows can no longer
        match any cookie — lookups hash the cookie first — but they still
        hold usable-looking secrets, so they're deleted. Idempotent: on a
        fresh or already-migrated DB every row is 64 chars and nothing goes.
        """
        self._conn.execute("DELETE FROM sessions WHERE length(id) != 64")

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

    def list_retention_candidates(self, states: Iterable[str]) -> list[RetentionCandidate]:
        """Lightweight rows (no full JSON decode) for every job in `states` —
        the retention sweep's input. Already-swept jobs are included so a
        deletion that failed half-way is retried."""
        state_list = list(states)
        placeholders = ", ".join("?" for _ in state_list)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, state, json_extract(data, '$.updated_at'), "
                "json_extract(data, '$.workspace_removed_at') IS NOT NULL "
                f"FROM jobs WHERE state IN ({placeholders})",
                state_list,
            ).fetchall()
        return [
            RetentionCandidate(
                job_id=row[0], state=row[1], updated_at=row[2], workspace_removed=bool(row[3])
            )
            for row in rows
        ]

    def mark_workspace_removed(
        self, job_id: str, *, states: Iterable[str], expected_updated_at: str, removed_at: datetime
    ) -> bool:
        """Atomically stamp `workspace_removed_at`, but only if the job is
        still in one of `states` and unchanged since it was listed (its
        updated_at still equals `expected_updated_at`). A single conditional
        UPDATE rather than get()+save(), so a request that moved the job on
        in between is never overwritten by a stale copy. True if stamped."""
        state_list = list(states)
        placeholders = ", ".join("?" for _ in state_list)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE jobs SET data = json_set(data, '$.workspace_removed_at', ?) "
                f"WHERE id = ? AND state IN ({placeholders}) "
                "AND json_extract(data, '$.updated_at') = ? "
                "AND json_extract(data, '$.workspace_removed_at') IS NULL",
                (removed_at.isoformat(), job_id, *state_list, expected_updated_at),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def list_unfinished_jobs(self) -> list[Job]:
        """Every job not in a terminal state, oldest first. Used once at
        startup to find jobs a previous process was running when it stopped
        (see recover_interrupted_jobs in app/jobs/runner.py)."""
        terminal = sorted(state.value for state in TERMINAL_STATES)
        placeholders = ", ".join("?" for _ in terminal)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM jobs WHERE state NOT IN ({placeholders}) "
                "ORDER BY created_at ASC",
                terminal,
            ).fetchall()
        return [Job.model_validate_json(row[0]) for row in rows]

    def cost_summary_by_owner(self) -> list[OwnerCostRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    owner_user_id,
                    COUNT(*),
                    SUM(COALESCE(json_extract(data, '$.usage.total_cost_usd'), 0)),
                    SUM(COALESCE(json_extract(data, '$.implementation_usage.total_cost_usd'), 0)),
                    SUM(
                        COALESCE(
                            json_extract(data, '$.implementation_correction_usage.total_cost_usd'),
                            0
                        )
                    )
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
                correction_cost_usd=row[4] or 0.0,
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

    def create_session(self, user_id: str, ttl_hours: int) -> str:
        """Store a new session and return its cookie token. Only the token's
        hash is written; the token itself exists nowhere but in the return
        value (and then the browser's cookie)."""
        token = new_session_token()
        session = Session.new(token=token, user_id=user_id, ttl_hours=ttl_hours)
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
        return token

    def get_session(self, token: str) -> Session | None:
        """Look a session up by its cookie token (hashed before the query)."""
        session_hash = hash_session_token(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE id = ?",
                (session_hash,),
            ).fetchone()
            if row is None:
                return None
            session = Session.model_validate_json(row[0])
            if session.is_expired:
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_hash,))
                self._conn.commit()
                return None
        return session

    def delete_session(self, token: str) -> None:
        """Delete a session by its cookie token (hashed before the query)."""
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (hash_session_token(token),))
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
