#!/usr/bin/env python3
"""Consistent, verified, rotated snapshots of the app's SQLite database.

Runs as the `backup` sidecar in deploy/vm/docker-compose.yml (no network, the
data volume mounted read-only), and as the one-off `db-restore` service for
restores. Standard library only, so it runs on a plain python:3.12-slim image.

Commands:
  run              loop forever: take a snapshot whenever the newest one is
                   older than BACKUP_INTERVAL_HOURS (so restarts don't add
                   extra snapshots and push good ones out of rotation)
  once             take one snapshot now
  list             list snapshots, newest first
  verify FILE      PRAGMA integrity_check on one file (exit 1 if not "ok")
  health           exit 0 if the newest snapshot is younger than the interval
                   plus one hour (the container's healthcheck)
  restore FILE     replace BACKUP_SOURCE with FILE; the backend must be stopped

Snapshots use SQLite's online backup API (`Connection.backup`), never a file
copy: copying a live database file can capture a half-written transaction.
Each snapshot is written to a `.partial` file, checked with
`PRAGMA integrity_check`, and only then renamed into place, so a failed or
interrupted run never leaves a file that looks like a good snapshot — and
never rotates a good one out.

Settings (environment variables):
  BACKUP_SOURCE          database to back up / restore into (/data/jobs.db)
  BACKUP_DIR             where snapshots live (/backups)
  BACKUP_KEEP            snapshots to keep (14)
  BACKUP_INTERVAL_HOURS  hours between snapshots (24)

The read-only mount works because the backend uses SQLite's default rollback
journal: readers only take shared locks on the database file. A WAL-mode
database would need write access to its -shm file.

Provider tokens inside the database are Fernet-encrypted with keys from
backend/.env. A snapshot is useless without those keys, and the keys must
never be stored next to the snapshots (deploy/vm/README.md, "Backups").
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

SNAPSHOT_PREFIX = "jobs-"
SNAPSHOT_SUFFIX = ".db"
PARTIAL_SUFFIX = ".partial"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SNAPSHOT_RE = re.compile(r"^jobs-(\d{8}T\d{6}Z)\.db$")

# After a failed snapshot, try again this soon instead of a full interval later.
RETRY_AFTER = timedelta(minutes=15)
# How much later than due the newest snapshot may be before `health` fails.
HEALTH_GRACE = timedelta(hours=1)
# Upper bound on one sleep, so a changed clock or interval is noticed.
_MAX_SLEEP_SECONDS = 300.0


class BackupError(Exception):
    """A snapshot, verification or restore that did not succeed."""


@dataclass(frozen=True)
class Settings:
    source: Path
    backup_dir: Path
    keep: int
    interval: timedelta

    @classmethod
    def from_env(cls) -> Settings:
        try:
            keep = int(os.environ.get("BACKUP_KEEP", "14"))
            hours = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
        except ValueError as exc:
            raise BackupError(f"bad BACKUP_KEEP or BACKUP_INTERVAL_HOURS: {exc}") from exc
        if keep < 1:
            raise BackupError("BACKUP_KEEP must be at least 1")
        if hours <= 0:
            raise BackupError("BACKUP_INTERVAL_HOURS must be positive")
        return cls(
            source=Path(os.environ.get("BACKUP_SOURCE", "/data/jobs.db")),
            backup_dir=Path(os.environ.get("BACKUP_DIR", "/backups")),
            keep=keep,
            interval=timedelta(hours=hours),
        )


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} backup: {message}", file=sys.stderr, flush=True)


def snapshot_name(when: datetime) -> str:
    return f"{SNAPSHOT_PREFIX}{when.astimezone(UTC).strftime(_STAMP_FORMAT)}{SNAPSHOT_SUFFIX}"


def snapshot_time(path: Path) -> datetime | None:
    """The UTC time encoded in a snapshot's name, or None for any other file."""
    match = _SNAPSHOT_RE.match(path.name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=UTC)


