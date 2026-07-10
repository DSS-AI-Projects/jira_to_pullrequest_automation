from collections.abc import Iterator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings() -> Iterator[None]:
    """Settings are cached; clear around every test so env patches apply cleanly."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
