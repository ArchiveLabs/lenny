"""Open Library being slow or down must not hang or break Lenny's feeds."""

import os
from unittest.mock import patch

import pytest
import requests

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from lenny.core import upstream  # noqa: E402
from lenny.core.upstream import Guard, UpstreamUnavailable  # noqa: E402


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def boom(*a, **k):
    raise requests.exceptions.ConnectionError("down")


def test_success_passes_through_and_is_remembered():
    g = Guard(clock=Clock())
    assert g.call("k", lambda: "fresh") == "fresh"
    assert g.call("k", boom) == "fresh"  # stale fallback on failure


def test_failure_without_cache_propagates_for_existing_handlers():
    with pytest.raises(requests.exceptions.RequestException):
        Guard(clock=Clock()).call("k", boom)


def test_breaker_opens_then_skips_calls_then_probes_after_cooldown():
    clock, calls = Clock(), []

    def failing():
        calls.append(1)
        raise requests.exceptions.Timeout("slow")

    g = Guard(threshold=3, cooldown=30, clock=clock)
    for _ in range(3):
        with pytest.raises(requests.exceptions.Timeout):
            g.call("k", failing)
    assert len(calls) == 3
    with pytest.raises(UpstreamUnavailable):  # open: no upstream call at all
        g.call("k", failing)
    assert len(calls) == 3
    clock.now = 31
    assert g.call("k", lambda: "back") == "back"  # probe succeeds, breaker closes
    assert g.call("k2", lambda: "x") == "x"


def test_open_breaker_serves_stale_not_error():
    clock = Clock()
    g = Guard(threshold=1, clock=clock)
    g.call("k", lambda: "old")
    with pytest.raises(requests.exceptions.ConnectionError):
        g.call("other", boom)
    assert g.call("k", boom) == "old"


def test_stale_answer_expires():
    clock = Clock()
    g = Guard(stale_ttl=60, clock=clock)
    g.call("k", lambda: "old")
    clock.now = 61
    with pytest.raises(requests.exceptions.ConnectionError):
        g.call("k", boom)


def test_stale_cache_is_bounded():
    g = Guard(max_entries=2, clock=Clock())
    for k in "abc":
        g.call(k, lambda k=k: k)
    assert len(g._stale) == 2 and "a" not in g._stale


def test_other_errors_are_not_swallowed():
    with pytest.raises(ValueError):
        Guard(clock=Clock()).call("k", lambda: (_ for _ in ()).throw(ValueError("bug")))


def test_open_library_requests_get_a_timeout():
    import pyopds2_openlibrary
    import lenny.core.api  # noqa: F401  (installs the shim)
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        raise requests.exceptions.ConnectionError

    with patch("requests.get", fake_get):
        with pytest.raises(requests.exceptions.ConnectionError):
            pyopds2_openlibrary.requests.get("https://openlibrary.org/x", params={})
    assert seen["timeout"] == upstream.OL_TIMEOUT
