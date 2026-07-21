"""Planning agent step: budgets, retry-once policy, and security invariant 3
(no secret ever reaches the agent boundary)."""

import asyncio
from pathlib import Path

import pytest

from app.core import secrets
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import AgentUsage
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData
from app.steps import plan_agent
from app.steps.plan_agent import (
    AgentRunOutcome,
    build_options,
    build_prompt,
    generate_plan,
    scrubbed_env,
)
from tests.fakes import sample_plan

FAKE_JIRA_TOKEN = "ATATT" + "3xQ" + "d" * 27
FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-" + "e" * 32


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC_KEY)
    monkeypatch.setenv("JIRA_API_TOKEN", FAKE_JIRA_TOKEN)
    secrets.register_secret(FAKE_JIRA_TOKEN)
    get_settings.cache_clear()


def ticket() -> TicketData:
    return TicketData(
        key="PROJ-1",
        summary="Add verbose flag",
        description="Users want more output.",
        comments=["Sam: also update docs"],
    )


def repo_map() -> RepoMap:
    return RepoMap(text="src/cli.py\n  fn main (line 1)", file_count=1)


def success_outcome() -> AgentRunOutcome:
    return AgentRunOutcome(
        subtype="success",
        structured_output=sample_plan().model_dump(mode="json"),
        usage={"input_tokens": 1000, "output_tokens": 200},
        total_cost_usd=0.05,
        num_turns=3,
        duration_ms=4200,
    )


def failure_outcome(subtype: str) -> AgentRunOutcome:
    return AgentRunOutcome(
        subtype=subtype,
        structured_output=None,
        usage=None,
        total_cost_usd=0.01,
        num_turns=1,
        duration_ms=1000,
    )


def install_fake_agent(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[AgentRunOutcome]
) -> list[str]:
    """Replace the harness execution with canned outcomes; capture prompts."""
    prompts: list[str] = []
    remaining = list(outcomes)

    async def fake_execute(prompt: str, options: object) -> AgentRunOutcome:
        prompts.append(prompt)
        return remaining.pop(0)

    monkeypatch.setattr(plan_agent, "execute_agent", fake_execute)
    return prompts


# --- invariant 3: the agent boundary ---


def test_scrubbed_env_excludes_all_secrets(agent_env: None) -> None:
    env = scrubbed_env(FAKE_ANTHROPIC_KEY)
    assert "JIRA_API_TOKEN" not in env
    assert FAKE_JIRA_TOKEN not in env.values()
    assert env["ANTHROPIC_API_KEY"] == FAKE_ANTHROPIC_KEY  # the harness's own key only


def test_options_and_prompt_carry_no_secret(agent_env: None, tmp_path: Path) -> None:
    options = build_options(tmp_path, FAKE_ANTHROPIC_KEY, get_settings())
    prompt = build_prompt(ticket(), repo_map())

    assert FAKE_JIRA_TOKEN not in prompt
    assert FAKE_ANTHROPIC_KEY not in prompt
    assert isinstance(options.system_prompt, str)
    assert FAKE_JIRA_TOKEN not in options.system_prompt
    assert FAKE_JIRA_TOKEN not in str(options.env)
    # agent sees only the local clone path — never a remote URL or credential
    assert options.cwd == str(tmp_path)


def test_agent_tools_are_read_only(agent_env: None, tmp_path: Path) -> None:
    options = build_options(tmp_path, FAKE_ANTHROPIC_KEY, get_settings())
    assert options.tools == ["Read", "Grep", "Glob"]
    assert "Bash" in options.disallowed_tools
    assert "WebSearch" in options.disallowed_tools


def test_budgets_are_configured(agent_env: None, tmp_path: Path) -> None:
    settings = get_settings()
    options = build_options(tmp_path, FAKE_ANTHROPIC_KEY, settings)
    assert options.max_turns == settings.agent_max_turns
    assert options.max_budget_usd == settings.agent_max_budget_usd
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"


def test_prompt_quotes_ticket_as_data(agent_env: None) -> None:
    prompt = build_prompt(ticket(), repo_map())
    assert "<ticket_data>" in prompt
    assert "untrusted data" in prompt
    assert "Add verbose flag" in prompt
    assert "src/cli.py" in prompt


# --- outcome handling ---


async def test_success_returns_plan_and_usage(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake_agent(monkeypatch, [success_outcome()])
    result = await generate_plan(ticket(), repo_map(), tmp_path)
    assert result.plan.schema_version == 1
    assert isinstance(result.usage, AgentUsage)
    assert result.usage.input_tokens == 1000
    assert result.usage.total_cost_usd == 0.05
    assert result.usage.duration_seconds == pytest.approx(4.2)


async def test_malformed_then_valid_retries_once(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = install_fake_agent(
        monkeypatch,
        [failure_outcome("error_max_structured_output_retries"), success_outcome()],
    )
    result = await generate_plan(ticket(), repo_map(), tmp_path)
    assert result.plan is not None
    assert len(prompts) == 2  # exactly one retry


async def test_malformed_twice_is_plan_invalid(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = install_fake_agent(
        monkeypatch,
        [
            failure_outcome("error_max_structured_output_retries"),
            failure_outcome("error_max_structured_output_retries"),
        ],
    )
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.PLAN_INVALID
    assert len(prompts) == 2  # never a third attempt


async def test_schema_violating_output_is_plan_invalid_after_retry(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = AgentRunOutcome(
        subtype="success",
        structured_output={"schema_version": 1, "summary": ""},  # fails validation
        usage=None,
        total_cost_usd=None,
        num_turns=1,
        duration_ms=500,
    )
    install_fake_agent(monkeypatch, [bad, bad])
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.PLAN_INVALID


async def test_max_turns_is_budget_exceeded(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake_agent(monkeypatch, [failure_outcome("error_max_turns")])
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.BUDGET_EXCEEDED


async def test_wall_clock_timeout_is_budget_exceeded(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "0")
    get_settings.cache_clear()

    async def slow_execute(prompt: str, options: object) -> AgentRunOutcome:
        await asyncio.sleep(5)
        return success_outcome()

    monkeypatch.setattr(plan_agent, "execute_agent", slow_execute)
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.BUDGET_EXCEEDED


async def test_missing_api_key_is_typed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # keep a developer's real .env out of this test
    monkeypatch.setattr(secrets, "_ENV_FILE", tmp_path / "missing.env")
    get_settings.cache_clear()
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.AGENT_CONFIG_MISSING


async def test_unexpected_harness_failure_is_internal_and_redacted(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = AgentRunOutcome(
        subtype="error_during_execution",
        structured_output=None,
        usage=None,
        total_cost_usd=None,
        num_turns=0,
        duration_ms=100,
        errors=[f"process failed with {FAKE_JIRA_TOKEN}"],
    )
    install_fake_agent(monkeypatch, [bad])
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert FAKE_JIRA_TOKEN not in (excinfo.value.internal_detail or "")


async def test_success_without_structured_output_is_typed_request_failure(
    agent_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = AgentRunOutcome(
        subtype="success",
        structured_output=None,
        usage=None,
        total_cost_usd=None,
        num_turns=0,
        duration_ms=100,
        api_error_status=400,
        errors=["Credit balance is too low"],
    )
    install_fake_agent(monkeypatch, [bad])
    with pytest.raises(AppError) as excinfo:
        await generate_plan(ticket(), repo_map(), tmp_path)
    assert excinfo.value.code == ErrorCode.AGENT_REQUEST_FAILED
    assert "Credit balance is too low" in (excinfo.value.internal_detail or "")
