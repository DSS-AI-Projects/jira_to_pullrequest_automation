"""The MANTIS requirement source: a MantisBT issue read through its REST API,
or its MantisConnect SOAP API, with the shared read-only account
(owner-approved)."""

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


# --- SOAP (MANTIS_API=soap) ---

SOAP_URL = f"{BASE}/api/soap/mantisconnect.php"
SOAP_USER = "jira2pullreq-reader"
FAKE_MANTIS_PASSWORD = "mantis-password-" + "z" * 20


def use_soap(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values: dict[str, Any] = {"mantis_api": "soap", "mantis_username": SOAP_USER, **overrides}
    use_settings(monkeypatch, **values)
    monkeypatch.setenv("MANTIS_PASSWORD", FAKE_MANTIS_PASSWORD)


def soap_reply(operation: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:ns1="http://futureware.biz/mantisconnect"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<SOAP-ENV:Body><ns1:{operation}Response>{inner}</ns1:{operation}Response>"
        "</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def soap_fault(message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<SOAP-ENV:Body><SOAP-ENV:Fault><faultcode>SOAP-ENV:Client</faultcode>"
        f"<faultstring>{message}</faultstring></SOAP-ENV:Fault>"
        "</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def soap_issue(attachments: str = "", summary: str = "Export the report as CSV") -> str:
    return soap_reply(
        "mc_issue_get",
        '<return xsi:type="tns:IssueData">'
        "<id>1234</id>"
        f"<summary>{summary}</summary>"
        "<description>Admins need a CSV export of the monthly report &amp; totals.</description>"
        "<steps_to_reproduce>Open Reports, look for an Export button.</steps_to_reproduce>"
        "<additional_information>Columns must match the on-screen table."
        "</additional_information>"
        "<notes>"
        "<item><id>1</id><reporter><id>3</id><name>sam</name><real_name>Sam</real_name>"
        "</reporter><text>Also needed for the Q4 audit.</text></item>"
        "<item><id>2</id><reporter><id>4</id><name>kim</name></reporter>"
        "<text>Use semicolons for EU locales.</text></item>"
        "</notes>"
        f"<attachments>{attachments}</attachments>"
        "</return>",
    )


def soap_operation(request: httpx.Request) -> str:
    body = request.content.decode()
    return "mc_issue_attachment_get" if "mc_issue_attachment_get" in body else "mc_issue_get"


@respx.mock
async def test_soap_maps_a_mantis_issue_to_the_ticket_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_soap(monkeypatch)
    route = respx.post(SOAP_URL).mock(return_value=httpx.Response(200, text=soap_issue()))

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    request = route.calls.last.request
    body = request.content.decode()
    assert request.headers["Content-Type"].startswith("text/xml")
    assert "<man:mc_issue_get>" in body
    assert f"<username>{SOAP_USER}</username>" in body
    assert "<issue_id>1234</issue_id>" in body
    # The REST token isn't used in SOAP mode; no Authorization header at all.
    assert "Authorization" not in request.headers
    assert ticket.key == "MANTIS-1234"
    assert ticket.summary == "Export the report as CSV"
    assert ticket.description == (
        "Admins need a CSV export of the monthly report & totals.\n\n"
        "Steps to reproduce:\nOpen Reports, look for an Export button."
    )
    assert ticket.acceptance_criteria == "Columns must match the on-screen table."
    assert ticket.comments == [
        "Sam: Also needed for the Q4 audit.",
        "kim: Use semicolons for EU locales.",
    ]
    assert ticket.attachments == []


@respx.mock
async def test_soap_escapes_credentials_in_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    use_soap(monkeypatch, mantis_username="a<b&c")
    monkeypatch.setenv("MANTIS_PASSWORD", "p</password><x>&q" + "z" * 12)
    route = respx.post(SOAP_URL).mock(return_value=httpx.Response(200, text=soap_issue()))

    await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    body = route.calls.last.request.content.decode()
    assert "<username>a&lt;b&amp;c</username>" in body
    assert "<password>p&lt;/password&gt;&lt;x&gt;&amp;q" in body


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("Access denied", ErrorCode.MANTIS_AUTH_FAILED),
        ("Issue '1234' does not exist.", ErrorCode.TICKET_NOT_FOUND),
        ("Issue #1234 not found", ErrorCode.TICKET_NOT_FOUND),
        ("Something unexpected happened", ErrorCode.MANTIS_UNREACHABLE),
    ],
)
@respx.mock
async def test_soap_faults_map_to_typed_errors(
    monkeypatch: pytest.MonkeyPatch, fault: str, code: ErrorCode
) -> None:
    use_soap(monkeypatch)
    # MantisConnect sends faults with HTTP 500.
    respx.post(SOAP_URL).mock(return_value=httpx.Response(500, text=soap_fault(fault)))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == code
    assert FAKE_MANTIS_PASSWORD not in str(excinfo.value.internal_detail)


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_soap_http_auth_rejection_is_mantis_auth_failed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    use_soap(monkeypatch)
    respx.post(SOAP_URL).mock(return_value=httpx.Response(status, text="Forbidden"))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_AUTH_FAILED


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>Maintenance</body></html>",  # parses, but has no <return>
        "not xml at all",
        # A DTD is refused before parsing (entity expansion / external entities).
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
    ],
)
@respx.mock
async def test_soap_unusable_replies_are_mantis_unreachable(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    use_soap(monkeypatch)
    respx.post(SOAP_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_UNREACHABLE


@respx.mock
async def test_soap_network_failure_is_mantis_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    use_soap(monkeypatch)
    respx.post(SOAP_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_UNREACHABLE


@respx.mock
async def test_soap_issue_with_no_summary_or_description_is_ticket_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_soap(monkeypatch)
    empty = soap_reply(
        "mc_issue_get", "<return><id>1234</id><summary> </summary><description/></return>"
    )
    respx.post(SOAP_URL).mock(return_value=httpx.Response(200, text=empty))

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.TICKET_EMPTY


@respx.mock  # no routes: any request at all would fail the test
async def test_soap_without_a_username_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(monkeypatch, mantis_api="soap")
    monkeypatch.setenv("MANTIS_PASSWORD", FAKE_MANTIS_PASSWORD)

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_CONFIG_MISSING


@respx.mock  # no routes: any request at all would fail the test
async def test_soap_without_a_password_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_settings(monkeypatch, mantis_api="soap", mantis_username=SOAP_USER)
    monkeypatch.delenv("MANTIS_PASSWORD", raising=False)
    monkeypatch.setattr("app.core.secrets._ENV_FILE", tmp_path / "missing.env")

    with pytest.raises(AppError) as excinfo:
        await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.MANTIS_CONFIG_MISSING


def soap_attachments() -> str:
    return (
        "<item><id>7</id><filename>spec.pdf</filename><size>2000</size>"
        "<content_type>application/pdf</content_type></item>"
        "<item><id>8</id><filename>screenshot.png</filename><size>2000</size>"
        "<content_type>image/png</content_type></item>"
        "<item><id>9</id><filename>fake.pdf</filename><size>10</size>"
        "<content_type>application/pdf</content_type></item>"
    )


def soap_attachment_reply(content: bytes) -> str:
    return soap_reply(
        "mc_issue_attachment_get",
        f'<return xsi:type="xsd:base64Binary">{base64.b64encode(content).decode()}</return>',
    )


@respx.mock
async def test_soap_pdf_attachments_are_extracted_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_soap(monkeypatch, mantis_attachment_fetch_enabled=True)
    pdf = make_pdf_bytes("Export columns: A, B, C.")
    requested: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if soap_operation(request) == "mc_issue_get":
            return httpx.Response(200, text=soap_issue(attachments=soap_attachments()))
        requested.append(body.split("<issue_attachment_id>")[1].split("<")[0])
        if "<issue_attachment_id>7<" in body:
            return httpx.Response(200, text=soap_attachment_reply(pdf))
        return httpx.Response(200, text=soap_attachment_reply(b"not really a pdf"))

    respx.post(SOAP_URL).mock(side_effect=reply)

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    # The PNG is skipped before any download.
    assert requested == ["7", "9"]
    assert len(ticket.attachments) == 1
    assert ticket.attachments[0].startswith("spec.pdf:")
    assert "Export columns: A, B, C." in ticket.attachments[0]


@respx.mock
async def test_soap_a_failing_attachment_never_fails_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_soap(monkeypatch, mantis_attachment_fetch_enabled=True)

    def reply(request: httpx.Request) -> httpx.Response:
        if soap_operation(request) == "mc_issue_get":
            return httpx.Response(200, text=soap_issue(attachments=soap_attachments()))
        return httpx.Response(500, text=soap_fault("Access denied"))

    respx.post(SOAP_URL).mock(side_effect=reply)

    ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert ticket.summary == "Export the report as CSV"
    assert ticket.attachments == []


@respx.mock
async def test_soap_password_never_reaches_logs_errors_or_the_ticket(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_soap(monkeypatch, mantis_attachment_fetch_enabled=True)

    def reply(request: httpx.Request) -> httpx.Response:
        if soap_operation(request) == "mc_issue_get":
            return httpx.Response(200, text=soap_issue(attachments=soap_attachments()))
        # A server that echoes the password back in its fault text.
        return httpx.Response(500, text=soap_fault(f"Bad login {FAKE_MANTIS_PASSWORD}"))

    respx.post(SOAP_URL).mock(side_effect=reply)

    with caplog.at_level(logging.DEBUG):
        ticket = await fetch_mantis_issue(make_job(), None)  # type: ignore[arg-type]

    assert FAKE_MANTIS_PASSWORD not in caplog.text
    assert FAKE_MANTIS_PASSWORD not in ticket.model_dump_json()


def test_settings_reject_an_unknown_mantis_api() -> None:
    with pytest.raises(ValueError, match="mantis_api"):
        Settings(_env_file=None, mantis_api="graphql")  # type: ignore[call-arg, arg-type]
