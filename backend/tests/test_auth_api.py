import time
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth.models import RepoHostingAuthKind, RepoHostingConnection, RepoHostingProvider
from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.jobs.models import AgentUsage, Job
from app.jobs.store import JobStore
from app.main import create_app
from tests.fakes import make_fake_steps


@pytest.fixture
def auth_store() -> JobStore:
    return JobStore(":memory:")


@pytest.fixture
def auth_client(auth_store: JobStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", '["admin@example.com"]')
    get_settings.cache_clear()
    app = create_app(store=auth_store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def trusted_proxy_client(
    auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "false")
    monkeypatch.setenv("AUTH_TRUSTED_PROXY_ENABLED", "true")
    monkeypatch.setenv("AUTH_TRUSTED_PROXY_SOURCES", '["testclient"]')
    get_settings.cache_clear()
    app = create_app(store=auth_store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def untrusted_proxy_client(
    auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "false")
    monkeypatch.setenv("AUTH_TRUSTED_PROXY_ENABLED", "true")
    monkeypatch.setenv("AUTH_TRUSTED_PROXY_SOURCES", '["203.0.113.10"]')
    get_settings.cache_clear()
    app = create_app(store=auth_store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    get_settings.cache_clear()


def login(
    client: TestClient, *, email: str = "sam@example.com", display_name: str = "Sam"
) -> dict[str, Any]:
    response = client.post(
        "/api/auth/dev-login",
        json={"email": email, "display_name": display_name},
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def test_session_reports_auth_enabled_when_configured(auth_client: TestClient) -> None:
    response = auth_client.get("/api/auth/session")
    assert response.status_code == 200
    assert response.json() == {
        "auth_enabled": True,
        "can_dev_login": True,
        "user": None,
    }


def test_trusted_proxy_headers_are_ignored_when_proxy_mode_disabled(
    auth_client: TestClient,
) -> None:
    response = auth_client.get(
        "/api/auth/session",
        headers={"X-Auth-Request-Email": "proxy@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["user"] is None


def test_trusted_proxy_bootstraps_session_from_allowed_source(
    trusted_proxy_client: TestClient,
) -> None:
    response = trusted_proxy_client.get(
        "/api/auth/session",
        headers={
            "X-Auth-Request-Email": "proxy@example.com",
            "X-Auth-Request-Name": "Proxy User",
            "X-Auth-Request-User": "proxy-subject",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "auth_enabled": True,
        "can_dev_login": False,
        "user": {
            "id": response.json()["user"]["id"],
            "email": "proxy@example.com",
            "display_name": "Proxy User",
            "role": "USER",
        },
    }

    repos = trusted_proxy_client.get("/api/repos")
    assert repos.status_code == 200


def test_trusted_proxy_headers_are_rejected_from_untrusted_source(
    untrusted_proxy_client: TestClient,
) -> None:
    response = untrusted_proxy_client.get(
        "/api/auth/session",
        headers={"X-Auth-Request-Email": "proxy@example.com"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "auth_enabled": True,
        "can_dev_login": False,
        "user": None,
    }

    repos = untrusted_proxy_client.get(
        "/api/repos",
        headers={"X-Auth-Request-Email": "proxy@example.com"},
    )
    assert repos.status_code == 401
    assert repos.json()["error"]["code"] == "UNAUTHENTICATED"


def test_unauthenticated_job_routes_are_rejected(auth_client: TestClient) -> None:
    response = auth_client.get("/api/repos")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_authenticated_user_can_create_and_read_own_job(
    auth_client: TestClient, auth_store: JobStore
) -> None:
    session = login(auth_client)
    create = auth_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    job = auth_store.get(job_id)
    assert job is not None
    user = cast(dict[str, Any], session["user"])
    assert job.owner_user_id == user["id"]

    read = auth_client.get(f"/api/jobs/{job_id}")
    assert read.status_code == 200
    assert read.json()["id"] == job_id


def test_user_cannot_access_another_users_job(auth_client: TestClient) -> None:
    login(auth_client, email="owner@example.com", display_name="Owner")
    create = auth_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    logout = auth_client.post("/api/auth/logout")
    assert logout.status_code == 200
    login(auth_client, email="other@example.com", display_name="Other")

    read = auth_client.get(f"/api/jobs/{job_id}")
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "FORBIDDEN"


def test_list_jobs_requires_authentication(auth_client: TestClient) -> None:
    response = auth_client.get("/api/jobs")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_list_jobs_scoped_to_owner_for_regular_user(auth_client: TestClient) -> None:
    login(auth_client, email="user-a@example.com", display_name="A")
    auth_client.post("/api/jobs", data={"ticket": "PROJ-1", "repo": "git@github.com:acme/repo.git"})
    auth_client.post("/api/auth/logout")

    login(auth_client, email="user-b@example.com", display_name="B")
    auth_client.post("/api/jobs", data={"ticket": "PROJ-2", "repo": "git@github.com:acme/repo.git"})

    response = auth_client.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert [job["ticket_key"] for job in body["jobs"]] == ["PROJ-2"]
    assert body["next_cursor"] is None


def test_admin_sees_every_users_jobs_in_list(auth_client: TestClient) -> None:
    login(auth_client, email="user-a@example.com", display_name="A")
    auth_client.post("/api/jobs", data={"ticket": "PROJ-1", "repo": "git@github.com:acme/repo.git"})
    auth_client.post("/api/auth/logout")

    login(auth_client, email="admin@example.com", display_name="Admin")
    auth_client.post("/api/jobs", data={"ticket": "PROJ-2", "repo": "git@github.com:acme/repo.git"})

    response = auth_client.get("/api/jobs")
    assert response.status_code == 200
    tickets = {job["ticket_key"] for job in response.json()["jobs"]}
    assert tickets == {"PROJ-1", "PROJ-2"}


def test_list_jobs_paginates_via_query_params(auth_client: TestClient) -> None:
    login(auth_client, email="user-c@example.com", display_name="C")
    for i in range(3):
        auth_client.post(
            "/api/jobs", data={"ticket": f"PROJ-{i}", "repo": "git@github.com:acme/repo.git"}
        )
        time.sleep(0.01)

    first = auth_client.get("/api/jobs", params={"limit": 2})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["jobs"]) == 2
    assert first_body["next_cursor"] is not None

    second = auth_client.get("/api/jobs", params={"limit": 2, "before": first_body["next_cursor"]})
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["jobs"]) == 1
    assert second_body["next_cursor"] is None

    seen = [job["ticket_key"] for job in (*first_body["jobs"], *second_body["jobs"])]
    assert sorted(seen) == ["PROJ-0", "PROJ-1", "PROJ-2"]


def test_cost_summary_requires_admin(auth_client: TestClient) -> None:
    login(auth_client, email="user@example.com", display_name="Regular User")
    response = auth_client.get("/api/admin/cost-summary")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_cost_summary_aggregates_and_resolves_user_identity(
    auth_client: TestClient, auth_store: JobStore
) -> None:
    session = login(auth_client, email="worker@example.com", display_name="Worker")
    user = cast(dict[str, Any], session["user"])

    job = Job.new(
        ticket_key="PROJ-1",
        repo_url="https://github.com/acme/repo",
        owner_user_id=cast(str, user["id"]),
    )
    job.usage = AgentUsage(duration_seconds=1.0, total_cost_usd=0.15)
    job.implementation_usage = AgentUsage(duration_seconds=2.0, total_cost_usd=0.35)
    auth_store.create(job)

    auth_client.post("/api/auth/logout")
    login(auth_client, email="admin@example.com", display_name="Admin")

    response = auth_client.get("/api/admin/cost-summary")
    assert response.status_code == 200
    body = response.json()

    worker_row = next(row for row in body["owners"] if row["user_id"] == user["id"])
    assert worker_row["email"] == "worker@example.com"
    assert worker_row["display_name"] == "Worker"
    assert worker_row["job_count"] == 1
    assert worker_row["planning_cost_usd"] == pytest.approx(0.15)
    assert worker_row["implementation_cost_usd"] == pytest.approx(0.35)
    assert worker_row["total_cost_usd"] == pytest.approx(0.50)
    assert body["grand_total_usd"] == pytest.approx(0.50)


def test_logout_revokes_session(auth_client: TestClient) -> None:
    login(auth_client)
    logout = auth_client.post("/api/auth/logout")
    assert logout.status_code == 200

    response = auth_client.get("/api/repos")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_jira_status_requires_authenticated_user(auth_client: TestClient) -> None:
    response = auth_client.get("/api/auth/jira")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_jira_status_reports_oauth_config_without_connection(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_OAUTH_ENABLED", "true")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("JIRA_OAUTH_CALLBACK_URL", "http://localhost:3000/api/auth/jira/callback")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("JIRA_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "server@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    get_settings.cache_clear()
    login(auth_client)

    response = auth_client.get("/api/auth/jira")

    assert response.status_code == 200
    assert response.json() == {
        "oauth_enabled": True,
        "oauth_configured": True,
        "shared_configured": True,
        "effective_mode": "SHARED",
        "connected": False,
        "connection": None,
    }


def test_jira_connect_returns_atlassian_authorization_url(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_OAUTH_ENABLED", "true")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("JIRA_OAUTH_CALLBACK_URL", "http://localhost:3000/api/auth/jira/callback")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("JIRA_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    login(auth_client)

    response = auth_client.post("/api/auth/jira/connect")

    assert response.status_code == 200
    authorization_url = response.json()["authorization_url"]
    parsed = urlparse(authorization_url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.atlassian.com"
    assert parsed.path == "/authorize"
    assert params["client_id"] == ["client-id"]
    assert params["audience"] == ["api.atlassian.com"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["read:jira-work offline_access"]
    assert params["state"]


@respx.mock
def test_jira_callback_persists_connection_and_disconnect_clears_it(
    auth_client: TestClient, auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_OAUTH_ENABLED", "true")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("JIRA_OAUTH_CALLBACK_URL", "http://testserver/api/auth/jira/callback")
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("JIRA_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    get_settings.cache_clear()
    session = login(auth_client)
    user = cast(dict[str, Any], session["user"])

    connect = auth_client.post("/api/auth/jira/connect")
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["authorization_url"]).query)["state"][0]

    respx.post("https://auth.atlassian.com/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "scope": "read:jira-work offline_access",
            },
        )
    )
    respx.get("https://api.atlassian.com/oauth/token/accessible-resources").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "cloud-123",
                    "name": "Acme Jira",
                    "url": "https://acme.atlassian.net",
                }
            ],
        )
    )

    callback = auth_client.get(f"/api/auth/jira/callback?code=test-code&state={state}")
    assert callback.status_code == 200
    assert callback.json()["connection"]["site"] == {
        "id": "cloud-123",
        "name": "Acme Jira",
        "url": "https://acme.atlassian.net",
    }

    stored = auth_store.get_jira_connection(user["id"])
    assert stored is not None
    assert stored.access_token_encrypted != "access-token"
    assert stored.refresh_token_encrypted != "refresh-token"

    status = auth_client.get("/api/auth/jira")
    assert status.status_code == 200
    assert status.json()["effective_mode"] == "DELEGATED"
    assert status.json()["connected"] is True

    disconnect = auth_client.delete("/api/auth/jira")
    assert disconnect.status_code == 200
    assert auth_store.get_jira_connection(user["id"]) is None


def test_repo_hosting_status_requires_authenticated_user(auth_client: TestClient) -> None:
    response = auth_client.get("/api/auth/repo-hosting")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_repo_hosting_status_reports_provider_configuration(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://localhost:3000/auth/github/callback")
    monkeypatch.setenv("GITLAB_OAUTH_ENABLED", "true")
    get_settings.cache_clear()
    login(auth_client)

    response = auth_client.get("/api/auth/repo-hosting")

    assert response.status_code == 200
    assert response.json() == {
        "providers": [
            {
                "provider": "GITHUB",
                "display_name": "GitHub",
                "enabled": True,
                "configured": True,
                "connected": False,
                "connection": None,
            },
            {
                "provider": "GITLAB",
                "display_name": "GitLab",
                "enabled": True,
                "configured": False,
                "connected": False,
                "connection": None,
            },
        ]
    }


def test_repo_hosting_disconnect_removes_stored_connection(
    auth_client: TestClient, auth_store: JobStore
) -> None:
    session = login(auth_client)
    user = cast(dict[str, Any], session["user"])
    auth_store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id=user["id"],
            provider=RepoHostingProvider.GITHUB,
            auth_kind=RepoHostingAuthKind.OAUTH_USER,
            account_name="octocat",
            account_id="12345",
            account_url="https://github.com/octocat",
            scopes=["repo", "read:user"],
        )
    )

    status = auth_client.get("/api/auth/repo-hosting")
    assert status.status_code == 200
    assert status.json()["providers"][0]["connected"] is True

    disconnect = auth_client.delete("/api/auth/repo-hosting/GITHUB")

    assert disconnect.status_code == 200
    assert auth_store.get_repo_hosting_connection(user["id"], RepoHostingProvider.GITHUB) is None


def test_github_repo_connect_returns_authorization_url(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://localhost:3000/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "github-secret")
    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    login(auth_client)

    response = auth_client.post("/api/auth/repo-hosting/github/connect")

    assert response.status_code == 200
    authorization_url = response.json()["authorization_url"]
    parsed = urlparse(authorization_url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert params["client_id"] == ["github-client"]
    assert params["redirect_uri"] == ["http://localhost:3000/auth/github/callback"]
    assert params["scope"] == ["repo read:user"]
    assert params["state"]


@respx.mock
def test_github_repo_callback_persists_connection(
    auth_client: TestClient, auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "github-secret")
    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    session = login(auth_client)
    user = cast(dict[str, Any], session["user"])

    connect = auth_client.post("/api/auth/repo-hosting/github/connect")
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["authorization_url"]).query)["state"][0]

    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "github-access-token",
                "scope": "repo,read:user",
                "token_type": "bearer",
            },
        )
    )
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "login": "octocat",
                "html_url": "https://github.com/octocat",
            },
        )
    )

    callback = auth_client.get(
        f"/api/auth/repo-hosting/github/callback?code=test-code&state={state}"
    )
    assert callback.status_code == 200
    assert callback.json()["connection"]["provider"] == "GITHUB"
    assert callback.json()["connection"]["account_name"] == "octocat"

    stored = auth_store.get_repo_hosting_connection(user["id"], RepoHostingProvider.GITHUB)
    assert stored is not None
    assert stored.account_name == "octocat"
    assert stored.access_token_encrypted is not None
    assert stored.access_token_encrypted != "github-access-token"


