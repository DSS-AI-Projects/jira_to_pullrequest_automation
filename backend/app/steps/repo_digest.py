"""Deterministic repo digest — a compact, high-signal orientation of the
repository, injected into the planning prompt so the agent needs fewer
exploration reads (cheaper) and targets the right files (more accurate).

It is derived with plain code (NO LLM call, zero tokens) from the tree-sitter
repo map plus a few well-known files, and cached per repo-state hash so it is
computed once per commit and reused across every ticket for that repo.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from app.schemas.repomap import RepoMap
from app.steps.repo_map import LANGUAGE_BY_EXTENSION

_DIGEST_VERSION = 1

# Well-known files that orient an engineer quickly (build/config/entry points).
_KEY_FILE_BASENAMES = {
    "readme.md",
    "readme.rst",
    "readme",
    "claude.md",
    "agents.md",
    "contributing.md",
    "architecture.md",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "composer.json",
    "dockerfile",
    "docker-compose.yml",
    "makefile",
    ".pre-commit-config.yaml",
}
_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_README_EXCERPT_CHARS = 1200
_TOP_CORE_MODULES = 8
_TOP_DIRS = 12
_TOP_LANGS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_digest (
    key TEXT PRIMARY KEY,
    digest_md TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def repo_digest_key(repo_map_text: str) -> str:
    """Key the digest on the repo map, which changes when the tree/symbols do
    (i.e. per commit-affecting change)."""
    return hashlib.sha256(f"v{_DIGEST_VERSION}\0{repo_map_text}".encode()).hexdigest()


def parse_repo_map_files(text: str) -> list[tuple[str, int]]:
    """Return (relative_path, symbol_count) for each file listed in the map.

    File lines are left-justified; symbol lines are indented; the truncation
    marker starts with '['. Shared with plan_stub.py so both the digest and
    the offline stub-plan generator rank files the same way.
    """
    files: list[tuple[str, int]] = []
    current: str | None = None
    count = 0
    for line in text.splitlines():
        if not line or line.startswith("["):
            continue
        if line.startswith(" "):
            if current is not None:
                count += 1
            continue
        if current is not None:
            files.append((current, count))
        current = line
        count = 0
    if current is not None:
        files.append((current, count))
    return files


def _language_line(paths: list[str]) -> str:
    counts: Counter[str] = Counter()
    for path in paths:
        suffix = "." + path.rsplit(".", 1)[1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        label = LANGUAGE_BY_EXTENSION.get(suffix, suffix.lstrip(".") or "other")
        counts[label] += 1
    top = counts.most_common(_TOP_LANGS)
    return ", ".join(f"{label} ({n})" for label, n in top) if top else "(none detected)"


def _top_dirs(paths: list[str]) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for path in paths:
        if "/" in path:
            counts[path.split("/", 1)[0]] += 1
    return counts.most_common(_TOP_DIRS)


def _key_files(paths: list[str]) -> list[str]:
    seen: list[str] = []
    for path in paths:
        base = path.rsplit("/", 1)[-1]
        if base.lower() in _KEY_FILE_BASENAMES and base not in seen:
            seen.append(base)
    return seen


def _core_modules(files: list[tuple[str, int]]) -> list[tuple[str, int]]:
    with_symbols = [(path, n) for path, n in files if n > 0]
    with_symbols.sort(key=lambda item: item[1], reverse=True)
    return with_symbols[:_TOP_CORE_MODULES]


def _readme_excerpt(clone_path: Path) -> str | None:
    for name in _README_NAMES:
        candidate = clone_path / name
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > _README_EXCERPT_CHARS:
            text = text[:_README_EXCERPT_CHARS].rstrip() + " [...]"
        return text
    return None


def build_repo_digest(clone_path: Path, repo_map: RepoMap, max_chars: int) -> str:
    files = parse_repo_map_files(repo_map.text)
    paths = [path for path, _ in files]

    lines: list[str] = ["Auto-generated repository orientation (deterministic; untrusted data)."]
    lines.append(f"- Files mapped: {len(paths)}{' (truncated)' if repo_map.truncated else ''}")
    lines.append(f"- Languages: {_language_line(paths)}")

    top_dirs = _top_dirs(paths)
    if top_dirs:
        lines.append("- Top-level directories (by mapped file count):")
        lines.extend(f"  - {name}/ ({n})" for name, n in top_dirs)

    key_files = _key_files(paths)
    if key_files:
        lines.append("- Key files: " + ", ".join(key_files))

    core = _core_modules(files)
    if core:
        lines.append("- Likely core modules (most symbols):")
        lines.extend(f"  - {path} ({n} symbols)" for path, n in core)

    readme = _readme_excerpt(clone_path)
    if readme:
        lines.append("- README excerpt:")
        lines.append(readme)

    digest = "\n".join(lines)
    if len(digest) > max_chars:
        digest = digest[:max_chars].rstrip() + "\n[... digest truncated ...]"
    return digest


class RepoDigestCache:
    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT digest_md FROM repo_digest WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def put(self, key: str, digest_md: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO repo_digest (key, digest_md) VALUES (?, ?)",
                (key, digest_md),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
