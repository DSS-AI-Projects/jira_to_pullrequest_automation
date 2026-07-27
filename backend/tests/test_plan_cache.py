"""Plan memoization cache."""

from pathlib import Path

from app.steps.plan_cache import PlanCache, plan_cache_key
from tests.fakes import sample_plan


def test_key_depends_on_prompt_model_and_effort() -> None:
    base = plan_cache_key("prompt A", "claude-sonnet-5", "medium")
    assert base == plan_cache_key("prompt A", "claude-sonnet-5", "medium")  # stable
    assert base != plan_cache_key("prompt B", "claude-sonnet-5", "medium")  # prompt
    assert base != plan_cache_key("prompt A", "claude-opus-4-8", "medium")  # model
    assert base != plan_cache_key("prompt A", "claude-sonnet-5", "high")  # effort


def test_put_then_get_roundtrips(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "plan_cache.db")
    plan = sample_plan()
    cache.put("k1", plan)
    got = cache.get("k1")
    assert got is not None
    assert got == plan
    cache.close()


def test_miss_returns_none(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "plan_cache.db")
    assert cache.get("absent") is None
    cache.close()


def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "plan_cache.db"
    cache = PlanCache(db)
    cache.put("k", sample_plan())
    cache.close()

    reopened = PlanCache(db)
    assert reopened.get("k") is not None
    reopened.close()


def test_corrupt_entry_is_treated_as_miss(tmp_path: Path) -> None:
    db = tmp_path / "plan_cache.db"
    cache = PlanCache(db)
    with cache._lock:  # pyright: ignore[reportPrivateUsage]
        cache._conn.execute(  # pyright: ignore[reportPrivateUsage]
            "INSERT INTO plan_cache (key, plan_json) VALUES (?, ?)", ("bad", "{not json}")
        )
        cache._conn.commit()  # pyright: ignore[reportPrivateUsage]
    assert cache.get("bad") is None
    cache.close()
