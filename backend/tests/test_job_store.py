import hashlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.auth.models import (
    JiraCloudSite,
    JiraConnection,
    RepoHostingAuthKind,
    RepoHostingConnection,
    RepoHostingProvider,
)
from app.jobs.models import AgentUsage, Job, JobState
from app.jobs.store import JobStore


def test_create_get_save_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    store.create(job)

    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.state == JobState.QUEUED
    assert loaded.ticket_key == "PROJ-1"

    loaded.state = JobState.PLANNING
    store.save(loaded)
    again = store.get(job.id)
    assert again is not None
    assert again.state == JobState.PLANNING
    assert again.updated_at >= again.created_at


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    assert store.get("nope") is None


def test_reads_a_job_persisted_before_plan_effort_estimates_existed(
    tmp_path: Path,
) -> None:
    """A job stored with a schema_version 1 plan (no estimated_story_points /
    complexity_level — those fields did not exist yet) must still load, not
    raise, when read back through the current Plan model."""
    store = JobStore(tmp_path / "jobs.db")
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    data = json.loads(job.model_dump_json())
    data["plan"] = {
        "schema_version": 1,
        "summary": "Do the thing.",
        "ticket_type": "feature",
        "impacted_files": [],
        "proposed_changes": [
            {"file": "a.py", "action": "modify", "description": "Apply the change."}
        ],
        "test_strategy": "Unit tests.",
        "risks": [],
        "open_questions": [],
    }
    store._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "INSERT INTO jobs (id, state, data, created_at, owner_user_id) VALUES (?, ?, ?, ?, ?)",
        (job.id, job.state.value, json.dumps(data), job.created_at.isoformat(), None),
    )
    store._conn.commit()  # pyright: ignore[reportPrivateUsage]

    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.plan is not None
    assert loaded.plan.schema_version == 1
    assert loaded.plan.estimated_story_points is None
    assert loaded.plan.complexity_level is None


def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job = Job.new(ticket_key="PROJ-2", repo_url="https://github.com/acme/repo")
    store.create(job)
    store.close()

    reopened = JobStore(db)
    loaded = reopened.get(job.id)
    assert loaded is not None
    assert loaded.ticket_key == "PROJ-2"


def test_jira_connection_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    connection = JiraConnection.new(
        user_id="user-1",
        site=JiraCloudSite(id="cloud-123", name="Acme", url="https://acme.atlassian.net"),
        scopes=["read:jira-work", "offline_access"],
        access_token_encrypted="encrypted-access",
        refresh_token_encrypted="encrypted-refresh",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store.save_jira_connection(connection)

    loaded = store.get_jira_connection("user-1")

    assert loaded is not None
    assert loaded.site.id == "cloud-123"
    assert loaded.refresh_token_encrypted == "encrypted-refresh"


def test_consume_jira_oauth_state_is_single_use(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    oauth_state = store.create_jira_oauth_state("user-1", ttl_minutes=10)

    consumed = store.consume_jira_oauth_state(oauth_state.state, "user-1")
    missing = store.consume_jira_oauth_state(oauth_state.state, "user-1")

    assert consumed is not None
    assert consumed.user_id == "user-1"
    assert missing is None


def test_repo_hosting_connection_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    connection = RepoHostingConnection.new(
        user_id="user-1",
        provider=RepoHostingProvider.GITHUB,
        auth_kind=RepoHostingAuthKind.OAUTH_USER,
        account_name="octocat",
        account_id="12345",
        account_url="https://github.com/octocat",
        scopes=["repo", "read:user"],
    )
    store.save_repo_hosting_connection(connection)

    loaded = store.get_repo_hosting_connection("user-1", RepoHostingProvider.GITHUB)

    assert loaded is not None
    assert loaded.account_name == "octocat"
    assert loaded.provider == RepoHostingProvider.GITHUB


def test_list_repo_hosting_connections_returns_all_saved_providers(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id="user-1",
            provider=RepoHostingProvider.GITHUB,
            auth_kind=RepoHostingAuthKind.OAUTH_USER,
            account_name="octocat",
            account_id="12345",
            account_url="https://github.com/octocat",
            scopes=["repo"],
        )
    )
    store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id="user-1",
            provider=RepoHostingProvider.GITLAB,
            auth_kind=RepoHostingAuthKind.APP_INSTALLATION,
            account_name="acme-group",
            account_id="999",
            account_url="https://gitlab.com/acme-group",
            scopes=["api"],
            installation_id="install-1",
        )
    )

    connections = store.list_repo_hosting_connections("user-1")

    assert [connection.provider for connection in connections] == [
        RepoHostingProvider.GITHUB,
        RepoHostingProvider.GITLAB,
    ]


