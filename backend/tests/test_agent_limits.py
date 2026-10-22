"""Load and spend limits: the agent-run concurrency cap and the per-user
rolling 24h budget (app/jobs/limits.py)."""

import asyncio
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.jobs.limits import AgentSlots, check_spend_limit, spend_status
from app.jobs.models import (
    AgentUsage,
    Job,
    JobState,
    RepoInfo,
    RepoSourceKind,
    ValidationResult,
    ValidationStatus,
)
from app.jobs.runner import (
    PlanResult,
    recover_interrupted_jobs,
    run_implementation,
    run_job,
    run_validation_correction,
)
from app.jobs.store import JobStore
from app.main import create_app
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData
from tests.fakes import make_fake_steps, sample_plan

OWNER = "user-1"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, workdir=tmp_path / "workdir", **overrides)  # type: ignore[call-arg]


def usage(cost: float) -> AgentUsage:
    return AgentUsage(total_cost_usd=cost, duration_seconds=1.0)


def add_job(
    store: JobStore,
    *,
    owner: str | None = OWNER,
    state: JobState = JobState.QUEUED,
    created_at: datetime | None = None,
) -> Job:
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo", owner_user_id=owner)
    if created_at is not None:
        job.created_at = created_at
    store.create(job)
    job.state = state
    store.save(job)
    return job


def add_implementable_job(
    store: JobStore, workspace: Path, state: JobState, owner: str = OWNER
) -> Job:
    workspace.mkdir(parents=True, exist_ok=True)
    job = add_job(store, owner=owner, state=state)
    job.plan = sample_plan()
    job.repo_info = RepoInfo(
        source_kind=RepoSourceKind.REMOTE, branch="main", commit_sha="a" * 40, is_dirty=False
    )
    job.workspace_path = str(workspace)
    job.validation_results = [
        ValidationResult(
            name="pytest", command="pytest", status=ValidationStatus.FAILED, summary="1 failed"
        )
    ]
    store.save(job)
    return job


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# --- Concurrency cap -------------------------------------------------------


async def test_slots_never_exceed_the_limit_and_start_in_queue_order() -> None:
    slots = AgentSlots(2)
    release = asyncio.Event()
    started: list[int] = []
    peak = 0

    async def run(index: int) -> None:
        nonlocal peak
        async with slots.hold(f"job-{index}"):
            started.append(index)
            peak = max(peak, slots.running)
            await release.wait()

    tasks = [asyncio.create_task(run(index)) for index in range(6)]
    await wait_for(lambda: slots.waiting == 4)
    assert started == [0, 1]
    assert slots.running == 2

    release.set()
    await asyncio.gather(*tasks)
    assert started == [0, 1, 2, 3, 4, 5]
    assert peak == 2
    assert (slots.running, slots.waiting) == (0, 0)


