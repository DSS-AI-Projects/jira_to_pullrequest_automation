"""API contract + security invariant 1: no credential field, ever."""

import io
import logging
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.logging import RedactionFilter
from app.jobs.store import JobStore
from app.main import create_app
from tests.fakes import make_fake_steps

FAKE_TOKEN = "ATATT" + "3xZ" + "b" * 27


@pytest.fixture
def store() -> JobStore:
    return JobStore(":memory:")


@pytest.fixture
def client(store: JobStore) -> Iterator[TestClient]:
    app = create_app(store=store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def poll_until_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["state"] in ("PLAN_READY", "FAILED"):
            return body
        time.sleep(0.02)
    raise AssertionError("job never reached a terminal state")


def test_submit_returns_job_id_and_job_reaches_plan_ready(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"}
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["state"] == "PLAN_READY"
    assert body["plan"] is not None
    plan = body["plan"]
    assert isinstance(plan, dict)
    assert plan["schema_version"] == 1


def test_unknown_job_returns_typed_404(client: TestClient) -> None:
    response = client.get("/api/jobs/doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_invalid_ticket_is_typed_400(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"ticket": "garbage", "repo": "git@github.com:a/b"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_disallowed_repo_host_is_typed_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"ticket": "PROJ-1", "repo": "https://evil.example.com/a/b"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REPO_HOST_NOT_ALLOWED"


def test_repos_endpoint_lists_choices_and_hosts(client: TestClient) -> None:
    body = client.get("/api/repos").json()
    assert "repos" in body
    assert "github.com" in body["allowed_hosts"]


def test_extra_credential_field_is_rejected_never_stored_never_logged(
    client: TestClient, store: JobStore
) -> None:
    """Security invariant 1 (backend): a token-bearing submit is refused outright;
    the value is not echoed, not persisted, and not logged."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = client.post(
            "/api/jobs",
            json={
                "ticket": "PROJ-123",
                "repo": "git@github.com:acme/repo.git",
                "jira_token": FAKE_TOKEN,
            },
        )
    finally:
        root.removeHandler(handler)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"
    assert FAKE_TOKEN not in response.text  # not echoed
    assert FAKE_TOKEN not in stream.getvalue()  # not logged
    # not persisted: no job row was created anywhere
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        rows = store._conn.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()
    assert rows[0] == 0


def test_credential_shaped_ticket_value_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"ticket": FAKE_TOKEN, "repo": "git@github.com:acme/repo.git"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"
    assert FAKE_TOKEN not in response.text
