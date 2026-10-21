"""deploy/vm/backup/sqlite_backup.py: the VM stack's database backup sidecar.

Snapshots must be consistent (online backup API, not a file copy), verified
with integrity_check before they count, rotated to the newest N, and
restorable — and a failed snapshot must never rotate a good one out.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_BACKUP_DIR = Path(__file__).resolve().parents[2] / "deploy" / "vm" / "backup"
sys.path.insert(0, str(_BACKUP_DIR))

import sqlite_backup  # noqa: E402

NOW = datetime(2026, 10, 21, 2, 30, 0, tzinfo=UTC)


def _make_db(path: Path, rows: list[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, ticket TEXT)")
        conn.executemany("INSERT INTO jobs (ticket) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _tickets(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT ticket FROM jobs ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    db = tmp_path / "var" / "jobs.db"
    db.parent.mkdir()
    _make_db(db, ["ABC-1", "ABC-2"])
    return db


def test_snapshot_is_complete_verified_and_private(source: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"

    snap = sqlite_backup.take_snapshot(source, backups, NOW)

    assert snap.name == "jobs-20261021T023000Z.db"
    assert _tickets(snap) == ["ABC-1", "ABC-2"]
    assert sqlite_backup.integrity_problems(snap) == []
    assert [p.name for p in backups.iterdir()] == [snap.name]  # no .partial left
    if os.name == "posix":
        assert snap.stat().st_mode & 0o777 == 0o600


def test_snapshot_of_live_db_excludes_uncommitted_write(source: Path, tmp_path: Path) -> None:
    writer = sqlite3.connect(source, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO jobs (ticket) VALUES ('IN-FLIGHT')")
        snap = sqlite_backup.take_snapshot(source, tmp_path / "backups", NOW)
        writer.execute("COMMIT")
    finally:
        writer.close()

    assert _tickets(snap) == ["ABC-1", "ABC-2"]
    assert _tickets(source) == ["ABC-1", "ABC-2", "IN-FLIGHT"]


def test_snapshot_never_writes_to_source(source: Path, tmp_path: Path) -> None:
    before = source.read_bytes()
    sqlite_backup.take_snapshot(source, tmp_path / "backups", NOW)
    assert source.read_bytes() == before
    assert sorted(p.name for p in source.parent.iterdir()) == ["jobs.db"]


def test_missing_source_is_a_backup_error(tmp_path: Path) -> None:
    with pytest.raises(sqlite_backup.BackupError, match="not found"):
        sqlite_backup.take_snapshot(tmp_path / "nope.db", tmp_path / "backups", NOW)


def test_unreadable_source_leaves_no_snapshot(tmp_path: Path) -> None:
    bad = tmp_path / "jobs.db"
    bad.write_bytes(b"this is not an sqlite database" * 200)
    backups = tmp_path / "backups"

    with pytest.raises(sqlite_backup.BackupError):
        sqlite_backup.take_snapshot(bad, backups, NOW)

    assert list(backups.iterdir()) == []


def test_failed_integrity_check_keeps_old_snapshots(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "backups"
    settings = sqlite_backup.Settings(source, backups, keep=2, interval=timedelta(hours=24))
    for days in (3, 2):
        sqlite_backup.backup_once(settings, NOW - timedelta(days=days))
    good = [p.name for p in sqlite_backup.list_snapshots(backups)]

    def corrupt(_path: Path) -> list[str]:
        return ["page 3: corrupt"]

    monkeypatch.setattr(sqlite_backup, "integrity_problems", corrupt)
    with pytest.raises(sqlite_backup.BackupError, match="integrity_check failed"):
        sqlite_backup.backup_once(settings, NOW)

    assert sorted(p.name for p in backups.iterdir()) == good


def test_rotation_keeps_newest_n_and_cleans_partials(source: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    settings = sqlite_backup.Settings(source, backups, keep=3, interval=timedelta(hours=24))
    backups.mkdir()
    (backups / "jobs-20261001T000000Z.db.partial").write_bytes(b"torn")
    (backups / "jobs-20261001T000000Z.db.partial-journal").write_bytes(b"torn")
    (backups / "notes.txt").write_text("not a snapshot")

    for day in range(5):
        sqlite_backup.backup_once(settings, NOW + timedelta(days=day))

    names = sorted(p.name for p in backups.iterdir())
    assert names == [
        "jobs-20261023T023000Z.db",
        "jobs-20261024T023000Z.db",
        "jobs-20261025T023000Z.db",
        "notes.txt",
    ]


def test_schedule_is_based_on_newest_snapshot() -> None:
    day = timedelta(hours=24)
    assert sqlite_backup.seconds_until_due(None, NOW, day) == 0
    assert sqlite_backup.seconds_until_due(NOW - timedelta(hours=25), NOW, day) == 0
    assert sqlite_backup.seconds_until_due(NOW - timedelta(hours=20), NOW, day) == 4 * 3600


def test_health_needs_a_recent_snapshot(source: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    day = timedelta(hours=24)
    assert not sqlite_backup.is_healthy(backups, NOW, day)

    sqlite_backup.take_snapshot(source, backups, NOW - timedelta(hours=24, minutes=30))
    assert sqlite_backup.is_healthy(backups, NOW, day)
    assert not sqlite_backup.is_healthy(backups, NOW + timedelta(hours=1), day)


def test_run_snapshots_immediately_then_waits(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "backups"
    settings = sqlite_backup.Settings(source, backups, keep=14, interval=timedelta(hours=24))
    sleeps: list[float] = []

    class _Stop(Exception):
        pass

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _Stop

    monkeypatch.setattr(sqlite_backup.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        sqlite_backup.run_forever(settings)

    assert len(sqlite_backup.list_snapshots(backups)) == 1
    assert 0 < sleeps[0] <= 300


def test_restore_replaces_db_and_keeps_the_old_one(source: Path, tmp_path: Path) -> None:
    snap = sqlite_backup.take_snapshot(source, tmp_path / "backups", NOW)
    _make_db(source, ["ABC-3"])  # work done after the snapshot
    stale_journal = source.with_name("jobs.db-journal")
    stale_journal.write_bytes(b"journal of the old database")

    saved = sqlite_backup.restore(snap, source, NOW + timedelta(hours=1))

    assert _tickets(source) == ["ABC-1", "ABC-2"]
    assert not stale_journal.exists()
    assert saved is not None
    assert saved.name == "jobs.db.pre-restore-20261021T033000Z"
    assert _tickets(saved) == ["ABC-1", "ABC-2", "ABC-3"]
    assert not source.with_name("jobs.db.restoring").exists()


def test_restore_into_empty_volume(source: Path, tmp_path: Path) -> None:
    snap = sqlite_backup.take_snapshot(source, tmp_path / "backups", NOW)
    target = tmp_path / "fresh" / "jobs.db"

    assert sqlite_backup.restore(snap, target, NOW) is None
    assert _tickets(target) == ["ABC-1", "ABC-2"]


def test_restore_refuses_a_corrupt_snapshot(source: Path, tmp_path: Path) -> None:
    bad = tmp_path / "jobs-20261021T023000Z.db"
    bad.write_bytes(b"garbage" * 1000)
    before = source.read_bytes()

    with pytest.raises(sqlite_backup.BackupError, match="integrity_check"):
        sqlite_backup.restore(bad, source, NOW)

    assert source.read_bytes() == before


def test_restore_refuses_a_database_in_use(source: Path, tmp_path: Path) -> None:
    snap = sqlite_backup.take_snapshot(source, tmp_path / "backups", NOW)
    holder = sqlite3.connect(source, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite_backup.BackupError, match="in use"):
            sqlite_backup.restore(snap, source, NOW)
        holder.execute("ROLLBACK")
    finally:
        holder.close()


def test_cli_once_verify_list_health(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backups = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_SOURCE", str(source))
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    monkeypatch.setenv("BACKUP_KEEP", "2")

    assert sqlite_backup.main(["health"]) == 1
    assert sqlite_backup.main(["once"]) == 0
    assert sqlite_backup.main(["health"]) == 0
    (snap,) = sqlite_backup.list_snapshots(backups)
    assert sqlite_backup.main(["verify", str(snap)]) == 0
    assert sqlite_backup.main(["list"]) == 0
    assert snap.name in capsys.readouterr().out

    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage" * 1000)
    assert sqlite_backup.main(["verify", str(bad)]) == 1

    monkeypatch.setenv("BACKUP_KEEP", "zero")
    assert sqlite_backup.main(["once"]) == 1
