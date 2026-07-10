from pathlib import Path

from app.jobs.models import Job, JobState
from app.jobs.store import JobStore


def test_create_get_save_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = Job.new(ticket_key="PROJ-1", repo_url="https://github.com/acme/repo")
    store.create(job)

    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.state == JobState.QUEUED
    assert loaded.ticket_key == "PROJ-1"

    loaded.state = JobState.PLANNING
    store.save(loaded)
    again = store.get(job.id)
    assert again is not None
    assert again.state == JobState.PLANNING
    assert again.updated_at >= again.created_at


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    assert store.get("nope") is None


def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job = Job.new(ticket_key="PROJ-2", repo_url="https://github.com/acme/repo")
    store.create(job)
    store.close()

    reopened = JobStore(db)
    loaded = reopened.get(job.id)
    assert loaded is not None
    assert loaded.ticket_key == "PROJ-2"
