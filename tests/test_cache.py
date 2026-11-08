"""Tests for Cache.is_throttled — used for OTP send rate-limiting and the
"one import run at a time" guard (standard ebooks, BRIET).

The concurrency test only proves anything against real Postgres: the
advisory lock closing the race is a documented no-op on SQLite, which
already serializes writers on its single test connection regardless of the
lock, so the race can't be reproduced there either way.
"""

import os
import threading

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from lenny.core.cache import Cache, CacheEntry  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_cache_table():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    try:
        db.execute(sqlalchemy.text("DELETE FROM cache"))
        db.commit()
    except Exception:
        db.rollback()
    db.remove()


def test_first_attempt_under_limit_is_not_throttled():
    assert Cache.is_throttled("test_scope", "key1", limit=3, ttl=60) is False


def test_nth_attempt_at_limit_is_throttled():
    for _ in range(3):
        Cache.is_throttled("test_scope", "key2", limit=3, ttl=60)
    assert Cache.is_throttled("test_scope", "key2", limit=3, ttl=60) is True


def test_different_keys_have_independent_limits():
    Cache.is_throttled("test_scope", "key3a", limit=1, ttl=60)
    assert Cache.is_throttled("test_scope", "key3b", limit=1, ttl=60) is False


def test_concurrent_callers_never_exceed_the_limit():
    """The actual race: two callers reading the same pre-record count and
    both concluding they're under the limit. Fires 10 truly concurrent
    calls at limit=1 and checks exactly one gets through."""
    if engine.dialect.name != "postgresql":
        pytest.skip("advisory lock is a no-op on SQLite; race isn't reproducible there")

    scope, key = "concurrency_test", "shared_key"
    results = []

    def attempt():
        results.append(Cache.is_throttled(scope, key, limit=1, ttl=60))

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(False) == 1, f"expected exactly 1 to get through, got: {results}"