@respx.mock
def test_github_repo_listing_returns_connected_user_repositories(
    auth_client: TestClient, auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    session = login(auth_client)
    user = cast(dict[str, Any], session["user"])
    auth_store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id=user["id"],
            provider=RepoHostingProvider.GITHUB,
            auth_kind=RepoHostingAuthKind.OAUTH_USER,
            account_name="octocat",
            account_id="12345",
            account_url="https://github.com/octocat",
            scopes=["repo", "read:user"],
            access_token_encrypted=encrypt_secret("github-access-token", provider="github"),
        )
    )
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1001,
                    "name": "repo-one",
                    "full_name": "octocat/repo-one",
                    "html_url": "https://github.com/octocat/repo-one",
                    "clone_url": "https://github.com/octocat/repo-one.git",
                    "default_branch": "main",
                    "private": False,
                    "owner": {"login": "octocat"},
                }
            ],
        )
    )

    response = auth_client.get("/api/auth/repo-hosting/github/repos")

    assert response.status_code == 200
    assert response.json() == {
        "repos": [
            {
                "id": 1001,
                "name": "repo-one",
                "full_name": "octocat/repo-one",
                "html_url": "https://github.com/octocat/repo-one",
                "clone_url": "https://github.com/octocat/repo-one.git",
                "default_branch": "main",
                "owner_login": "octocat",
                "private": False,
            }
        ]
    }


