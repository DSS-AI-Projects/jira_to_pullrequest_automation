from collections.abc import Iterator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Settings are cached; clear around every test so env patches apply cleanly.

    The plan cache and stub mode default ON/off for real use, but the test suite
    must be deterministic and must not write a real cache file into the repo, so
    both are forced off here. Tests that exercise them opt in explicitly.
    """
    monkeypatch.setenv("AGENT_PLAN_CACHE_ENABLED", "false")
    monkeypatch.setenv("AGENT_PLAN_STUB", "false")
    monkeypatch.setenv("AGENT_REPO_DIGEST_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
