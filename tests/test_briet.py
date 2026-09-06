#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    tests.test_briet
    ~~~~~~~~~~~~~~~~

    Tests the BRIET importer: redeem-response parsing, retry/backoff policy,
    and the import job state that backs the admin "processing" view.

    :copyright: (c) 2015 by Authors.
    :license: see LICENSE for more details.
"""

import io
from unittest.mock import patch

import pytest

pytest.importorskip("httpx")
import httpx

from lenny.core.briet import BRIET, import_briet_books, parse_olid
from lenny.core.client import MAX_DOWNLOAD_BYTES, assert_fetchable, safe_log_url
from lenny.core.imports import ImportJob, SCOPE, DONE, FAILED, PENDING

EPUB_BYTES = b'PK\x03\x04' + b'0' * 128


@pytest.fixture
def db():
    """Create the cache table on the global (in-memory, TESTING) session."""
    from lenny.core.db import engine, session
    from lenny.core.cache import CacheEntry

    CacheEntry.__table__.create(engine, checkfirst=True)
    yield session
    session.rollback()
    CacheEntry.__table__.drop(engine, checkfirst=True)


def _response(status_code, json_data=None, headers=None):
    return httpx.Response(
        status_code,
        json=json_data if json_data is not None else {},
        headers=headers or {},
        request=httpx.Request("GET", "https://example.test/redeem/CODE"),
    )


def _patch_get(responses):
    """Patch httpx.Client.get to return each response in turn."""
    calls = {"n": 0}

    def fake_get(self, url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        result = responses[min(i, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    return patch.object(httpx.Client, "get", fake_get), calls


# --- parse_olid -------------------------------------------------------------

def test_parse_olid_accepts_ol_form_and_bare_int():
    assert parse_olid("OL32941311M") == 32941311
    assert parse_olid("ol32941311m") == 32941311
    assert parse_olid(32941311) == 32941311
    assert parse_olid("not-an-olid") is None
    assert parse_olid(None) is None


def test_parse_olid_rejects_out_of_range_values():
    """An integer column downstream cannot hold these; drop them at the door."""
    assert parse_olid(0) is None
    assert parse_olid(-5) is None
    assert parse_olid(2 ** 31) is None


# --- download URL guard -----------------------------------------------------

def test_assert_fetchable_rejects_non_https_and_internal_addresses():
    """A bundle names where each book lives, so the URL is untrusted input."""
    for url in (
        "http://cdn.test/a.epub",           # plaintext
        "file:///etc/passwd",               # not even http
        "https://127.0.0.1/a.epub",         # loopback
        "https://169.254.169.254/latest",   # cloud metadata
        "https://10.0.0.5/a.epub",          # private network
        "https://[::1]/a.epub",             # loopback, v6
    ):
        with pytest.raises(ValueError):
            assert_fetchable(url)


def test_safe_log_url_drops_a_presigned_signature():
    assert safe_log_url("https://cdn.test/a.epub?X-Amz-Signature=deadbeef") == "https://cdn.test/a.epub"


def test_download_epub_aborts_an_oversized_stream(monkeypatch):
    """An unbounded response must not be read into the API's memory."""
    from lenny.core import client as client_module

    monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 16)
    monkeypatch.setattr(client_module, "assert_fetchable", lambda url: None)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"P" * 64))
    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    assert client_module.download_epub("https://cdn.test/a.epub") is None


# --- redeem -----------------------------------------------------------------

def test_redeem_parses_book_list():
    payload = {"books": [
        {"olid": "OL111M", "url": "https://cdn.test/a.epub", "title": "A"},
        {"openlibrary_edition": "OL222M", "download_url": "https://cdn.test/b.epub"},
    ]}
    patcher, calls = _patch_get([_response(200, payload)])
    with patcher:
        books = BRIET.redeem("CODE")

    assert calls["n"] == 1
    assert books == [
        {"olid": 111, "url": "https://cdn.test/a.epub", "title": "A"},
        {"olid": 222, "url": "https://cdn.test/b.epub", "title": None},
    ]


def test_redeem_accepts_bare_list_payload():
    patcher, _ = _patch_get([_response(200, [{"olid": "OL9M", "url": "https://cdn.test/x.epub"}])])
    with patcher:
        assert BRIET.redeem("CODE")[0]["olid"] == 9


