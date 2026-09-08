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

from app.core.config import Settings
from app.core.logging import RedactionFilter
from app.jobs.models import (
    AgentUsage,
    ImplementationChange,
    ImplementationResult,
    Job,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.runner import CloneResult, ImplementationStepResult
from app.jobs.store import JobStore
from app.main import create_app
from app.steps.branch_prep import create_branch as real_create_branch
from app.steps.branch_prep import push_branch as real_push_branch
from tests.fakes import make_fake_steps
from tests.pdf_fixtures import make_pdf_bytes

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

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job, validation_failures
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
        # local_client has a real git workspace on disk (init_git_workspace
        # above), so use the real branch-creation git plumbing too rather
        # than the fake's unconditional echo — several tests below rely on
        # it actually validating names and committing.
        create_branch=real_create_branch,
        push_branch=real_push_branch,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def local_client_with_pushable_remote(store: JobStore, tmp_path: Path) -> Iterator[TestClient]:
    """Same as local_client, but RepoInfo.origin_url points at a real, local
    bare repo (a plain filesystem path is a fully valid git remote — no test
    server needed), so push-branch tests can verify an actual push landed."""

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

    remote_path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote_path)], check=True, capture_output=True, text=True
    )

    async def local_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        del repo_url
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL,
                branch=ticket_key,
                commit_sha="e" * 40,
                origin_url=str(remote_path),
                is_dirty=False,
                local_path="D:\\repos\\repo",
            ),
        )

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job, validation_failures
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md", action="modify", rationale="Match the approved plan."
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=1.0),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_plan,
        create_branch=real_create_branch,
        push_branch=real_push_branch,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def remote_client(store: JobStore) -> Iterator[TestClient]:
    """Same as local_client, but the cloned workspace's RepoInfo reports
    REMOTE (no local_path) — implementation is no longer gated to local
    sources; the isolated workspace clone is identical either way."""

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

    async def remote_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.REMOTE,
                branch="main",
                commit_sha="e" * 40,
                origin_url=repo_url,
                is_dirty=False,
                local_path=None,
            ),
        )

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job, validation_failures
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
        clone_repo=remote_clone,
        implement_plan=implement_plan,
        create_branch=real_create_branch,
        push_branch=real_push_branch,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def local_client_npm_install_failing(store: JobStore) -> Iterator[TestClient]:
    """Same as local_client, but validation reports only a failed
    "npm install" step — an environment/dependency problem, not something an
    implementation-agent correction could ever fix — so the
    correct-validation endpoint should refuse to offer a fix."""

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

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job, validation_failures
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md", action="modify", rationale="Match the approved plan."
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=1.0),
        )

    async def validate_npm_install_failed(workspace_path: Path) -> list[ValidationResult]:
        del workspace_path
        return [
            ValidationResult(
                name="npm install",
                command="npm install",
                status=ValidationStatus.FAILED,
                summary="Validation command failed with exit code 1.",
                output_excerpt="npm ERR! network request to registry failed",
            )
        ]

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_plan,
        validate_workspace=validate_npm_install_failed,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def local_client_failing_validation(store: JobStore) -> Iterator[TestClient]:
    """Same as local_client, but validation reports FAILED on its first run
    and PASSED on its second — so the correct-validation endpoint has
    something real to fix and revalidate."""

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

    validate_calls: list[bool] = []

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        if validation_failures:
            (workspace_path / "fix.py").write_text("x = 1\n", encoding="utf-8")
            return ImplementationStepResult(
                result=ImplementationResult(
                    summary="Fixed the lint failure.",
                    changed_files=[
                        ImplementationChange(
                            path="fix.py", action="create", rationale="Fix the lint error."
                        )
                    ],
                    warnings=[],
                    follow_up_questions=[],
                ),
                usage=AgentUsage(duration_seconds=0.5),
            )
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md", action="modify", rationale="Match the approved plan."
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=1.0),
        )

    async def validate_once_failed_then_passed(workspace_path: Path) -> list[ValidationResult]:
        del workspace_path
        if not validate_calls:
            validate_calls.append(True)
            return [
                ValidationResult(
                    name="ruff",
                    command="ruff check .",
                    status=ValidationStatus.FAILED,
                    summary="1 lint error",
                    output_excerpt="fix.py:1: undefined name",
                )
            ]
        return [
            ValidationResult(
                name="ruff", command="ruff check .", status=ValidationStatus.PASSED, summary="ok"
            )
        ]

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=local_clone,
        implement_plan=implement_plan,
        validate_workspace=validate_once_failed_then_passed,
    )
    app = create_app(store=store, steps=steps)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def local_folder_client(store: JobStore) -> Iterator[TestClient]:
    """Same as local_client, but simulates a LOCAL_FOLDER source (a plain,
    non-git folder populated via ALLOW_LOCAL_NON_GIT_FOLDERS) — no branch."""

    def init_git_workspace(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text("Hello AI Agentic World\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True, text=True)
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
            ["git", "-C", str(path), "commit", "-m", "jira2pullreq: baseline snapshot"],
            check=True,
            capture_output=True,
            text=True,
        )

    async def folder_clone(
        job_id: str, ticket_key: str, repo_url: str, workdir: Path
    ) -> CloneResult:
        del ticket_key
        clone_path = workdir / job_id
        init_git_workspace(clone_path)
        return CloneResult(
            clone_path=clone_path,
            repo_info=RepoInfo(
                source_kind=RepoSourceKind.LOCAL_FOLDER,
                branch=None,
                commit_sha="f" * 40,
                origin_url=None,
                is_dirty=False,
                local_path=repo_url,
            ),
        )

    async def implement_plan(
        job: Job,
        workspace_path: Path,
        validation_failures: list[ValidationResult] | None = None,
    ) -> ImplementationStepResult:
        del job, validation_failures
        (workspace_path / "README.md").write_text("Hello Back To World\n", encoding="utf-8")
        return ImplementationStepResult(
            result=ImplementationResult(
                summary="Updated the README greeting.",
                changed_files=[
                    ImplementationChange(
                        path="README.md", action="modify", rationale="Match the approved plan."
                    )
                ],
                warnings=[],
                follow_up_questions=[],
            ),
            usage=AgentUsage(duration_seconds=1.0),
        )

    steps = dataclasses.replace(
        make_fake_steps(),
        clone_repo=folder_clone,
        implement_plan=implement_plan,
        create_branch=real_create_branch,
        push_branch=real_push_branch,
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
        "/api/jobs", data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"}
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
    assert plan["schema_version"] == 2


def test_submit_persists_planning_notes_and_surfaces_them_on_the_job(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/jobs",
        data={
            "ticket": "PROJ-123",
            "repo": "git@github.com:acme/repo.git",
            "planning_notes": "  Reuse the existing retry helper.  ",
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["state"] == "PLAN_READY"
    assert body["planning_notes"] == "Reuse the existing retry helper."


def test_submit_blank_planning_notes_normalizes_to_none(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        data={
            "ticket": "PROJ-123",
            "repo": "git@github.com:acme/repo.git",
            "planning_notes": "   ",
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["planning_notes"] is None


def test_submit_omitted_planning_notes_stays_backward_compatible(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"}
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["planning_notes"] is None


def test_submit_rejects_credential_shaped_planning_notes_never_echoed(
    client: TestClient, store: JobStore
) -> None:
    response = client.post(
        "/api/jobs",
        data={
            "ticket": "PROJ-123",
            "repo": "git@github.com:acme/repo.git",
            "planning_notes": FAKE_TOKEN,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"
    assert FAKE_TOKEN not in response.text
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        rows = store._conn.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()
    assert rows[0] == 0


def test_submit_rejects_overlong_planning_notes(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        data={
            "ticket": "PROJ-123",
            "repo": "git@github.com:acme/repo.git",
            "planning_notes": "x" * 4001,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_unknown_job_returns_typed_404(client: TestClient) -> None:
    response = client.get("/api/jobs/doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_implement_unknown_job_returns_typed_404(client: TestClient) -> None:
    response = client.post("/api/jobs/doesnotexist/implement")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_invalid_ticket_is_typed_400(client: TestClient) -> None:
    response = client.post("/api/jobs", data={"ticket": "garbage", "repo": "git@github.com:a/b"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_disallowed_repo_host_is_typed_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", data={"ticket": "PROJ-1", "repo": "https://evil.example.com/a/b"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REPO_HOST_NOT_ALLOWED"


# --- requirement document upload (alternative to a Jira ticket) ---


def test_submit_with_neither_ticket_nor_document_is_rejected(client: TestClient) -> None:
    response = client.post("/api/jobs", data={"repo": "git@github.com:acme/repo.git"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_submit_with_both_ticket_and_document_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        data={"ticket": "PROJ-1", "repo": "git@github.com:acme/repo.git"},
        files={"requirement_document": ("req.pdf", make_pdf_bytes("Some text"), "application/pdf")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"


def test_submit_with_non_pdf_upload_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        data={"repo": "git@github.com:acme/repo.git"},
        files={"requirement_document": ("req.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_PDF"


def test_submit_with_oversized_upload_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.get_settings",
        lambda: Settings(_env_file=None, document_max_upload_bytes=1),  # type: ignore[call-arg]
    )
    response = client.post(
        "/api/jobs",
        data={"repo": "git@github.com:acme/repo.git"},
        files={
            "requirement_document": (
                "req.pdf",
                make_pdf_bytes("more than one byte"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DOCUMENT_TOO_LARGE"


def test_submit_with_document_reaches_plan_ready_with_synthetic_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.api.routes.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    response = client.post(
        "/api/jobs",
        data={"repo": "git@github.com:acme/repo.git"},
        files={
            "requirement_document": (
                "requirements.pdf",
                make_pdf_bytes("The system shall support single sign-on."),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    body = poll_until_terminal(client, job_id)

    assert body["state"] == "PLAN_READY"
    assert body["ticket_key"].startswith("DOC-")
    assert (tmp_path / job_id / "requirement.pdf").exists()


def test_repos_endpoint_lists_choices_and_hosts(client: TestClient) -> None:
    body = client.get("/api/repos").json()
    assert "repos" in body
    assert "github.com" in body["allowed_hosts"]
    assert "local_repo_support" in body
    assert "enabled" in body["local_repo_support"]


def test_list_jobs_without_auth_returns_every_job(client: TestClient) -> None:
    client.post("/api/jobs", data={"ticket": "PROJ-1", "repo": "git@github.com:acme/repo.git"})
    client.post("/api/jobs", data={"ticket": "PROJ-2", "repo": "git@github.com:acme/repo.git"})

    response = client.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert {job["ticket_key"] for job in body["jobs"]} == {"PROJ-1", "PROJ-2"}
    assert body["next_cursor"] is None


def test_cost_summary_is_forbidden_without_auth(client: TestClient) -> None:
    response = client.get("/api/admin/cost-summary")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_plan_ready_local_job_can_be_approved_for_implementation(
    local_client: TestClient,
) -> None:
    response = local_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
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


def _poll_until_correction_attempted(
    client: TestClient, job_id: str, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["implementation_correction_attempted"]:
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished its validation correction attempt")


def test_correct_validation_fixes_and_revalidates(
    local_client_failing_validation: TestClient,
) -> None:
    create = local_client_failing_validation.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client_failing_validation, job_id)

    implement = local_client_failing_validation.post(f"/api/jobs/{job_id}/implement")
    assert implement.status_code == 202
    ready = poll_until_terminal(
        local_client_failing_validation,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert ready["state"] == "IMPLEMENTATION_READY"
    assert ready["validation_results"][0]["status"] == "FAILED"
    assert ready["implementation_correction_attempted"] is False

    correct = local_client_failing_validation.post(f"/api/jobs/{job_id}/correct-validation")
    assert correct.status_code == 202
    assert correct.json()["job_id"] == job_id

    final = _poll_until_correction_attempted(local_client_failing_validation, job_id)
    assert final["state"] == "IMPLEMENTATION_READY"
    assert final["implementation_correction_attempted"] is True
    assert final["implementation_correction_error"] is None
    assert final["implementation_correction_result"]["summary"] == "Fixed the lint failure."
    assert final["validation_results"][0]["status"] == "PASSED"
    paths = {f["path"] for f in final["implementation_diff"]["files"]}
    assert paths == {"README.md", "fix.py"}


def test_correct_validation_rejects_a_second_attempt(
    local_client_failing_validation: TestClient,
) -> None:
    create = local_client_failing_validation.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client_failing_validation, job_id)
    local_client_failing_validation.post(f"/api/jobs/{job_id}/implement")
    poll_until_terminal(
        local_client_failing_validation,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )

    first = local_client_failing_validation.post(f"/api/jobs/{job_id}/correct-validation")
    assert first.status_code == 202
    _poll_until_correction_attempted(local_client_failing_validation, job_id)

    second = local_client_failing_validation.post(f"/api/jobs/{job_id}/correct-validation")
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "VALIDATION_CORRECTION_NOT_AVAILABLE"


def test_correct_validation_rejects_when_nothing_failed(local_client: TestClient) -> None:
    """local_client's validation always reports SKIPPED — nothing to fix."""
    create = local_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client, job_id)
    local_client.post(f"/api/jobs/{job_id}/implement")
    poll_until_terminal(
        local_client, job_id, terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED")
    )

    response = local_client.post(f"/api/jobs/{job_id}/correct-validation")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_CORRECTION_NOT_AVAILABLE"


def test_correct_validation_rejects_when_only_npm_install_failed(
    local_client_npm_install_failing: TestClient,
) -> None:
    """A failed npm install is an environment problem, not something a
    source-code edit could fix — the endpoint must not offer a correction
    for it, even though the job does have a FAILED validation result."""
    create = local_client_npm_install_failing.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(local_client_npm_install_failing, job_id)
    local_client_npm_install_failing.post(f"/api/jobs/{job_id}/implement")
    ready = poll_until_terminal(
        local_client_npm_install_failing,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert ready["state"] == "IMPLEMENTATION_READY"
    assert ready["validation_results"][0]["status"] == "FAILED"

    response = local_client_npm_install_failing.post(f"/api/jobs/{job_id}/correct-validation")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_CORRECTION_NOT_AVAILABLE"


def test_correct_validation_rejects_job_not_implementation_ready(
    client: TestClient, store: JobStore
) -> None:
    job = Job.new(ticket_key="PROJ-1", repo_url="git@github.com:acme/repo.git")
    store.create(job)

    response = client.post(f"/api/jobs/{job.id}/correct-validation")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_CORRECTION_NOT_AVAILABLE"


def _reach_implementation_ready(client: TestClient) -> str:
    create = client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(client, job_id)
    client.post(f"/api/jobs/{job_id}/implement")
    ready = poll_until_terminal(
        client, job_id, terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED")
    )
    assert ready["state"] == "IMPLEMENTATION_READY"
    return job_id


def test_create_branch_commits_the_reviewed_diff(local_client: TestClient) -> None:
    job_id = _reach_implementation_ready(local_client)

    response = local_client.post(f"/api/jobs/{job_id}/create-branch")
    assert response.status_code == 200
    body = response.json()
    assert body["branch_name"] == "jira2pullreq/PROJ-123"
    assert body["commit_sha"]

    job = local_client.get(f"/api/jobs/{job_id}").json()
    assert job["branch_name"] == "jira2pullreq/PROJ-123"
    assert job["branch_commit_sha"] == body["commit_sha"]
    assert job["branch_created_at"] is not None


def test_create_branch_honors_a_custom_name_and_message(local_client: TestClient) -> None:
    job_id = _reach_implementation_ready(local_client)

    response = local_client.post(
        f"/api/jobs/{job_id}/create-branch",
        json={"branch_name": "custom/my-branch", "commit_message": "A custom message"},
    )
    assert response.status_code == 200
    assert response.json()["branch_name"] == "custom/my-branch"


def test_create_branch_rejects_a_second_attempt(local_client: TestClient) -> None:
    job_id = _reach_implementation_ready(local_client)

    first = local_client.post(f"/api/jobs/{job_id}/create-branch")
    assert first.status_code == 200

    second = local_client.post(f"/api/jobs/{job_id}/create-branch")
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "BRANCH_CREATION_NOT_AVAILABLE"


def test_create_branch_rejects_an_invalid_name_and_stays_retryable(
    local_client: TestClient,
) -> None:
    job_id = _reach_implementation_ready(local_client)

    bad = local_client.post(f"/api/jobs/{job_id}/create-branch", json={"branch_name": "bad..name"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "BRANCH_NAME_INVALID"

    job = local_client.get(f"/api/jobs/{job_id}").json()
    assert job["branch_name"] is None

    good = local_client.post(f"/api/jobs/{job_id}/create-branch")
    assert good.status_code == 200


def test_create_branch_rejects_job_not_implementation_ready(
    client: TestClient, store: JobStore
) -> None:
    job = Job.new(ticket_key="PROJ-1", repo_url="git@github.com:acme/repo.git")
    store.create(job)

    response = client.post(f"/api/jobs/{job.id}/create-branch")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BRANCH_CREATION_NOT_AVAILABLE"


def test_push_branch_pushes_to_the_real_remote(
    local_client_with_pushable_remote: TestClient,
) -> None:
    job_id = _reach_implementation_ready(local_client_with_pushable_remote)
    created = local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/create-branch")
    assert created.status_code == 200

    response = local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/push-branch")
    assert response.status_code == 200
    body = response.json()
    assert body["branch_name"] == "jira2pullreq/PROJ-123"
    assert body["remote_url"]
    assert body["compare_url"] is None  # a bare local path isn't a github.com URL

    job = local_client_with_pushable_remote.get(f"/api/jobs/{job_id}").json()
    assert job["branch_pushed_at"] is not None
    assert job["branch_push_remote_url"] == body["remote_url"]


def test_push_branch_is_idempotent_on_a_repeat_call(
    local_client_with_pushable_remote: TestClient,
) -> None:
    job_id = _reach_implementation_ready(local_client_with_pushable_remote)
    local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/create-branch")

    first = local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/push-branch")
    assert first.status_code == 200

    second = local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/push-branch")
    assert second.status_code == 200
    assert second.json() == first.json()


def test_push_branch_rejects_a_name_already_on_the_remote(
    local_client_with_pushable_remote: TestClient,
) -> None:
    job_id = _reach_implementation_ready(local_client_with_pushable_remote)
    local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/create-branch")
    local_client_with_pushable_remote.post(f"/api/jobs/{job_id}/push-branch")

    # A second, independent job aimed at the same branch name/remote.
    other_job_id = _reach_implementation_ready(local_client_with_pushable_remote)
    local_client_with_pushable_remote.post(
        f"/api/jobs/{other_job_id}/create-branch",
        json={"branch_name": "jira2pullreq/PROJ-123"},
    )

    response = local_client_with_pushable_remote.post(f"/api/jobs/{other_job_id}/push-branch")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BRANCH_PUSH_REJECTED"


def test_push_branch_rejects_when_no_branch_created_yet(local_client: TestClient) -> None:
    job_id = _reach_implementation_ready(local_client)

    response = local_client.post(f"/api/jobs/{job_id}/push-branch")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BRANCH_PUSH_NOT_AVAILABLE"


def test_push_branch_rejects_when_repo_has_no_remote(
    local_folder_client: TestClient,
) -> None:
    job_id = _reach_implementation_ready(local_folder_client)
    created = local_folder_client.post(f"/api/jobs/{job_id}/create-branch")
    assert created.status_code == 200

    response = local_folder_client.post(f"/api/jobs/{job_id}/push-branch")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BRANCH_PUSH_NOT_AVAILABLE"


def test_push_branch_rejects_job_not_found(client: TestClient) -> None:
    response = client.post("/api/jobs/does-not-exist/push-branch")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def testgithub_compare_url_handles_https_and_ssh_remotes() -> None:
    from app.api.routes import github_compare_url

    https_url = github_compare_url("https://github.com/acme/repo.git", "feature/x")
    assert https_url == "https://github.com/acme/repo/compare/feature/x?expand=1"

    ssh_url = github_compare_url("git@github.com:acme/repo.git", "feature/x")
    assert ssh_url == "https://github.com/acme/repo/compare/feature/x?expand=1"

    assert github_compare_url("https://gitlab.com/acme/repo.git", "feature/x") is None
    assert github_compare_url("/local/path/to/remote.git", "feature/x") is None


def test_implement_no_longer_rejects_remote_repo_jobs_by_source_kind(
    client: TestClient,
) -> None:
    """Remote-sourced jobs are no longer blocked at the implement gate merely
    for being remote (see test_implement_accepts_remote_repo_jobs_end_to_end
    for the full happy path). The plain `client` fixture's fake clone doesn't
    create a real workspace on disk, so it now fails for a different, more
    specific reason than IMPLEMENTATION_NOT_SUPPORTED."""
    create = client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    body = poll_until_terminal(client, job_id)
    assert body["state"] == "PLAN_READY"

    response = client.post(f"/api/jobs/{job_id}/implement")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMPLEMENTATION_WORKSPACE_MISSING"


def test_implement_accepts_remote_repo_jobs_end_to_end(remote_client: TestClient) -> None:
    create = remote_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/repo.git"},
    )
    job_id = create.json()["job_id"]
    poll_until_terminal(remote_client, job_id)

    implement = remote_client.post(f"/api/jobs/{job_id}/implement")
    assert implement.status_code == 202

    ready = poll_until_terminal(
        remote_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert ready["state"] == "IMPLEMENTATION_READY"
    assert ready["implementation_result"]["summary"] == "Updated the README greeting."
    assert ready["repo_info"]["source_kind"] == "REMOTE"


def test_implement_accepts_local_folder_repo_jobs(local_folder_client: TestClient) -> None:
    create = local_folder_client.post(
        "/api/jobs",
        data={"ticket": "PROJ-123", "repo": "git@github.com:acme/plain-folder.git"},
    )
    assert create.status_code == 202
    job_id = create.json()["job_id"]

    body = poll_until_terminal(local_folder_client, job_id)
    assert body["state"] == "PLAN_READY"
    repo_info = cast(dict[str, object], body["repo_info"])
    assert repo_info is not None
    assert repo_info["source_kind"] == "LOCAL_FOLDER"
    assert repo_info["branch"] is None

    implement = local_folder_client.post(f"/api/jobs/{job_id}/implement")
    assert implement.status_code == 202
    final = poll_until_terminal(
        local_folder_client,
        job_id,
        terminal_states=("IMPLEMENTATION_READY", "IMPLEMENTATION_FAILED"),
    )
    assert final["state"] == "IMPLEMENTATION_READY"


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
            data={
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
        "/api/jobs", data={"ticket": FAKE_TOKEN, "repo": "git@github.com:acme/repo.git"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INPUT_INVALID"
    assert FAKE_TOKEN not in response.text
