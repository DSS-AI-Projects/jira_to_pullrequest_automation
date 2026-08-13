"""Document-upload step: PDF text extraction as an alternative to Jira fetch."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job, RequirementSource
from app.steps.document_fetch import (
    extract_pdf_text,
    fetch_requirement_document,
    requirement_document_path,
)
from tests.pdf_fixtures import make_pdf_bytes


def make_job(job_id: str = "job1") -> Job:
    job = Job.new(
        ticket_key="DOC-ABCD1234",
        repo_url="https://github.com/acme/repo",
        requirement_source=RequirementSource.DOCUMENT,
        requirement_document_name="requirements.pdf",
    )
    job.id = job_id
    return job


# --- extract_pdf_text ---


def test_extracts_real_text_from_a_valid_pdf(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_pdf_bytes("Hello Requirements Document"))
    text = extract_pdf_text(path, max_chars=1000)
    assert "Hello Requirements Document" in text


def test_truncates_when_extracted_text_exceeds_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class FakeReader:
        def __init__(self, _path: str) -> None:
            self.pages = [FakePage("x" * 30), FakePage("y" * 30)]

    monkeypatch.setattr("app.steps.document_fetch.PdfReader", FakeReader)
    text = extract_pdf_text(Path("ignored.pdf"), max_chars=40)
    assert len(text) <= 40 + len("\n[... document truncated ...]")
    assert text.endswith("[... document truncated ...]")


def test_blank_pages_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class FakeReader:
        def __init__(self, _path: str) -> None:
            self.pages = [FakePage(""), FakePage("real content")]

    monkeypatch.setattr("app.steps.document_fetch.PdfReader", FakeReader)
    text = extract_pdf_text(Path("ignored.pdf"), max_chars=1000)
    assert text == "real content"


# --- fetch_requirement_document ---


async def test_happy_path_returns_ticket_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-happy")
    path = requirement_document_path(job.id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_pdf_bytes("The system shall support single sign-on."))

    result = await fetch_requirement_document(job, None)  # type: ignore[arg-type]

    assert result.key == job.ticket_key
    assert "requirements.pdf" in result.summary
    assert "single sign-on" in result.description
    assert result.acceptance_criteria is None
    assert result.comments == []


async def test_missing_file_is_typed_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-missing")
    with pytest.raises(AppError) as excinfo:
        await fetch_requirement_document(job, None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.DOCUMENT_UNREADABLE


async def test_non_pdf_file_on_disk_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-not-pdf")
    path = requirement_document_path(job.id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not actually a pdf")

    with pytest.raises(AppError) as excinfo:
        await fetch_requirement_document(job, None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.DOCUMENT_NOT_PDF


async def test_pdf_with_no_extractable_text_is_document_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-blank")
    path = requirement_document_path(job.id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())

    with pytest.raises(AppError) as excinfo:
        await fetch_requirement_document(job, None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.DOCUMENT_EMPTY


async def test_malformed_pdf_is_typed_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-malformed")
    path = requirement_document_path(job.id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid magic bytes, garbage body -> pypdf raises while parsing.
    path.write_bytes(b"%PDF-1.4\n" + b"\x00\x01\x02garbage" * 50)

    with pytest.raises(AppError) as excinfo:
        await fetch_requirement_document(job, None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.DOCUMENT_UNREADABLE


# --- dispatch (steps/__init__.py) ---


async def test_dispatch_routes_document_jobs_to_the_document_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.steps import fetch_requirement

    monkeypatch.setattr(
        "app.steps.document_fetch.get_settings",
        lambda: Settings(_env_file=None, document_upload_dir=tmp_path),  # type: ignore[call-arg]
    )
    job = make_job("job-dispatch-doc")
    path = requirement_document_path(job.id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_pdf_bytes("Dispatched via the document step."))

    result = await fetch_requirement(job, None)  # type: ignore[arg-type]

    assert "Dispatched via the document step." in result.description


async def test_dispatch_routes_jira_jobs_to_the_jira_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.steps import fetch_requirement

    called: dict[str, bool] = {"jira": False}

    async def fake_jira_fetch(job: Job, store: object) -> object:
        del store
        called["jira"] = True
        from app.schemas.ticket import TicketData

        return TicketData(key=job.ticket_key, summary="s", description="d")

    monkeypatch.setattr("app.steps.fetch_jira_ticket", fake_jira_fetch)
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    assert job.requirement_source == RequirementSource.JIRA

    await fetch_requirement(job, None)  # type: ignore[arg-type]

    assert called["jira"] is True
