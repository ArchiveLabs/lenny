"""Tests for admin item search (live OL query, no local storage/cache) and
the loan_duration_days cap enforcement on PATCH /admin/items/{book_id}.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

HDRS = {"X-Admin-Internal-Secret": "x", "Authorization": "Bearer t"}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        yield


def _mock_item(olid, encrypted=False):
    item = MagicMock()
    item.id = olid
    item.openlibrary_edition = olid
    item.encrypted = encrypted
    item.formats.name = "EPUB"
    item.created_at.isoformat.return_value = "2026-01-01T00:00:00+00:00"
    return item


def _mock_ol_record(olid, title, authors):
    rec = MagicMock()
    rec.olid = str(olid)
    rec.title = title
    rec.author_name = authors
    return rec


# ─── LennyAPI._prefix_query ──────────────────────────────────────────────────

def test_prefix_query_wildcards_only_the_last_word():
    from lenny.core.api import LennyAPI
    assert LennyAPI._prefix_query("the suit") == "the suit*"
    assert LennyAPI._prefix_query("suit") == "suit*"


def test_prefix_query_empty_string_is_unchanged():
    from lenny.core.api import LennyAPI
    assert LennyAPI._prefix_query("") == ""


def test_admin_search_applies_prefix_wildcard_to_ol_query():
    """The exact bug bob reported: 'the suit' must become 'the suit*' so a
    partially-typed title (whole-word-only OL matching otherwise) is found."""
    from lenny.core.api import LennyAPI

    all_items = {10: _mock_item(10)}
    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=[]) as mock_search:
        LennyAPI.admin_search_items(q="the suit")

    query = mock_search.call_args.kwargs["query"]
    assert query.startswith("the suit* AND")


# ─── LennyAPI.admin_search_items — core logic ────────────────────────────────

def test_admin_search_scopes_query_to_local_editions():
    from lenny.core.api import LennyAPI

    all_items = {10: _mock_item(10), 20: _mock_item(20)}
    ol_hit = _mock_ol_record(10, "Dune", ["Frank Herbert"])

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=[ol_hit]) as mock_search:
        items, ol_unavailable = LennyAPI.admin_search_items(q="dune")

    query = mock_search.call_args.kwargs["query"]
    assert "dune* AND edition_key:" in query
    assert "OL10M" in query and "OL20M" in query
    assert ol_unavailable is False
    assert items == [{
        "id": 10, "edition_key": "OL10M", "title": "Dune", "author": "Frank Herbert",
        "encrypted": False, "formats": "EPUB", "created_at": "2026-01-01T00:00:00+00:00",
        "num_lendable_total": 1, "available_copies": 1,       # the page asks for copies too; nothing on loan here
    }]


def test_admin_search_drops_hits_outside_local_editions():
    """OL can surface an edition from its wider catalog if the disjunction
    query is ever malformed — a hit for an id Lenny doesn't hold must never
    leak into the result."""
    from lenny.core.api import LennyAPI

    all_items = {10: _mock_item(10)}
    foreign_hit = _mock_ol_record(999, "Not Ours", ["Someone"])

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=[foreign_hit]):
        items, _ = LennyAPI.admin_search_items(q="anything")

    assert items == []


def test_admin_search_filters_by_encrypted():
    from lenny.core.api import LennyAPI

    all_items = {10: _mock_item(10, encrypted=True), 20: _mock_item(20, encrypted=False)}

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=[]) as mock_search:
        LennyAPI.admin_search_items(q="x", encrypted=True)

    query = mock_search.call_args.kwargs["query"]
    assert "OL10M" in query
    assert "OL20M" not in query


def test_admin_search_dedupes_and_respects_limit():
    from lenny.core.api import LennyAPI

    all_items = {i: _mock_item(i) for i in range(1, 6)}
    # Same olid returned twice — must only count once toward the limit.
    hits = [_mock_ol_record(1, "A", []), _mock_ol_record(1, "A", []), _mock_ol_record(2, "B", [])]

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=hits):
        items, _ = LennyAPI.admin_search_items(q="x", limit=1)

    assert len(items) == 1
    assert items[0]["id"] == 1


def test_admin_search_no_items_short_circuits_without_ol_call():
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_all", return_value={}), \
         patch("lenny.core.api.OpenLibrary.search") as mock_search:
        items, ol_unavailable = LennyAPI.admin_search_items(q="x")

    mock_search.assert_not_called()
    assert items == []
    assert ol_unavailable is False


def test_admin_search_ol_failure_reports_unavailable_not_empty_match():
    """The whole point of ol_unavailable: a transport failure must not look
    identical to 'we searched and found nothing'."""
    import requests
    from lenny.core.api import LennyAPI

    all_items = {10: _mock_item(10)}

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", side_effect=requests.exceptions.ConnectionError("boom")):
        items, ol_unavailable = LennyAPI.admin_search_items(q="x")

    assert items == []
    assert ol_unavailable is True


# ─── GET /admin/items/search — route ─────────────────────────────────────────

def test_search_route_requires_admin(client):
    resp = client.get("/v1/api/admin/items/search", params={"q": "x"})
    assert resp.status_code in (401, 403)


def test_search_route_requires_q(client, admin_ok):
    resp = client.get("/v1/api/admin/items/search", headers=HDRS)
    assert resp.status_code == 422


def test_search_route_rejects_blank_q(client, admin_ok):
    resp = client.get("/v1/api/admin/items/search", params={"q": "   "}, headers=HDRS)
    assert resp.status_code == 400


def test_search_route_wraps_response_shape(client, admin_ok):
    with patch("lenny.routes.api.LennyAPI.admin_search_items", return_value=([{"id": 1}], False)):
        resp = client.get("/v1/api/admin/items/search", params={"q": "dune"}, headers=HDRS)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [{"id": 1}], "total": 1, "limit": 50, "ol_unavailable": False}


def test_search_route_surfaces_ol_unavailable(client, admin_ok):
    with patch("lenny.routes.api.LennyAPI.admin_search_items", return_value=([], True)):
        resp = client.get("/v1/api/admin/items/search", params={"q": "dune"}, headers=HDRS)

    assert resp.json()["ol_unavailable"] is True


# ─── PATCH /admin/items/{book_id} — loan_duration_days cap ───────────────────

def _mock_updated_item(duration):
    item = MagicMock()
    item.openlibrary_edition = 42
    item.encrypted = False
    item.loan_duration_days = duration
    return item


def test_patch_rejects_duration_above_global_max(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": 30}, headers=HDRS
        )
    assert resp.status_code == 400
    assert "cannot exceed" in resp.json()["detail"]


def test_patch_allows_duration_at_global_max(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14), \
         patch("lenny.routes.api.LennyAPI.update_item", return_value=_mock_updated_item(14)):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": 14}, headers=HDRS
        )
    assert resp.status_code == 200


def test_patch_allows_zero_even_under_a_cap(client, admin_ok):
    """0 means unlimited on both the global setting and the per-item
    override — it is never something the cap constrains."""
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14), \
         patch("lenny.routes.api.LennyAPI.update_item", return_value=_mock_updated_item(0)):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": 0}, headers=HDRS
        )
    assert resp.status_code == 200


def test_patch_rejects_negative_duration(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": -5}, headers=HDRS
        )
    assert resp.status_code == 400
    assert "never expire" in resp.json()["detail"]


def test_patch_allows_null_to_clear_override(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14), \
         patch("lenny.routes.api.LennyAPI.update_item", return_value=_mock_updated_item(None)):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": None}, headers=HDRS
        )
    assert resp.status_code == 200


def test_patch_allows_any_positive_value_when_global_unlimited(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=0), \
         patch("lenny.routes.api.LennyAPI.update_item", return_value=_mock_updated_item(9999)):
        resp = client.patch(
            "/v1/api/admin/items/42", json={"loan_duration_days": 9999}, headers=HDRS
        )
    assert resp.status_code == 200


def test_patch_rename_does_not_run_when_another_field_is_invalid(client, admin_ok):
    """rename_item moves S3 files and commits immediately — an invalid
    loan_duration_days in the same request must reject before that, not
    permanently rename the item and then report an unrelated 400."""
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=14), \
         patch("lenny.routes.api.LennyAPI.rename_item") as mock_rename:
        resp = client.patch(
            "/v1/api/admin/items/42",
            json={"openlibrary_edition": 999, "loan_duration_days": -5},
            headers=HDRS,
        )
    assert resp.status_code == 400
    mock_rename.assert_not_called()


# ─── PATCH /admin/items/{book_id} — OLID-vs-bare-int path (production bug) ──
# The library edit page's encrypted/loan-duration toggle sends the book's
# OpenLibrary edition key (e.g. "OL62577438M"), same format the OPDS feed
# accepts. `book_id: int` used to hard-reject that with a 422 before the
# route body ever ran ("unable to find item" even though OPDS could read the
# same book). Route now parses it like DELETE always has.

def test_patch_accepts_ol_prefixed_book_id(client, admin_ok):
    with patch("lenny.routes.api.configs.get_loan_duration_days", return_value=0), \
         patch("lenny.routes.api.LennyAPI.update_item", return_value=_mock_updated_item(None)) as mock_update:
        resp = client.patch(
            "/v1/api/admin/items/OL42M", json={"encrypted": True}, headers=HDRS
        )
    assert resp.status_code == 200
    mock_update.assert_called_once_with(42, encrypted=True)


def test_patch_rejects_unparseable_book_id(client, admin_ok):
    resp = client.patch(
        "/v1/api/admin/items/not-an-olid", json={"encrypted": True}, headers=HDRS
    )
    assert resp.status_code == 400


# ─── POST /admin/items/{book_id}/reupload — same OLID-vs-int path ───────────

def test_reupload_accepts_ol_prefixed_book_id(client, admin_ok):
    with patch("lenny.routes.api.LennyAPI.reupload") as mock_reupload:
        resp = client.post(
            "/v1/api/admin/items/OL42M/reupload",
            headers=HDRS,
            data={"encrypted": "false"},
            files={"file": ("book.epub", b"fake epub bytes", "application/epub+zip")},
        )
    assert resp.status_code == 200
    assert mock_reupload.call_args.args[0] == 42


def test_reupload_rejects_unparseable_book_id(client, admin_ok):
    resp = client.post(
        "/v1/api/admin/items/not-an-olid/reupload",
        headers=HDRS,
        data={"encrypted": "false"},
        files={"file": ("book.epub", b"fake epub bytes", "application/epub+zip")},
    )
    assert resp.status_code == 400
