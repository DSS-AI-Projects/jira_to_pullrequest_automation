from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.auth.models import (
    JiraCloudSite,
    JiraConnection,
    RepoHostingAuthKind,
    RepoHostingConnection,
    RepoHostingProvider,
)
from app.jobs.models import Job, JobState
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