async def test_queued_plan_jobs_stay_queued_until_a_slot_frees(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    slots = AgentSlots(1)
    release = asyncio.Event()
    planning_now = 0
    planning_peak = 0

    async def generate_plan(
        ticket: TicketData, repo_map: RepoMap, clone_path: Path, planning_notes: str | None
    ) -> PlanResult:
        nonlocal planning_now, planning_peak
        planning_now += 1
        planning_peak = max(planning_peak, planning_now)
        await release.wait()
        planning_now -= 1
        return PlanResult(plan=sample_plan(), usage=usage(0.1))

    steps = dataclasses.replace(make_fake_steps(), generate_plan=generate_plan)
    settings = make_settings(tmp_path)
    jobs = [add_job(store) for _ in range(3)]
    tasks = [asyncio.create_task(run_job(job.id, store, settings, steps, slots)) for job in jobs]

    def state(job: Job) -> JobState | None:
        current = store.get(job.id)
        return current.state if current else None

    await wait_for(lambda: state(jobs[0]) == JobState.PLANNING)
    await wait_for(lambda: slots.waiting == 2)
    assert [state(job) for job in jobs[1:]] == [JobState.QUEUED, JobState.QUEUED]

    release.set()
    await asyncio.gather(*tasks)
    assert [state(job) for job in jobs] == [JobState.PLAN_READY] * 3
    assert planning_peak == 1
    assert slots.running == 0


async def test_a_failed_run_releases_its_slot(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    slots = AgentSlots(1)

    async def fetch_ticket(job: Job, store: JobStore) -> TicketData:
        raise AppError(ErrorCode.TICKET_NOT_FOUND)

    steps = dataclasses.replace(make_fake_steps(), fetch_ticket=fetch_ticket)
    settings = make_settings(tmp_path)
    jobs = [add_job(store) for _ in range(3)]
    await asyncio.gather(*(run_job(job.id, store, settings, steps, slots) for job in jobs))

    for job in jobs:
        current = store.get(job.id)
        assert current is not None
        assert current.state == JobState.FAILED
        assert current.error is not None
        assert current.error.code == ErrorCode.TICKET_NOT_FOUND
    assert (slots.running, slots.waiting) == (0, 0)


async def test_implementation_and_correction_share_the_cap_with_planning(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    slots = AgentSlots(1)
    release = asyncio.Event()

    async def generate_plan(
        ticket: TicketData, repo_map: RepoMap, clone_path: Path, planning_notes: str | None
    ) -> PlanResult:
        await release.wait()
        return PlanResult(plan=sample_plan(), usage=usage(0.1))

    steps = dataclasses.replace(make_fake_steps(), generate_plan=generate_plan)
    settings = make_settings(tmp_path)
    planning = add_job(store)
    implementing = add_implementable_job(
        store, tmp_path / "ws-impl", JobState.IMPLEMENTATION_QUEUED
    )
    correcting = add_implementable_job(store, tmp_path / "ws-fix", JobState.CORRECTING)

    plan_task = asyncio.create_task(run_job(planning.id, store, settings, steps, slots))
    await wait_for(lambda: slots.running == 1)
    other_tasks = [
        asyncio.create_task(run_implementation(implementing.id, store, settings, steps, slots)),
        asyncio.create_task(
            run_validation_correction(correcting.id, store, settings, steps, slots)
        ),
    ]
    await wait_for(lambda: slots.waiting == 2)
    waiting_impl = store.get(implementing.id)
    waiting_fix = store.get(correcting.id)
    assert waiting_impl is not None and waiting_impl.state == JobState.IMPLEMENTATION_QUEUED
    assert waiting_fix is not None and waiting_fix.state == JobState.CORRECTING
    assert waiting_fix.implementation_correction_started_at is None

    release.set()
    await asyncio.gather(plan_task, *other_tasks)
    done_impl = store.get(implementing.id)
    done_fix = store.get(correcting.id)
    assert done_impl is not None and done_impl.state == JobState.IMPLEMENTATION_READY
    assert done_fix is not None and done_fix.state == JobState.IMPLEMENTATION_READY
    assert done_fix.implementation_correction_attempted is True
    assert done_fix.implementation_correction_started_at is not None


def test_app_sizes_the_cap_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_AGENT_RUNS", "3")
    get_settings.cache_clear()
    app = create_app(store=JobStore(":memory:"), steps=make_fake_steps())
    assert isinstance(app.state.agent_slots, AgentSlots)
    assert app.state.agent_slots.limit == 3


def test_default_cap_is_two() -> None:
    assert Settings(_env_file=None).max_concurrent_agent_runs == 2  # type: ignore[call-arg]


async def test_recovery_of_a_correction_still_waiting_for_a_slot_keeps_the_attempt(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = add_implementable_job(store, tmp_path / "ws", JobState.CORRECTING)

    assert await recover_interrupted_jobs(store) == 1

    recovered = store.get(job.id)
    assert recovered is not None
    assert recovered.state == JobState.IMPLEMENTATION_READY
    assert recovered.implementation_correction_error is not None
    assert recovered.implementation_correction_error.code == ErrorCode.JOB_INTERRUPTED
    assert recovered.implementation_correction_attempted is False


async def test_recovery_of_waiting_plan_and_implementation_jobs_ends_terminal(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    queued = add_job(store)
    impl_queued = add_implementable_job(store, tmp_path / "ws", JobState.IMPLEMENTATION_QUEUED)

    assert await recover_interrupted_jobs(store) == 2

    plan_job = store.get(queued.id)
    impl_job = store.get(impl_queued.id)
    assert plan_job is not None and plan_job.state == JobState.FAILED
    assert impl_job is not None and impl_job.state == JobState.IMPLEMENTATION_FAILED


# --- Per-user spend limit --------------------------------------------------


def test_spend_limit_is_off_by_default(tmp_path: Path) -> None:
    store = JobStore(":memory:")
    job = add_job(store, state=JobState.PLAN_READY)
    job.usage = usage(500.0)
    store.save(job)

    settings = make_settings(tmp_path)
    assert settings.user_daily_budget_usd is None
    check_spend_limit(store, OWNER, settings)  # no raise


def test_spend_limit_applied_refused_and_reset(tmp_path: Path) -> None:
    store = JobStore(":memory:")
    now = datetime.now(UTC)
    settings = make_settings(tmp_path, user_daily_budget_usd=5.0)

    old = add_job(store, state=JobState.PLAN_READY, created_at=now - timedelta(hours=23))
    old.usage = usage(3.0)
    store.save(old)
    check_spend_limit(store, OWNER, settings, now=now)  # $3 of $5: allowed

    recent = add_job(store, state=JobState.IMPLEMENTATION_READY, created_at=now)
    recent.usage = usage(0.5)
    recent.implementation_started_at = now - timedelta(hours=1)
    recent.implementation_usage = usage(1.5)
    store.save(recent)

    with pytest.raises(AppError) as refused:
        check_spend_limit(store, OWNER, settings, now=now)
    assert refused.value.code == ErrorCode.BUDGET_LIMIT_REACHED
    assert refused.value.http_status == 429
    # $5.00 spent; the oldest $3 ages out 24h after it was spent.
    resets_at = old.created_at + timedelta(hours=24)
    assert "$5.00 of your $5.00" in refused.value.user_message
    assert resets_at.strftime("%Y-%m-%d %H:%M UTC") in refused.value.user_message

    status = spend_status(store, OWNER, 5.0, now=now)
    assert status.exceeded
    assert status.resets_at == resets_at

    # Just after the reset time only the recent $2 is in the window.
    later = resets_at + timedelta(minutes=1)
    check_spend_limit(store, OWNER, settings, now=later)
    assert spend_status(store, OWNER, 5.0, now=later).spent_usd == pytest.approx(2.0)


def test_spend_counts_only_this_owner_and_only_the_last_24_hours(tmp_path: Path) -> None:
    store = JobStore(":memory:")
    now = datetime.now(UTC)

    other = add_job(store, owner="someone-else", state=JobState.PLAN_READY)
    other.usage = usage(10.0)
    store.save(other)

    # Planned two days ago (out of the window), implemented and corrected
    # today (in it).
    mixed = add_job(store, state=JobState.IMPLEMENTATION_READY, created_at=now - timedelta(days=2))
    mixed.usage = usage(4.0)
    mixed.implementation_started_at = now - timedelta(hours=2)
    mixed.implementation_usage = usage(1.0)
    mixed.implementation_correction_started_at = now - timedelta(hours=1)
    mixed.implementation_correction_usage = usage(0.25)
    store.save(mixed)

    assert spend_status(store, OWNER, 2.0, now=now).spent_usd == pytest.approx(1.25)


def test_spend_limit_skips_jobs_without_an_owner(tmp_path: Path) -> None:
    store = JobStore(":memory:")
    job = add_job(store, owner=None, state=JobState.PLAN_READY)
    job.usage = usage(100.0)
    store.save(job)

    check_spend_limit(store, None, make_settings(tmp_path, user_daily_budget_usd=1.0))


async def test_queued_plan_run_is_refused_once_the_owner_is_over_budget(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    settings = make_settings(tmp_path, user_daily_budget_usd=1.0)
    spent = add_job(store, state=JobState.PLAN_READY)
    spent.usage = usage(1.0)
    store.save(spent)
    calls = 0

    async def generate_plan(
        ticket: TicketData, repo_map: RepoMap, clone_path: Path, planning_notes: str | None
    ) -> PlanResult:
        nonlocal calls
        calls += 1
        return PlanResult(plan=sample_plan(), usage=usage(0.1))

    steps = dataclasses.replace(make_fake_steps(), generate_plan=generate_plan)
    queued = add_job(store)
    await run_job(queued.id, store, settings, steps, AgentSlots(1))

    refused = store.get(queued.id)
    assert refused is not None
    assert refused.state == JobState.FAILED
    assert refused.error is not None
    assert refused.error.code == ErrorCode.BUDGET_LIMIT_REACHED
    assert refused.error.stage == JobState.QUEUED
    assert calls == 0


async def test_refused_correction_keeps_its_one_attempt(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    settings = make_settings(tmp_path, user_daily_budget_usd=1.0)
    job = add_implementable_job(store, tmp_path / "ws", JobState.CORRECTING)
    job.usage = usage(2.0)
    store.save(job)

    await run_validation_correction(job.id, store, settings, make_fake_steps(), AgentSlots(1))

    refused = store.get(job.id)
    assert refused is not None
    assert refused.state == JobState.IMPLEMENTATION_READY
    assert refused.implementation_correction_error is not None
    assert refused.implementation_correction_error.code == ErrorCode.BUDGET_LIMIT_REACHED
    assert refused.implementation_correction_attempted is False
    assert refused.implementation_correction_usage is None


# --- API -------------------------------------------------------------------


@pytest.fixture
def budget_store() -> JobStore:
    return JobStore(":memory:")


@pytest.fixture
def budget_client(budget_store: JobStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    monkeypatch.setenv("USER_DAILY_BUDGET_USD", "1.0")
    get_settings.cache_clear()
    app = create_app(store=budget_store, steps=make_fake_steps())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    get_settings.cache_clear()


def login(client: TestClient, email: str) -> str:
    response = client.post("/api/auth/dev-login", json={"email": email, "display_name": "U"})
    assert response.status_code == 200
    user_id = response.json()["user"]["id"]
    assert isinstance(user_id, str)
    return user_id


def assert_budget_refusal(response: Any) -> None:
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "BUDGET_LIMIT_REACHED"
    assert "UTC" in error["message"]


def test_api_refuses_new_plans_implementations_and_fixes_over_budget(
    budget_client: TestClient, budget_store: JobStore, tmp_path: Path
) -> None:
    user_id = login(budget_client, "spender@example.com")
    plan_ready = add_implementable_job(
        budget_store, tmp_path / "ws-1", JobState.PLAN_READY, owner=user_id
    )
    plan_ready.usage = usage(1.25)
    budget_store.save(plan_ready)
    fixable = add_implementable_job(
        budget_store, tmp_path / "ws-2", JobState.IMPLEMENTATION_READY, owner=user_id
    )

    assert_budget_refusal(
        budget_client.post(
            "/api/jobs", data={"ticket": "PROJ-1", "repo": "https://github.com/acme/repo"}
        )
    )
    assert_budget_refusal(budget_client.post(f"/api/jobs/{plan_ready.id}/implement"))
    assert_budget_refusal(budget_client.post(f"/api/jobs/{fixable.id}/correct-validation"))

    # Nothing moved: no job was created and both jobs kept their state.
    jobs = budget_client.get("/api/jobs").json()["jobs"]
    assert {job["id"] for job in jobs} == {plan_ready.id, fixable.id}
    assert budget_client.get(f"/api/jobs/{plan_ready.id}").json()["state"] == "PLAN_READY"
    after_fix = budget_client.get(f"/api/jobs/{fixable.id}").json()
    assert after_fix["state"] == "IMPLEMENTATION_READY"
    assert after_fix["implementation_correction_attempted"] is False


def test_api_lets_a_user_under_budget_start_a_plan(
    budget_client: TestClient, budget_store: JobStore
) -> None:
    # Someone else's spend doesn't count against this user.
    big_spender = add_job(budget_store, owner="someone-else", state=JobState.PLAN_READY)
    big_spender.usage = usage(50.0)
    budget_store.save(big_spender)
    login(budget_client, "frugal@example.com")

    response = budget_client.post(
        "/api/jobs", data={"ticket": "PROJ-2", "repo": "https://github.com/acme/repo"}
    )
    assert response.status_code == 202
