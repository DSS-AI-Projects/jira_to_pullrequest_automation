"""Persistent plan memoization.

Re-running the planner for the same ticket + repository inputs is pure waste —
the result is deterministic given the prompt, model, and effort. This cache
stores completed plans keyed on a hash of exactly those inputs, so identical
re-runs (common during testing/iteration) cost zero Anthropic tokens.

The key intentionally includes the full prompt (which embeds the ticket content
and the repo map, so it changes when either does), the model, the effort level,
and the plan schema version — any change to those invalidates the entry.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

from app.schemas.plan import SCHEMA_VERSION, Plan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_cache (
    key TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def plan_cache_key(prompt: str, model: str, effort: str) -> str:
    material = f"v{SCHEMA_VERSION}\0{model}\0{effort}\0{prompt}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PlanCache:
    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> Plan | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_json FROM plan_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return Plan.model_validate_json(row[0])
        except ValueError:
            # A stale/corrupt entry (e.g. from an older schema) must not poison
            # the run — treat it as a miss.
            return None

    def put(self, key: str, plan: Plan) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO plan_cache (key, plan_json) VALUES (?, ?)",
                (key, plan.model_dump_json()),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
