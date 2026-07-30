"""API contract + security invariant 1: no credential field, ever."""

import dataclasses
import io
import logging
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.core.logging import RedactionFilter
from app.jobs.models import (
    AgentUsage,
    ImplementationChange,
    ImplementationResult,
    Job,
    RepoInfo,
    RepoSourceKind,
)
from app.jobs.runner import CloneResult, ImplementationStepResult
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


@pytest.fixture
def local_client(store: JobStore) -> Iterator[TestClient]:
    def init_git_workspace(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text("Hello AI Agentic World\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(path), "branch", "-M", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "add", "README.md"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-m", "Initial commit"],
            check=True,
            capture_output=True,
            text=True,
        )

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch=ticket_key,
                commit_sha="e" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_plan(job: Job, workspace_path: Path) -> ImplementationStepResult:
        del job
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md",
                        action="modify",
                        rationale="Update the greeting text to match the approved plan.",
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(
                input_tokens=120,
                output_tokens=80,
                total_cost_usd=0.02,
                num_turns=2,
                duration_seconds=1.2,
            ),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_plan,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def poll_until_terminal(
    client: TestClient,
    job_id: str,
    timeout: float = 5.0,
    terminal_states: tuple[str, ...] = ("PLAN_READY", "FAILED"),
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["state"] in terminal_states:
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
    repo_info = cast(dict[str, object], body["repo_info"])
    assert repo_info is not None
    assert repo_info["branch"] == "main"
    plan = body["plan"]
    assert isinstance(plan, dict)
    assert plan["schema_version"] == 1


def test_unknown_job_returns_typed_404(client: TestClient) -> None:
    response = client.get("/api/jobs/doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_implement_unknown_job_returns_typed_404(client: TestClient) -> None:
    response = client.post("/api/jobs/doesnotexist/implement")
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
    assert "local_repo_support" in body
    assert "enabled" in body["local_repo_support"]


def test_plan_ready_local_job_can_be_approved_for_implementation(
    local_client: TestClient,
) -> None:
    response = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(local_client, job_id)
    assert body["state"] == "PLAN_READY"

    implement = local_client.post(f"/api/jobs/{job_id}/implement")
    assert implement.status_code == 202
    assert implement.json()["job_id"] == job_id

    final = poll_until_terminal(
        local_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert final["state"] == "IMPLEMENTATION_READY"
    assert final["implementation_result"] is not None
    assert final["implementation_diff"] is not None
    assert "README.md" in final["implementation_diff"]["overall_patch"]
    assert final["implementation_diff"]["files"][0]["path"] == "README.md"
    assert final["implementation_usage"] is not None
    assert final["validation_results"] is not None


def test_implement_persists_clarifications_and_surfaces_them_on_the_job(
    local_client: TestClient,
) -> None:
    create = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)

    implement = local_client.post(
        f"/api/jobs/{job_id}/implement",
        json={"clarifications": "  Prefer British spelling for the greeting.  "},
    )
    assert implement.status_code == 202

    final = poll_until_terminal(
        local_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert final["state"] == "IMPLEMENTATION_READY"
    assert final["implementation_clarifications"] == "Prefer British spelling for the greeting."


def test_implement_blank_clarifications_normalize_to_none(local_client: TestClient) -> None:
    create = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)

    implement = local_client.post(f"/api/jobs/{job_id}/implement", json={"clarifications": "   "})
    assert implement.status_code == 202

    final = poll_until_terminal(
        local_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert final["implementation_clarifications"] is None


def test_implement_omitted_body_stays_backward_compatible(local_client: TestClient) -> None:
    create = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)

    implement = local_client.post(f"/api/jobs/{job_id}/implement")
    assert implement.status_code == 202

    final = poll_until_terminal(
        local_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert final["state"] == "IMPLEMENTATION_READY"
    assert final["implementation_clarifications"] is None


def test_implement_rejects_credential_shaped_clarifications_never_echoed(
    local_client: TestClient,
) -> None:
    create = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)

    response = local_client.post(
        f"/api/jobs/{job_id}/implement", json={"clarifications": FAKE_TOKEN}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"
    assert FAKE_TOKEN not in response.text

    # rejected before the job was ever moved out of PLAN_READY
    unchanged = local_client.get(f"/api/jobs/{job_id}").json()
    assert unchanged["state"] == "PLAN_READY"
    assert unchanged["implementation_clarifications"] is None


def test_implement_rejects_overlong_clarifications(local_client: TestClient) -> None:
    create = local_client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)

    response = local_client.post(
        f"/api/jobs/{job_id}/implement", json={"clarifications": "x" * 4001}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_implement_rejects_job_not_in_plan_ready_state(client: TestClient, store: JobStore) -> None:
    job = Job.new(ticket_key="PROJ-1", repo_url="git@github.com:acme/repo.git")
    store.create(job)

    response = client.post(f"/api/jobs/{job.id}/implement")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMPLEMENTATION_NOT_READY"


def test_implement_rejects_remote_repo_jobs(client: TestClient) -> None:
    create = client.post(
        "/api/jobs",
        json={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["state"] == "PLAN_READY"

    response = client.post(f"/api/jobs/{job_id}/implement")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMPLEMENTATION_NOT_SUPPORTED"


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
