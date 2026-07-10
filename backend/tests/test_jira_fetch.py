"""Jira fetch step: typed errors, lazy auth, ADF flattening, no token leaks."""

import io
import logging
from typing import Any

import httpx
import pytest
import respx

from app.core import secrets
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import RedactionFilter
from app.steps.jira_auth import get_jira_auth
from app.steps.jira_fetch import adf_to_text, fetch_ticket, split_acceptance_criteria

BASE = "https://acme.atlassian.net"
FAKE_TOKEN = "ATATT" + "3xM" + "c" * 27
ISSUE_URL = f"{BASE}/rest/api/3/issue/PROJ-1"


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", FAKE_TOKEN)
    get_settings.cache_clear()


def adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def issue_payload() -> dict[str, Any]:
    return {
        "key": "PROJ-1",
        "fields": {
            "summary": "Add a verbose flag",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Users want more output."}],
                    },
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Acceptance Criteria"}],
                    },
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Passing -v prints debug lines."}],
                    },
                ],
            },
            "comment": {
                "comments": [
                    {"author": {"displayName": "Sam"}, "body": adf("please also update docs")}
                ]
            },
        },
    }


@respx.mock
async def test_fetch_extracts_fields(jira_env: None) -> None:
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_payload()))
    ticket = await fetch_ticket("PROJ-1")
    assert ticket.key == "PROJ-1"
    assert ticket.summary == "Add a verbose flag"
    assert "Users want more output." in ticket.description
    assert ticket.acceptance_criteria is not None
    assert "Passing -v prints debug lines." in ticket.acceptance_criteria
    assert ticket.comments == ["Sam: please also update docs"]


@respx.mock
async def test_auth_failure_is_typed(jira_env: None) -> None:
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AppError) as excinfo:
        await fetch_ticket("PROJ-1")
    assert excinfo.value.code == ErrorCode.JIRA_AUTH_FAILED


@respx.mock
async def test_unknown_ticket_is_typed(jira_env: None) -> None:
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(AppError) as excinfo:
        await fetch_ticket("PROJ-1")
    assert excinfo.value.code == ErrorCode.TICKET_NOT_FOUND


@respx.mock
async def test_empty_ticket_is_typed(jira_env: None) -> None:
    payload: dict[str, Any] = {"key": "PROJ-1", "fields": {"summary": "", "description": None}}
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(AppError) as excinfo:
        await fetch_ticket("PROJ-1")
    assert excinfo.value.code == ErrorCode.TICKET_EMPTY


@respx.mock
async def test_network_failure_is_typed(jira_env: None) -> None:
    respx.get(ISSUE_URL).mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(AppError) as excinfo:
        await fetch_ticket("PROJ-1")
    assert excinfo.value.code == ErrorCode.JIRA_UNREACHABLE
    assert FAKE_TOKEN not in (excinfo.value.internal_detail or "")


async def test_missing_config_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(AppError) as excinfo:
        get_jira_auth(settings)
    assert excinfo.value.code == ErrorCode.JIRA_CONFIG_MISSING


def test_auth_registers_token_and_b64_with_redactor(
    jira_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = get_jira_auth(get_settings())
    assert FAKE_TOKEN in secrets.registered_secrets()
    encoded = auth.auth_header.removeprefix("Basic ")
    assert encoded in secrets.registered_secrets()


@respx.mock
async def test_fetch_never_logs_the_token(jira_env: None) -> None:
    """Invariant 2, exercised on the real step: capture all logs during a fetch
    (success and auth-failure paths) and assert the token appears nowhere."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_payload()))
        await fetch_ticket("PROJ-1")
        respx.get(ISSUE_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(AppError):
            await fetch_ticket("PROJ-1")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    encoded = get_jira_auth(get_settings()).auth_header
    assert encoded not in output


def test_adf_flattening_handles_lists_and_code() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "item one"}],
                            }
                        ],
                    }
                ],
            },
            {"type": "codeBlock", "content": [{"type": "text", "text": "x = 1"}]},
            {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@sam"}}]},
        ],
    }
    text = adf_to_text(doc)
    assert "item one" in text
    assert "x = 1" in text
    assert "@sam" in text


def test_split_acceptance_criteria_without_heading() -> None:
    description, acceptance = split_acceptance_criteria("just a description")
    assert description == "just a description"
    assert acceptance is None