def test_redeem_skips_entries_missing_olid_or_url():
    payload = {"books": [
        {"olid": "garbage", "url": "https://cdn.test/a.epub"},
        {"olid": "OL5M"},  # no url
        {"olid": "OL7M", "url": "https://cdn.test/c.epub"},
    ]}
    patcher, _ = _patch_get([_response(200, payload)])
    with patcher:
        books = BRIET.redeem("CODE")

    assert [b["olid"] for b in books] == [7]


def test_redeem_skips_non_https_urls():
    payload = {"books": [
        {"olid": "OL1M", "url": "http://cdn.test/a.epub"},
        {"olid": "OL2M", "url": "https://cdn.test/b.epub"},
    ]}
    patcher, _ = _patch_get([_response(200, payload)])
    with patcher:
        assert [b["olid"] for b in BRIET.redeem("CODE")] == [2]


def test_redeem_caps_an_oversized_bundle_and_bounds_titles():
    payload = {"books": [
        {"olid": f"OL{i + 1}M", "url": "https://cdn.test/a.epub", "title": "T" * 5000}
        for i in range(BRIET.MAX_BOOKS + 10)
    ]}
    patcher, _ = _patch_get([_response(200, payload)])
    with patcher:
        books = BRIET.redeem("CODE")

    assert len(books) == BRIET.MAX_BOOKS
    assert len(books[0]["title"]) == BRIET.MAX_TITLE


def test_redeem_survives_a_payload_that_is_not_a_list():
    patcher, _ = _patch_get([_response(200, {"books": {"olid": "OL1M"}})])
    with patcher:
        assert BRIET.redeem("CODE") == []


def test_backoff_never_returns_a_negative_delay():
    """time.sleep() raises on a negative, so a hostile Retry-After must clamp."""
    response = _response(429, {}, headers={"Retry-After": "-10"})
    assert BRIET._backoff(response, 1) == 0.0


def test_redeem_retries_on_429_then_succeeds():
    responses = [
        _response(429, {}, headers={"Retry-After": "1"}),
        _response(200, {"books": [{"olid": "OL3M", "url": "https://cdn.test/a.epub"}]}),
    ]
    patcher, calls = _patch_get(responses)
    with patcher, patch("lenny.core.briet.time.sleep") as sleep:
        books = BRIET.redeem("CODE")

    assert calls["n"] == 2
    assert len(books) == 1
    sleep.assert_called_once_with(1.0)  # honored Retry-After, not the 2**attempt default


def test_redeem_backoff_is_capped():
    hostile = _response(503, {}, headers={"Retry-After": "99999"})
    patcher, _ = _patch_get([hostile])
    with patcher, patch("lenny.core.briet.time.sleep") as sleep:
        with pytest.raises(httpx.HTTPStatusError):
            BRIET.redeem("CODE")

    assert all(call.args[0] <= BRIET.MAX_BACKOFF for call in sleep.call_args_list)


def test_redeem_does_not_retry_a_spent_code():
    """A 4xx means the code is bad or already used — burning retries on it is pointless."""
    patcher, calls = _patch_get([_response(403, {"error": "already redeemed"})])
    with patcher, patch("lenny.core.briet.time.sleep") as sleep:
        with pytest.raises(httpx.HTTPStatusError):
            BRIET.redeem("CODE")

    assert calls["n"] == 1
    sleep.assert_not_called()


