"""When Open Library cannot be reached the item listing must not crash (it was a bare 500) or look empty:
serve the last known listing if there is one, else say 503."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from lenny.core.api import LennyAPI  # noqa: E402


class Book(dict):
    olid = 7

    def __add__(self, other):
        return {**self, **other}


@pytest.fixture(autouse=True)
def clean_cache():
    LennyAPI._enrich_cache.clear()
    yield
    LennyAPI._enrich_cache.clear()


ITEMS = [SimpleNamespace(openlibrary_edition=7)]


def down(*a, **k):
    raise httpx.ConnectTimeout("timed out")


def test_a_stale_listing_is_served_while_open_library_is_down():
    with patch("lenny.core.api.OpenLibrary.search", return_value=[Book(title="Dune")]):
        assert LennyAPI._enrich_items(ITEMS)[7]["title"] == "Dune"
    key = next(iter(LennyAPI._enrich_cache))
    expiry, data = LennyAPI._enrich_cache[key]
    LennyAPI._enrich_cache[key] = (0, data)  # expired
    with patch("lenny.core.api.OpenLibrary.search", side_effect=down):
        assert LennyAPI._enrich_items(ITEMS)[7]["title"] == "Dune"
    assert LennyAPI._enrich_cache[key][0] == 0, "the stale entry must not be refreshed, so the next call retries"


def test_with_nothing_cached_the_error_surfaces():
    with patch("lenny.core.api.OpenLibrary.search", side_effect=down), pytest.raises(httpx.ConnectTimeout):
        LennyAPI._enrich_items(ITEMS)


def test_the_admin_listing_answers_503_not_500():
    from fastapi.testclient import TestClient
    from lenny.app import app
    from lenny.core import auth
    secret = "internal-secret-for-tests"
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", secret), patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None), patch.object(LennyAPI, "get_enriched_items", side_effect=down):
        h = {"X-Admin-Internal-Secret": secret, "Authorization": f"Bearer {auth.issue_admin_token()}"}
        r = TestClient(app).get("/v1/api/admin/items?limit=500", headers=h)
        assert r.status_code == 503 and "Open Library" in r.json()["detail"]
        assert TestClient(app).get("/v1/api/items").status_code == 503
