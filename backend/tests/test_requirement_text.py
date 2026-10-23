"""The TEXT requirement source: a requirement pasted into the job form."""

from __future__ import annotations

import re

import pytest

from app.core.errors import AppError, ErrorCode
from app.jobs.models import Job, RequirementSource
from app.steps.requirement_text import fetch_requirement_text, synthetic_text_key


def make_job(text: str | None, title: str | None = None, key: str = "TXT-ABCD1234") -> Job:
    return Job.new(
        ticket_key=key,
        repo_url="https://github.com/acme/repo",
        requirement_source=RequirementSource.TEXT,
        requirement_title=title,
        requirement_text=text,
    )


def test_synthetic_key_is_deterministic_and_content_based() -> None:
    first = synthetic_text_key("Login", "Users can sign in with SSO.")
    assert first == synthetic_text_key("Login", "Users can sign in with SSO.")
    assert first != synthetic_text_key("Login", "Users can sign in with a password.")
    assert first != synthetic_text_key("Logout", "Users can sign in with SSO.")


def test_synthetic_key_has_the_shape_the_clone_step_recognises() -> None:
    # repo_clone's _SYNTHETIC_DOCUMENT_KEY_RE (and the frontend's retry
    # check) treat exactly this shape as "not a real ticket key".
    assert re.fullmatch(r"TXT-[0-9A-F]{8}", synthetic_text_key(None, "Anything"))


async def test_fetch_uses_the_title_as_summary_and_the_text_as_description() -> None:
    job = make_job("Users can export the report as CSV.\nOnly admins.", title="CSV export")

    ticket = await fetch_requirement_text(job, None)  # type: ignore[arg-type]

    assert ticket.key == "TXT-ABCD1234"
    assert ticket.summary == "CSV export"
    assert ticket.description == "Users can export the report as CSV.\nOnly admins."
    assert ticket.comments == []


async def test_fetch_without_a_title_uses_the_first_line() -> None:
    job = make_job("\n\n  Add a dark mode toggle  \nIt should remember the choice.")

    ticket = await fetch_requirement_text(job, None)  # type: ignore[arg-type]

    assert ticket.summary == "Add a dark mode toggle"


async def test_a_long_first_line_is_shortened_for_the_summary() -> None:
    job = make_job("x" * 500)

    ticket = await fetch_requirement_text(job, None)  # type: ignore[arg-type]

    assert len(ticket.summary) == 120
    assert ticket.summary.endswith("…")
    assert ticket.description == "x" * 500


async def test_fetch_of_a_record_with_no_text_is_ticket_empty() -> None:
    with pytest.raises(AppError) as excinfo:
        await fetch_requirement_text(make_job("   "), None)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.TICKET_EMPTY


async def test_dispatch_routes_text_jobs_to_the_text_step() -> None:
    from app.steps import fetch_requirement

    job = make_job("Dispatched via the text step.", title="Dispatch")

    ticket = await fetch_requirement(job, None)  # type: ignore[arg-type]

    assert ticket.description == "Dispatched via the text step."
    assert ticket.summary == "Dispatch"
