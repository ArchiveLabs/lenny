"""Tests for the admin loans listing endpoint and its core query helper.

Covers the HTTP plumbing and validation of GET /admin/loans without touching a
real database: the core listing functions are mocked, so these tests verify the
route's dispatch (bare array vs. wrapped response), filter/param validation, and
the core function's input guards. The DB query behaviour itself (status filters,
pagination window, total count) is exercised against SQLite separately.
"""

import os

import pytest
from unittest.mock import patch

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

HDRS = {"X-Admin-Internal-Secret": "x", "Authorization": "Bearer t"}


@pytest.fixture(scope="module")
def client():
    """TestClient that bypasses DB init — the listing helpers are mocked."""
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        yield


# ─── Response-shape dispatch ──────────────────────────────────────────────────

def test_no_params_returns_wrapped_object(client, admin_ok):
    """Always returns the wrapped {items,total,limit,offset} shape — even with
    no params — so the response shape is stable for the UI."""
    with patch("lenny.core.admin_loans.query_loans_for_admin", return_value=([{"id": 1}], 1)) as m:
        resp = client.get("/v1/api/admin/loans", headers=HDRS)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 1}], "total": 1, "limit": 500, "offset": 0}
    _, kwargs = m.call_args
    assert kwargs["status"] == "all" and kwargs["sort"] == "borrowed_at" and kwargs["order"] == "desc"


def test_status_param_wrapped_with_filters(client, admin_ok):
    """Filter/pagination params flow through to the core query."""
    with patch("lenny.core.admin_loans.query_loans_for_admin", return_value=([{"id": 9}], 42)) as m:
        resp = client.get("/v1/api/admin/loans?status=active&limit=50&offset=100", headers=HDRS)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 9}], "total": 42, "limit": 50, "offset": 100}
    _, kwargs = m.call_args
    assert kwargs["status"] == "active"
    assert kwargs["offset"] == 100


# ─── Validation (no DB: guards run before the query) ──────────────────────────

def test_invalid_status_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/loans?status=bogus", headers=HDRS)
    assert resp.status_code == 400


def test_invalid_sort_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/loans?sort=nope", headers=HDRS)
    assert resp.status_code == 400


def test_invalid_order_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/loans?order=sideways", headers=HDRS)
    assert resp.status_code == 400


def test_non_hex_user_returns_400(client, admin_ok):
    """Patron filter must be a hex prefix — also blocks LIKE-wildcard injection."""
    resp = client.get("/v1/api/admin/loans?user=ZZZ", headers=HDRS)
    assert resp.status_code == 400


def test_user_wildcard_rejected(client, admin_ok):
    resp = client.get("/v1/api/admin/loans?user=%25", headers=HDRS)  # '%'
    assert resp.status_code == 400


def test_negative_offset_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/loans?offset=-1", headers=HDRS)
    assert resp.status_code == 400


def test_requires_admin(client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=False):
        resp = client.get("/v1/api/admin/loans", headers=HDRS)
    assert resp.status_code == 403


# ─── Core helper input guards ─────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"status": "bogus"},
    {"sort": "nope"},
    {"order": "sideways"},
])
def test_query_loans_rejects_bad_inputs(kwargs):
    from lenny.core.admin_loans import query_loans_for_admin
    with pytest.raises(ValueError):
        query_loans_for_admin(**kwargs)
