"""The MANTIS requirement source: a MantisBT issue read through its REST API
with the shared read-only account (owner-approved)."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job, RequirementSource
from app.steps.mantis_fetch import fetch_mantis_issue, issue_id_from_key
from tests.pdf_fixtures import make_pdf_bytes

BASE = "https://mantis.example.com"
ISSUE_URL = f"{BASE}/api/rest/issues/1234"
FAKE_MANTIS_TOKEN = "mantis-token-" + "q" * 24


def make_job(key: str = "MANTIS-1234") -> Job:
    return Job.new(
        ticket_key=key,
        repo_url="https://github.com/acme/repo",
        requirement_source=RequirementSource.MANTIS,
        requirement_url=f"{BASE}/view.php?id=1234",
    )


def use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values: dict[str, Any] = {"mantis_enabled": True, "mantis_base_url": BASE, **overrides}
    monkeypatch.setattr(
        "app.steps.mantis_fetch.get_settings",
        lambda: Settings(_env_file=None, **values),  # type: ignore[call-arg]
    )


@pytest.fixture(autouse=True)
def mantis_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MANTIS_API_TOKEN", FAKE_MANTIS_TOKEN)
    yield


def issue_body(**fields: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "id": 1234,
        "summary": "Export the report as CSV",
        "description": "Admins need a CSV export of the monthly report.",
        "steps_to_reproduce": "Open Reports, look for an Export button.",
        "additional_information": "Columns must match the on-screen table.",
        "notes": [
            {
                "text": "Also needed for the Q4 audit.",
                "reporter": {"name": "sam", "real_name": "Sam"},
            },
            {"text": "  ", "reporter": {"name": "ignored"}},
            {"text": "Use semicolons for EU locales.", "reporter": {"name": "kim"}},
        ],
    }
    issue.update(fields)
    return {"issues": [issue]}


@respx.mock
async def test_maps_a_mantis_issue_to_the_ticket_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    route = respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_body()))

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    # MantisBT takes the token as the bare Authorization value.
    assert route.calls.last.request.headers["Authorization"] == FAKE_MANTIS_TOKEN
    assert ticket.key == "MANTIS-1234"
    assert ticket.summary == "Export the report as CSV"
    assert ticket.description == (
        "Admins need a CSV export of the monthly report.\n\n"
        "Steps to reproduce:\nOpen Reports, look for an Export button."
    )
    assert ticket.acceptance_criteria == "Columns must match the on-screen table."
    assert ticket.comments == [
        "Sam: Also needed for the Q4 audit.",
        "kim: Use semicolons for EU locales.",
    ]
    assert ticket.attachments == []


@respx.mock
async def test_a_custom_key_prefix_still_fetches_the_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, mantis_key_prefix="BUGS")
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_body()))

    ticket = await fetch_mantis_issue(make_job("BUGS-1234"), None)  # type: ignore[arg-type]

    assert ticket.key == "BUGS-1234"
    assert issue_id_from_key("BUGS-1234") == 1234


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_rejected_token_is_mantis_auth_failed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_AUTH_FAILED
    assert FAKE_MANTIS_TOKEN not in str(excinfo.value.internal_detail)


@respx.mock
async def test_missing_issue_is_ticket_not_found_with_a_mantis_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.TICKET_NOT_FOUND
    assert "Mantis issue" in excinfo.value.user_message


@respx.mock
async def test_an_empty_issues_list_is_ticket_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json={"issues": []}))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.TICKET_NOT_FOUND


@respx.mock
async def test_an_issue_with_no_summary_or_description_is_ticket_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(
            200, json=issue_body(summary=" ", description=None, steps_to_reproduce="")
        )
    )

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.TICKET_EMPTY


@respx.mock
async def test_server_errors_and_network_failures_are_mantis_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_UNREACHABLE

    respx.get(ISSUE_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_UNREACHABLE


@pytest.mark.parametrize(
    "overrides",
    [
        {"mantis_enabled": False},
        {"mantis_base_url": None},
        # The token travels in a header: plain http would send it in the clear.
        {"mantis_base_url": "http://mantis.example.com"},
    ],
)
@respx.mock  # no routes: any request at all would fail the test
async def test_missing_or_unsafe_config_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    use_settings(monkeypatch, **overrides)

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_CONFIG_MISSING


@respx.mock  # no routes: any request at all would fail the test
async def test_a_missing_token_is_mantis_config_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_settings(monkeypatch)
    monkeypatch.delenv("MANTIS_API_TOKEN")
    monkeypatch.setattr("app.core.secrets._ENV_FILE", tmp_path / "missing.env")

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_CONFIG_MISSING


# --- PDF attachments (best-effort) ---


def attachments_body() -> dict[str, Any]:
    return issue_body(
        attachments=[
            {"id": 7, "filename": "spec.pdf", "size": 2000, "content_type": "application/pdf"},
            {"id": 8, "filename": "screenshot.png", "size": 2000, "content_type": "image/png"},
            {"id": 9, "filename": "fake.pdf", "size": 10, "content_type": "application/pdf"},
        ]
    )


def file_body(content: bytes) -> dict[str, Any]:
    return {"files": [{"id": 1, "content": base64.b64encode(content).decode()}]}


@respx.mock
async def test_pdf_attachments_are_extracted_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, mantis_attachment_fetch_enabled=True)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=attachments_body()))
    respx.get(f"{ISSUE_URL}/files/7").mock(
        return_value=httpx.Response(200, json=file_body(make_pdf_bytes("Export columns: A, B, C.")))
    )
    # Claims to be a PDF but isn't: skipped by the magic-byte check.
    respx.get(f"{ISSUE_URL}/files/9").mock(
        return_value=httpx.Response(200, json=file_body(b"not really a pdf"))
    )
    # No route for /files/8 (the PNG): downloading it would fail the test.

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert len(ticket.attachments) == 1
    assert ticket.attachments[0].startswith("spec.pdf:")
    assert "Export columns: A, B, C." in ticket.attachments[0]


@respx.mock
async def test_a_failing_attachment_never_fails_the_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, mantis_attachment_fetch_enabled=True)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=attachments_body()))
    respx.get(f"{ISSUE_URL}/files/7").mock(return_value=httpx.Response(500))
    respx.get(f"{ISSUE_URL}/files/9").mock(side_effect=httpx.ConnectError("refused"))

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert ticket.summary == "Export the report as CSV"
    assert ticket.attachments == []


@respx.mock
async def test_attachments_are_not_downloaded_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=attachments_body()))
    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert ticket.attachments == []
    # Only the issue itself: an attachment download attempt would be a second
    # call (its failure would be swallowed, so count calls instead).
    assert [str(call.request.url) for call in respx.calls] == [ISSUE_URL]


# --- settings ---


def test_settings_normalise_the_base_url_and_prefix() -> None:
    values: dict[str, Any] = {
        "mantis_base_url": " https://mantis.example.com/ ",
        "mantis_key_prefix": " bugs ",
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    assert settings.mantis_base_url == "https://mantis.example.com"
    assert settings.mantis_key_prefix == "BUGS"


@pytest.mark.parametrize("prefix", ["", "1BUGS", "B", "BU-GS", "BUGS!"])
def test_settings_reject_a_key_prefix_that_would_not_make_a_ticket_key(prefix: str) -> None:
    with pytest.raises(ValueError, match="MANTIS_KEY_PREFIX"):
        Settings(_env_file=None, mantis_key_prefix=prefix)  # type: ignore[call-arg]


# --- secrets ---


@respx.mock
async def test_the_token_never_reaches_logs_or_the_ticket(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_settings(monkeypatch, mantis_attachment_fetch_enabled=True)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=attachments_body()))
    respx.get(f"{ISSUE_URL}/files/7").mock(return_value=httpx.Response(403))
    respx.get(f"{ISSUE_URL}/files/9").mock(return_value=httpx.Response(403))

    with caplog.at_level(logging.DEBUG):
        ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert FAKE_MANTIS_TOKEN not in caplog.text
    assert FAKE_MANTIS_TOKEN not in ticket.model_dump_json()


@respx.mock
async def test_dispatch_routes_mantis_jobs_to_the_mantis_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.steps import fetch_requirement

    use_settings(monkeypatch)
    respx.get(ISSUE_URL).mock(return_value=httpx.Response(200, json=issue_body()))

    ticket = await fetch_requirement(make_job(), None)  # type: ignore[arg-type]

    assert ticket.summary == "Export the report as CSV"
