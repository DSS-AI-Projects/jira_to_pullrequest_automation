from collections.abc import Iterator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Settings are cached; clear around every test so env patches apply cleanly.

    The plan cache, stub mode and workspace retention default ON/off for real
    use, but the test suite must be deterministic and must not write (or
    delete) real files in the repo, so they are forced off here. Tests that
    exercise them opt in explicitly.
    """
    monkeypatch.setenv("AGENT_PLAN_CACHE_ENABLED", "false")
    monkeypatch.setenv("AGENT_PLAN_STUB", "false")
    monkeypatch.setenv("AGENT_REPO_DIGEST_CACHE_ENABLED", "false")
    # The retention sweep would otherwise start with every TestClient lifespan,
    # pointed at the real var/ directories; test_retention.py drives it directly.
    monkeypatch.setenv("WORKSPACE_RETENTION_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
