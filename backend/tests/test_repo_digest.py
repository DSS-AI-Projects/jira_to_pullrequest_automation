"""Deterministic repo digest + its cache."""

from pathlib import Path

from app.schemas.repomap import RepoMap
from app.steps.repo_digest import (
    RepoDigestCache,
    build_repo_digest,
    repo_digest_key,
)

REPO_MAP_TEXT = """\
README.md
pyproject.toml
backend/app/jobs/store.py
  class JobStore (line 10)
  fn create (line 20)
  fn get (line 30)
backend/app/steps/repo_map.py
  fn build_repo_map (line 5)
frontend/src/lib/api.ts
  fn fetchRepos (line 1)
frontend/src/app/page.tsx
"""


def repo_map() -> RepoMap:
    return RepoMap(text=REPO_MAP_TEXT, file_count=6, symbol_count=5)


def test_digest_summarizes_languages_dirs_key_files_and_core_modules(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Acme\nThis service does X.", encoding="utf-8")
    digest = build_repo_digest(tmp_path, repo_map(), max_chars=8000)

    assert "python" in digest.lower()
    assert "typescript" in digest.lower() or "tsx" in digest.lower()
    # top-level dirs
    assert "backend/" in digest
    assert "frontend/" in digest
    # key files detected from the map
    assert "README.md" in digest
    assert "pyproject.toml" in digest
    # core module = the file with the most symbols
    assert "backend/app/jobs/store.py" in digest
    # README excerpt pulled from the clone
    assert "This service does X." in digest


def test_digest_without_readme_still_builds(tmp_path: Path) -> None:
    digest = build_repo_digest(tmp_path, repo_map(), max_chars=8000)
    assert "Languages:" in digest
    assert "README excerpt" not in digest  # none present


def test_digest_is_capped(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * 5000, encoding="utf-8")
    digest = build_repo_digest(tmp_path, repo_map(), max_chars=200)
    assert "digest truncated" in digest
    assert len(digest) <= 200 + len("\n[... digest truncated ...]")


def test_key_changes_with_repo_map() -> None:
    a = repo_digest_key("map A")
    assert a == repo_digest_key("map A")
    assert a != repo_digest_key("map B")


def test_cache_roundtrip_and_persistence(tmp_path: Path) -> None:
    db = tmp_path / "repo_digest.db"
    cache = RepoDigestCache(db)
    cache.put("k", "## digest body")
    assert cache.get("k") == "## digest body"
    assert cache.get("absent") is None
    cache.close()

    reopened = RepoDigestCache(db)
    assert reopened.get("k") == "## digest body"
    reopened.close()