def list_snapshots(backup_dir: Path) -> list[Path]:
    """Finished snapshots, oldest first (the names sort chronologically)."""
    if not backup_dir.is_dir():
        return []
    found = [p for p in backup_dir.iterdir() if p.is_file() and snapshot_time(p) is not None]
    return sorted(found, key=lambda p: p.name)


def _connect_read_only(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    # mode=ro: never create, never write — not even to roll back a journal.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)


def integrity_problems(path: Path) -> list[str]:
    """PRAGMA integrity_check's findings; an empty list means the file is sound."""
    if not path.is_file():
        return [f"{path} does not exist"]
    try:
        conn = _connect_read_only(path)
        try:
            rows: list[tuple[str]] = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return [f"not a readable SQLite database: {exc}"]
    messages = [str(row[0]) for row in rows]
    return [] if messages == ["ok"] else messages


def _fsync_file(path: Path) -> None:
    # Read-write open: Windows refuses to fsync a read-only handle.
    with path.open("rb+") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    # Makes a rename durable. Directories can't be opened this way on Windows.
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def take_snapshot(source: Path, backup_dir: Path, now: datetime) -> Path:
    """Write one verified snapshot of `source` into `backup_dir` and return it."""
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / snapshot_name(now)
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)
    try:
        src = _connect_read_only(source)
        try:
            dst = sqlite3.connect(partial)
            try:
                # pages=-1 copies everything in one step under one read lock:
                # a single consistent point in time.
                src.backup(dst, pages=-1)
            finally:
                dst.close()
        finally:
            src.close()
        problems = integrity_problems(partial)
        if problems:
            raise BackupError("integrity_check failed: " + "; ".join(problems[:5]))
        _fsync_file(partial)
        partial.chmod(0o600)  # user data and encrypted provider tokens
        partial.replace(final)
        _fsync_dir(backup_dir)
    except sqlite3.Error as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"snapshot failed: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return final