def test_github_repo_listing_requires_connected_account(auth_client: TestClient) -> None:
    login(auth_client)

    response = auth_client.get("/api/auth/repo-hosting/github/repos")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REPO_PROVIDER_NOT_AVAILABLE"


def test_github_repo_listing_treats_undecryptable_token_as_not_connected(
    auth_client: TestClient, auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stored token encrypted under a key that no longer matches
    # GITHUB_OAUTH_ENCRYPTION_KEY (e.g. after a key rotation) must degrade to
    # the same "not connected" response the UI already knows how to show,
    # never a bare INTERNAL error.
    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()
    session = login(auth_client)
    user = cast(dict[str, Any], session["user"])
    auth_store.save_repo_hosting_connection(
        RepoHostingConnection.new(
            user_id=user["id"],
            provider=RepoHostingProvider.GITHUB,
            auth_kind=RepoHostingAuthKind.OAUTH_USER,
            account_name="octocat",
            account_id="12345",
            account_url="https://github.com/octocat",
            scopes=["repo", "read:user"],
            access_token_encrypted=encrypt_secret("github-access-token", provider="github"),
        )
    )

    monkeypatch.setenv("GITHUB_OAUTH_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    get_settings.cache_clear()

    response = auth_client.get("/api/auth/repo-hosting/github/repos")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "REPO_PROVIDER_NOT_AVAILABLE"
    assert body["error"]["message"] == "Connect your GitHub account before loading repositories."
