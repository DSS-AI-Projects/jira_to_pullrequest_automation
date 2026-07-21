from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.jobs.store import JobStore
from app.main import create_app
from tests.fakes import make_fake_steps


@pytest.fixture
def auth_store() -> JobStore:
    return JobStore(":memory:")


@pytest.fixture
def auth_client(
    auth_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
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
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    logout = auth_client.post("/api/auth/logout")
    assert logout.status_code == 200
    login(auth_client, email="other@example.com", display_name="Other")

    read = auth_client.get(f"/api/jobs/{job_id}")
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "FORBIDDEN"


def test_logout_revokes_session(auth_client: TestClient) -> None:
    login(auth_client)
    logout = auth_client.post("/api/auth/logout")
    assert logout.status_code == 200

    response = auth_client.get("/api/repos")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