def make_job(ticket_key: str, owner_user_id: str | None) -> Job:
    return Job.new(
        ticket_key=ticket_key,
        repo_url="https://github.com/acme/repo",
        owner_user_id=owner_user_id,
    )


def test_list_jobs_filters_by_owner(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.create(make_job("PROJ-1", "user-1"))
    store.create(make_job("PROJ-2", "user-2"))
    store.create(make_job("PROJ-3", "user-1"))

    jobs, next_cursor = store.list_jobs(owner_user_id="user-1", limit=20)

    assert {job.ticket_key for job in jobs} == {"PROJ-1", "PROJ-3"}
    assert next_cursor is None


def test_list_jobs_no_filter_returns_every_owner(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.create(make_job("PROJ-1", "user-1"))
    store.create(make_job("PROJ-2", "user-2"))
    store.create(make_job("PROJ-3", None))

    jobs, _ = store.list_jobs(owner_user_id=None, limit=20)

    assert {job.ticket_key for job in jobs} == {"PROJ-1", "PROJ-2", "PROJ-3"}


def test_list_jobs_orders_most_recent_first(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    for ticket in ("PROJ-1", "PROJ-2", "PROJ-3"):
        store.create(make_job(ticket, "user-1"))
        time.sleep(0.01)

    jobs, _ = store.list_jobs(owner_user_id="user-1", limit=20)

    assert [job.ticket_key for job in jobs] == ["PROJ-3", "PROJ-2", "PROJ-1"]


def test_list_jobs_paginates_without_gaps_or_duplicates(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    tickets = [f"PROJ-{i}" for i in range(5)]
    for ticket in tickets:
        store.create(make_job(ticket, "user-1"))
        time.sleep(0.01)

    first_page, cursor1 = store.list_jobs(owner_user_id="user-1", limit=2)
    assert len(first_page) == 2
    assert cursor1 is not None

    second_page, cursor2 = store.list_jobs(owner_user_id="user-1", limit=2, before=cursor1)
    assert len(second_page) == 2
    assert cursor2 is not None

    third_page, cursor3 = store.list_jobs(owner_user_id="user-1", limit=2, before=cursor2)
    assert len(third_page) == 1
    assert cursor3 is None

    seen = [job.ticket_key for job in (*first_page, *second_page, *third_page)]
    assert sorted(seen) == sorted(tickets)
    assert len(set(seen)) == len(seen)  # no duplicates across pages


def test_migration_backfills_owner_user_id_on_pre_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    # Simulate a database created before owner_user_id existed as a column.
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
        "data TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    legacy_job = make_job("PROJ-9", "user-legacy")
    raw.execute(
        "INSERT INTO jobs (id, state, data, created_at) VALUES (?, ?, ?, ?)",
        (
            legacy_job.id,
            legacy_job.state.value,
            legacy_job.model_dump_json(),
            legacy_job.created_at.isoformat(),
        ),
    )
    raw.commit()
    raw.close()

    store = JobStore(db_path)
    jobs, _ = store.list_jobs(owner_user_id="user-legacy", limit=20)

    assert [job.ticket_key for job in jobs] == ["PROJ-9"]


def _raw_session_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    raw = sqlite3.connect(str(db_path))
    try:
        return raw.execute("SELECT id, user_id, expires_at, data FROM sessions").fetchall()
    finally:
        raw.close()


def test_session_row_stores_only_a_hash_of_the_cookie_token(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)

    token = store.create_session("user-1", ttl_hours=12)

    rows = _raw_session_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    # Nowhere in the row — key, columns, or JSON blob — is the cookie value.
    assert all(token not in column for column in rows[0])


def test_session_lookup_and_delete_work_by_cookie_token(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)
    token = store.create_session("user-1", ttl_hours=12)

    session = store.get_session(token)
    assert session is not None
    assert session.user_id == "user-1"

    # What a database reader sees is not a usable cookie.
    stored_hash = _raw_session_rows(db_path)[0][0]
    assert store.get_session(stored_hash) is None

    store.delete_session(token)
    assert store.get_session(token) is None
    assert _raw_session_rows(db_path) == []


def test_expired_session_is_rejected_and_removed(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)
    token = store.create_session("user-1", ttl_hours=0)

    assert store.get_session(token) is None
    assert _raw_session_rows(db_path) == []


def test_migration_signs_out_plaintext_sessions_once(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    # A database from before session tokens were hashed: the row's key is
    # the cookie value itself (a uuid4 hex).
    legacy_token = uuid.uuid4().hex
    now = datetime.now(UTC)
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, data TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO sessions (id, user_id, expires_at, data) VALUES (?, ?, ?, ?)",
        (
            legacy_token,
            "user-legacy",
            (now + timedelta(hours=12)).isoformat(),
            json.dumps(
                {
                    "id": legacy_token,
                    "user_id": "user-legacy",
                    "expires_at": (now + timedelta(hours=12)).isoformat(),
                    "created_at": now.isoformat(),
                }
            ),
        ),
    )
    raw.commit()
    raw.close()

    store = JobStore(db_path)
    assert _raw_session_rows(db_path) == []
    assert store.get_session(legacy_token) is None

    # Idempotent: reopening the upgraded DB keeps sessions made since.
    token = store.create_session("user-1", ttl_hours=12)
    reopened = JobStore(db_path)
    session = reopened.get_session(token)
    assert session is not None
    assert session.user_id == "user-1"


def test_cost_summary_by_owner_aggregates_and_handles_missing_costs(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")

    planned_only = make_job("PROJ-1", "user-1")
    planned_only.usage = AgentUsage(duration_seconds=1.0, total_cost_usd=0.10)
    store.create(planned_only)

    planned_and_implemented = make_job("PROJ-2", "user-1")
    planned_and_implemented.usage = AgentUsage(duration_seconds=1.0, total_cost_usd=0.20)
    planned_and_implemented.implementation_usage = AgentUsage(
        duration_seconds=2.0, total_cost_usd=0.50
    )
    # The optional automatic-fix pass records its own, separate cost.
    planned_and_implemented.implementation_correction_usage = AgentUsage(
        duration_seconds=1.5, total_cost_usd=0.07
    )
    store.create(planned_and_implemented)

    other_user = make_job("PROJ-3", "user-2")
    other_user.usage = AgentUsage(duration_seconds=1.0, total_cost_usd=0.05)
    store.create(other_user)

    never_planned = make_job("PROJ-4", "user-2")  # usage stays None
    store.create(never_planned)

    ownerless = make_job("PROJ-5", None)
    ownerless.usage = AgentUsage(duration_seconds=1.0, total_cost_usd=0.01)
    store.create(ownerless)

    rows = {row.owner_user_id: row for row in store.cost_summary_by_owner()}

    assert rows["user-1"].job_count == 2
    assert rows["user-1"].planning_cost_usd == pytest.approx(0.30)
    assert rows["user-1"].implementation_cost_usd == pytest.approx(0.50)
    assert rows["user-1"].correction_cost_usd == pytest.approx(0.07)

    assert rows["user-2"].job_count == 2
    assert rows["user-2"].planning_cost_usd == pytest.approx(0.05)
    assert rows["user-2"].implementation_cost_usd == pytest.approx(0.0)
    assert rows["user-2"].correction_cost_usd == pytest.approx(0.0)

    assert rows[None].job_count == 1
    assert rows[None].planning_cost_usd == pytest.approx(0.01)


def test_consume_provider_oauth_state_is_single_use(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    oauth_state = store.create_provider_oauth_state(
        "user-1", RepoHostingProvider.GITHUB, ttl_minutes=10
    )

    consumed = store.consume_provider_oauth_state(
        oauth_state.state, "user-1", RepoHostingProvider.GITHUB
    )
    missing = store.consume_provider_oauth_state(
        oauth_state.state, "user-1", RepoHostingProvider.GITHUB
    )

    assert consumed is not None
    assert consumed.user_id == "user-1"
    assert consumed.provider == RepoHostingProvider.GITHUB
    assert missing is None
