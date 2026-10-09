"""Keep Open Library trouble from taking Lenny down with it.

Every OPDS page asks Open Library for its metadata, and the library Lenny uses
makes that request with no timeout. When openlibrary.org is slow or unreachable
each patron's request then waits (a minute or more), the worker threads fill with
waiting requests, and the whole API stops answering, even for pages that never
touch Open Library.

Three small guards, shared by every request in a worker process:

* a deadline on every call (`OL_TIMEOUT`), so a request fails fast;
* a circuit breaker: after `FAILURE_THRESHOLD` failures in a row, calls are not
  attempted for `COOLDOWN` seconds, so a down service costs nothing while it is
  down, and one probe is let through afterwards to find out if it came back;
* the last good answer for the same question, served when a call fails or the
  breaker is open, so a patron sees a slightly old feed instead of an empty one.

State is per worker process on purpose: it needs no shared store, and each worker
discovers an outage after a handful of failed calls.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace

import requests

logger = logging.getLogger(__name__)

OL_TIMEOUT = float(os.environ.get("LENNY_OL_TIMEOUT", 8))
STALE_TTL = int(os.environ.get("LENNY_OL_STALE_TTL", 3600))
STALE_MAX_ENTRIES = 256
FAILURE_THRESHOLD = 3
COOLDOWN = 30


class UpstreamUnavailable(requests.exceptions.RequestException):
    """Raised without calling out, because the breaker is open and nothing is cached.

    A RequestException so the existing "Open Library unreachable" handlers
    answer with an empty catalog, exactly as they do for a real failure.
    """


def install_timeout(module) -> None:
    """Give `module.requests.get` a default timeout.

    pyopds2_openlibrary calls `requests.get(...)` bare. Looking `requests.get` up
    at call time keeps `patch("requests.get")` working in tests.
    """
    def get(url, **kwargs):
        kwargs.setdefault("timeout", OL_TIMEOUT)
        return requests.get(url, **kwargs)

    module.requests = SimpleNamespace(
        **{**{k: v for k, v in vars(requests).items() if not k.startswith("__")}, "get": get})


class Guard:
    def __init__(self, threshold=FAILURE_THRESHOLD, cooldown=COOLDOWN,
                 stale_ttl=STALE_TTL, max_entries=STALE_MAX_ENTRIES, clock=time.monotonic):
        self.threshold, self.cooldown = threshold, cooldown
        self.stale_ttl, self.max_entries, self.clock = stale_ttl, max_entries, clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._stale = OrderedDict()

    def _is_open(self) -> bool:
        return self.clock() < self._open_until

    def _remember(self, key, value) -> None:
        with self._lock:
            self._stale[key] = (self.clock(), value)
            self._stale.move_to_end(key)
            while len(self._stale) > self.max_entries:
                self._stale.popitem(last=False)

    def _recall(self, key):
        with self._lock:
            hit = self._stale.get(key)
        if hit and self.clock() - hit[0] <= self.stale_ttl:
            return hit[1]
        return None

    def call(self, key, fn, *args, **kwargs):
        """Run `fn`; on failure return the last good answer for `key` if any.

        With nothing cached the failure propagates (or UpstreamUnavailable, if the
        breaker is open), for the caller's existing handler.
        """
        if self._is_open():
            if (stale := self._recall(key)) is not None:
                return stale
            raise UpstreamUnavailable("upstream unavailable; retrying shortly")
        try:
            result = fn(*args, **kwargs)
        except (requests.exceptions.RequestException, OSError) as e:
            with self._lock:
                self._failures += 1
                if self._failures >= self.threshold:
                    # Re-arming on each failed probe keeps a long outage quiet.
                    self._open_until = self.clock() + self.cooldown
            logger.warning("Open Library call failed (%s)", e)
            if (stale := self._recall(key)) is not None:
                return stale
            raise
        with self._lock:
            self._failures = 0
        self._remember(key, result)
        return result


open_library = Guard()