def test_redeem_gives_up_after_max_attempts():
    patcher, calls = _patch_get([_response(500)])
    with patcher, patch("lenny.core.briet.time.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            BRIET.redeem("CODE")

    assert calls["n"] == BRIET.MAX_ATTEMPTS


# --- import -----------------------------------------------------------------

def test_import_marks_books_done(db):
    books = [{"olid": 111, "url": "https://cdn.test/a.epub", "title": "A"}]
    with patch("lenny.core.briet.download_epub", return_value=io.BytesIO(EPUB_BYTES)), \
         patch("lenny.core.briet.LennyClient.upload", return_value=True) as upload:
        stats = import_briet_books(books)

    assert stats == {"uploaded": 1, "failed": 0}
    assert upload.call_args.kwargs["encrypted"] is True  # BRIET books are lendable, not open access
    records = ImportJob.list()
    assert len(records) == 1
    assert records[0]["status"] == DONE


def test_import_marks_bad_download_failed(db):
    books = [{"olid": 111, "url": "https://cdn.test/a.epub"}]
    with patch("lenny.core.briet.download_epub", return_value=io.BytesIO(b"not an epub")):
        stats = import_briet_books(books)

    assert stats == {"uploaded": 0, "failed": 1}
    assert ImportJob.list()[0]["status"] == FAILED


def test_one_bad_book_does_not_abort_the_bundle(db):
    books = [
        {"olid": 111, "url": "https://cdn.test/a.epub"},
        {"olid": 222, "url": "https://cdn.test/b.epub"},
        {"olid": 333, "url": "https://cdn.test/c.epub"},
    ]

    def flaky(url, **kwargs):
        if url.endswith("b.epub"):
            raise RuntimeError("boom")
        return io.BytesIO(EPUB_BYTES)

    with patch("lenny.core.briet.download_epub", side_effect=flaky), \
         patch("lenny.core.briet.LennyClient.upload", return_value=True):
        stats = import_briet_books(books)

    assert stats == {"uploaded": 2, "failed": 1}
    by_olid = {r["olid"]: r["status"] for r in ImportJob.list()}
    assert by_olid == {111: DONE, 222: FAILED, 333: DONE}


# --- ImportJob --------------------------------------------------------------

def test_status_transition_leaves_exactly_one_row(db):
    from lenny.core.cache import CacheEntry

    ImportJob.record("briet", 111, PENDING)
    ImportJob.record("briet", 111, DONE)

    rows = db.query(CacheEntry).filter(CacheEntry.scope == SCOPE).all()
    assert len(rows) == 1
    assert ImportJob.list() == [
        {"source": "briet", "olid": 111, "status": DONE, "error": None,
         "updated_at": rows[0].created_at.isoformat()}
    ]


def test_long_errors_are_truncated_to_fit_the_column(db):
    ImportJob.record("briet", 111, FAILED, "x" * 5000)
    assert len(ImportJob.list()[0]["error"]) == 400


# --- routes -----------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True), \
         patch("lenny.routes.api.Cache.is_throttled", return_value=False):
        yield


def test_redeem_requires_admin(client):
    assert client.post("/v1/api/briet/redeem", json={"code": "X"}).status_code in (403, 404)
    assert client.post("/v1/api/admin/briet/redeem", json={"code": "X"}).status_code == 403


def test_imports_requires_admin(client):
    assert client.get("/v1/api/admin/imports").status_code == 403


def test_redeem_queues_books_and_returns_them(client, admin_ok):
    books = [{"olid": 111, "url": "https://cdn.test/a.epub", "title": "A"}]
    with patch("lenny.routes.api.BRIET.redeem", return_value=books), \
         patch("lenny.routes.api.ImportJob.record"), \
         patch("lenny.routes.api.import_briet_books") as importer:
        response = client.post("/v1/api/admin/briet/redeem", json={"code": "GOOD"})

    assert response.status_code == 200
    assert response.json() == {"code": "GOOD", "count": 1, "books": books}
    importer.assert_called_once_with(books)  # ran as a background task, after the response


def test_redeem_rejects_empty_code(client, admin_ok):
    assert client.post("/v1/api/admin/briet/redeem", json={"code": "  "}).status_code == 400


def test_redeem_maps_spent_code_to_400(client, admin_ok):
    error = httpx.HTTPStatusError("spent", request=_response(403).request, response=_response(403))
    with patch("lenny.routes.api.BRIET.redeem", side_effect=error):
        assert client.post("/v1/api/admin/briet/redeem", json={"code": "X"}).status_code == 400


def test_redeem_maps_upstream_outage_to_502(client, admin_ok):
    error = httpx.HTTPStatusError("down", request=_response(503).request, response=_response(503))
    with patch("lenny.routes.api.BRIET.redeem", side_effect=error):
        assert client.post("/v1/api/admin/briet/redeem", json={"code": "X"}).status_code == 502


def test_redeem_is_throttled_per_code(client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True), \
         patch("lenny.routes.api.Cache.is_throttled", return_value=True):
        assert client.post("/v1/api/admin/briet/redeem", json={"code": "X"}).status_code == 429


def test_imports_lists_in_flight_books(client, admin_ok):
    records = [{"source": "briet", "olid": 111, "status": "downloading", "error": None}]
    with patch("lenny.routes.api.ImportJob.list", return_value=records):
        response = client.get("/v1/api/admin/imports")

    assert response.status_code == 200
    assert response.json() == {"imports": records}