def prune(backup_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` snapshots, plus leftover partial files."""
    removed: list[Path] = []
    snapshots = list_snapshots(backup_dir)
    for old in snapshots[: max(len(snapshots) - keep, 0)]:
        old.unlink()
        removed.append(old)
    if backup_dir.is_dir():
        # Trailing *: an interrupted write can also leave a .partial-journal.
        for leftover in backup_dir.glob(f"{SNAPSHOT_PREFIX}*{PARTIAL_SUFFIX}*"):
            leftover.unlink(missing_ok=True)
            removed.append(leftover)
    return removed


def backup_once(settings: Settings, now: datetime) -> Path:
    path = take_snapshot(settings.source, settings.backup_dir, now)
    size_kib = path.stat().st_size // 1024
    log(f"snapshot {path.name} written ({size_kib} KiB), integrity_check ok")
    for old in prune(settings.backup_dir, settings.keep):
        log(f"removed {old.name}")
    return path


def seconds_until_due(newest: datetime | None, now: datetime, interval: timedelta) -> float:
    """How long until the next snapshot is due; 0 if one is due now."""
    if newest is None:
        return 0.0
    return max((newest + interval - now).total_seconds(), 0.0)


def newest_snapshot_time(backup_dir: Path) -> datetime | None:
    snapshots = list_snapshots(backup_dir)
    return snapshot_time(snapshots[-1]) if snapshots else None


def is_healthy(backup_dir: Path, now: datetime, interval: timedelta) -> bool:
    newest = newest_snapshot_time(backup_dir)
    return newest is not None and now - newest <= interval + HEALTH_GRACE


def run_forever(settings: Settings) -> None:
    log(
        f"every {settings.interval.total_seconds() / 3600:g} h, keeping {settings.keep}, "
        f"{settings.source} -> {settings.backup_dir}"
    )
    retry_at: datetime | None = None
    while True:
        now = datetime.now(UTC)
        wait = seconds_until_due(newest_snapshot_time(settings.backup_dir), now, settings.interval)
        if retry_at is not None:
            wait = max(wait, (retry_at - now).total_seconds())
        if wait > 0:
            time.sleep(min(wait, _MAX_SLEEP_SECONDS))
            continue
        try:
            backup_once(settings, now)
            retry_at = None
        except (BackupError, OSError) as exc:
            retry_at = now + RETRY_AFTER
            log(f"ERROR {exc}; retrying in {RETRY_AFTER.total_seconds() / 60:g} min")


def _database_in_use(path: Path) -> bool:
    """True if another process holds a lock on `path` right now."""
    conn = sqlite3.connect(path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()


def restore(snapshot: Path, target: Path, now: datetime) -> Path | None:
    """Replace `target` with `snapshot`; returns where the old database was saved.

    The backend must be stopped. The current database (if any) is first saved
    beside it as `<name>.pre-restore-<time>` via the backup API, so a restore
    can itself be undone. Stale -journal/-wal/-shm files are removed: SQLite
    would otherwise apply the old database's journal to the restored file.
    """
    problems = integrity_problems(snapshot)
    if problems:
        raise BackupError(f"{snapshot} failed integrity_check: " + "; ".join(problems[:5]))
    target.parent.mkdir(parents=True, exist_ok=True)
    saved: Path | None = None
    if target.exists():
        if _database_in_use(target):
            raise BackupError(f"{target} is in use; stop the backend first")
        stamp = now.astimezone(UTC).strftime(_STAMP_FORMAT)
        saved = target.with_name(f"{target.name}.pre-restore-{stamp}")
        old = sqlite3.connect(target)
        try:
            copy = sqlite3.connect(saved)
            try:
                old.backup(copy, pages=-1)
            finally:
                copy.close()
        finally:
            old.close()
        saved.chmod(0o600)
    incoming = target.with_name(target.name + ".restoring")
    shutil.copyfile(snapshot, incoming)
    incoming.chmod(0o600)
    problems = integrity_problems(incoming)
    if problems:
        incoming.unlink(missing_ok=True)
        raise BackupError("copied file failed integrity_check: " + "; ".join(problems[:5]))
    _fsync_file(incoming)
    for suffix in ("-journal", "-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    incoming.replace(target)
    _fsync_dir(target.parent)
    return saved


def _stop(signum: int, frame: FrameType | None) -> None:
    # PID 1 in a container ignores SIGTERM unless it handles it; exit promptly
    # on `docker compose stop`. A half-written .partial is cleaned up next run.
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="snapshot on a schedule, forever")
    sub.add_parser("once", help="take one snapshot now")
    sub.add_parser("list", help="list snapshots, newest first")
    sub.add_parser("health", help="exit 0 if the newest snapshot is recent enough")
    verify_cmd = sub.add_parser("verify", help="integrity_check one file")
    verify_cmd.add_argument("file", type=Path)
    restore_cmd = sub.add_parser("restore", help="restore a snapshot (backend stopped)")
    restore_cmd.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
        command: str = args.command
        if command == "run":
            signal.signal(signal.SIGTERM, _stop)
            run_forever(settings)
        elif command == "once":
            backup_once(settings, datetime.now(UTC))
        elif command == "list":
            for path in reversed(list_snapshots(settings.backup_dir)):
                print(f"{path.name}\t{path.stat().st_size} bytes")
        elif command == "health":
            return 0 if is_healthy(settings.backup_dir, datetime.now(UTC), settings.interval) else 1
        elif command == "verify":
            file: Path = args.file
            problems = integrity_problems(file)
            for problem in problems:
                print(problem)
            print(f"{file}: {'FAILED' if problems else 'ok'}")
            return 1 if problems else 0
        elif command == "restore":
            snapshot: Path = args.file
            saved = restore(snapshot, settings.source, datetime.now(UTC))
            if saved is not None:
                log(f"previous database saved as {saved}")
            log(f"restored {snapshot} -> {settings.source}; start the backend again")
    except (BackupError, sqlite3.Error, OSError) as exc:
        log(f"ERROR {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
