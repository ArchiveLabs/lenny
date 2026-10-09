"""Connected apps: the default Book Server client and the admin endpoints that
list, add, disable and re-enable OAuth2 clients (what `make oauth2-register`
does, for the admin page).
"""

import os
from unittest.mock import patch

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import (  # noqa: E402
    DEFAULT_CLIENTS, AccessToken, OAuthClient, ensure_default_clients)
from lenny.core.utils import hash_email  # noqa: E402

BASE = "/v1/api/admin/oauth2/clients"
HDRS = {"X-Admin-Internal-Secret": "x", "Authorization": "Bearer y"}


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_authorization_codes", "oauth_clients"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


@pytest.fixture
def http():
    return TestClient(app)


@pytest.fixture
def admin():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        yield


# ─── the default Book Server client ──────────────────────────────────────────

def test_a_fresh_node_gets_book_server():
    assert ensure_default_clients() == ["reader-archive-org"]
    c = OAuthClient.get("reader-archive-org")
    assert c.name == "Book Server" and c.is_confidential is False
    assert c.allows_redirect("https://reader.archive.org")
    assert not c.allows_redirect("https://reader.archive.org/evil")


def test_seeding_twice_changes_nothing():
    ensure_default_clients()
    assert ensure_default_clients() == []
    assert db.query(OAuthClient).count() == len(DEFAULT_CLIENTS)


def test_an_operator_who_disabled_it_is_not_overruled_by_a_restart():
    ensure_default_clients()
    OAuthClient.disable("reader-archive-org")
    assert ensure_default_clients() == []
    assert OAuthClient.get("reader-archive-org") is None, "must stay disabled"


def test_seeding_does_not_run_under_tests_so_other_suites_start_empty(http):
    assert db.query(OAuthClient).count() == 0


# ─── admin gate ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", BASE), ("post", BASE),
    ("post", f"{BASE}/reader-archive-org/disable"),
    ("post", f"{BASE}/reader-archive-org/enable"),
])
def test_attack_every_endpoint_needs_the_admin_pair(http, method, path):
    r = http.request(method.upper(), path, json={"name": "x", "redirect_uris": ["https://a.example.org/cb"]})
    assert r.status_code in (401, 403)
    assert db.query(OAuthClient).count() == 0


# ─── list ────────────────────────────────────────────────────────────────────

def test_list_shows_clients_scopes_and_never_a_secret(http, admin):
    ensure_default_clients()
    OAuthClient.register(name="Server App", redirect_uris=["https://a.example.org/cb"])
    r = http.get(BASE, headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    names = {c["name"]: c for c in body["clients"]}
    assert names["Book Server"]["is_default"] is True
    assert names["Server App"]["is_default"] is False
    assert names["Server App"]["is_confidential"] is True
    assert {s["name"] for s in body["available_scopes"]} == {"loans:read", "borrow"}
    assert "secret" not in r.text.lower().replace("client_secret", "")  # no hash/secret fields
    assert all("client_secret" not in c for c in body["clients"])


def test_list_tells_an_app_developer_how_to_connect_to_this_node(http, admin):
    """The admin screen shows these so a developer knows what to point at."""
    conn = http.get(BASE, headers=HDRS).json()["connection"]
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert conn["issuer"] == meta["issuer"]
    assert conn["authorization_endpoint"] == meta["authorization_endpoint"]
    assert conn["token_endpoint"] == meta["token_endpoint"]
    assert conn["revocation_endpoint"] == meta["revocation_endpoint"]
    assert conn["discovery_url"] == f"{meta['issuer']}/.well-known/oauth-authorization-server"
    assert conn["pkce_method"] == "S256"


# ─── register ────────────────────────────────────────────────────────────────

def test_register_a_public_app_with_a_chosen_id(http, admin):
    r = http.post(BASE, headers=HDRS, json={
        "name": "Other Reader", "client_id": "other-reader",
        "redirect_uris": ["https://other.example.org/callback"], "public": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"] == "other-reader" and body["client_secret"] is None
    assert OAuthClient.get("other-reader").allows_redirect("https://other.example.org/callback")


def test_register_a_confidential_app_returns_its_secret_once(http, admin):
    r = http.post(BASE, headers=HDRS, json={
        "name": "Server App", "redirect_uris": ["https://srv.example.org/cb"], "public": False})
    assert r.status_code == 201
    secret = r.json()["client_secret"]
    assert secret and OAuthClient.get(r.json()["client_id"]).verify_secret(secret)
    again = http.get(BASE, headers=HDRS)
    assert secret not in again.text, "the secret is shown once and never again"


def test_register_accepts_a_pasted_list_of_uris(http, admin):
    r = http.post(BASE, headers=HDRS, json={
        "name": "Two", "redirect_uris": "https://a.example.org/cb, https://b.example.org/cb"})
    assert r.status_code == 201 and len(r.json()["redirect_uris"]) == 2


def test_register_limits_scopes(http, admin):
    r = http.post(BASE, headers=HDRS, json={
        "name": "Reader only", "redirect_uris": ["https://a.example.org/cb"], "scopes": ["loans:read"]})
    assert r.json()["scopes"] == ["loans:read"]


@pytest.mark.parametrize("payload,why", [
    ({"redirect_uris": ["https://a.example.org/cb"]}, "no name"),
    ({"name": "x"}, "no redirect"),
    ({"name": "x", "redirect_uris": []}, "empty redirect list"),
    ({"name": "x", "redirect_uris": ["http://evil.example.org/cb"]}, "plain http is not allowed"),
    ({"name": "x", "redirect_uris": ["javascript:alert(1)"]}, "dangerous scheme"),
    ({"name": "x", "redirect_uris": ["https://a.example.org/cb"], "scopes": ["admin:all"]}, "unknown scope"),
    ({"name": "x", "redirect_uris": ["https://a.example.org/cb"], "client_id": "a b"}, "bad id"),
    ({"name": "x", "redirect_uris": ["https://a.example.org/cb"], "public": "yes"}, "public must be a boolean"),
])
def test_attack_bad_registrations_are_refused_and_create_nothing(http, admin, payload, why):
    r = http.post(BASE, headers=HDRS, json=payload)
    assert r.status_code == 400, why
    assert db.query(OAuthClient).count() == 0


def test_attack_a_taken_id_cannot_be_registered_again(http, admin):
    ensure_default_clients()
    r = http.post(BASE, headers=HDRS, json={
        "name": "Imposter", "client_id": "reader-archive-org",
        "redirect_uris": ["https://evil.example.org/cb"]})
    assert r.status_code == 400
    assert OAuthClient.get("reader-archive-org").allows_redirect("https://reader.archive.org")


# ─── disable / enable ────────────────────────────────────────────────────────

def test_disable_stops_the_app_and_revokes_its_tokens(http, admin):
    ensure_default_clients()
    access, _r, _row = AccessToken.issue(
        client_id="reader-archive-org", patron_email_hash=hash_email("p@example.org"),
        scope="loans:read")
    r = http.post(f"{BASE}/reader-archive-org/disable", headers=HDRS)
    assert r.status_code == 200 and r.json()["revoked_tokens"] == 1
    assert OAuthClient.get("reader-archive-org") is None
    assert AccessToken.authenticate(access) is None


def test_enable_brings_the_app_back_but_not_its_old_tokens(http, admin):
    ensure_default_clients()
    access, _r, _row = AccessToken.issue(
        client_id="reader-archive-org", patron_email_hash=hash_email("p@example.org"),
        scope="loans:read")
    http.post(f"{BASE}/reader-archive-org/disable", headers=HDRS)
    r = http.post(f"{BASE}/reader-archive-org/enable", headers=HDRS)
    assert r.status_code == 200
    assert OAuthClient.get("reader-archive-org") is not None
    assert AccessToken.authenticate(access) is None, "old tokens stay revoked"


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_unknown_client_is_a_404(http, admin, action):
    assert http.post(f"{BASE}/no-such-app/{action}", headers=HDRS).status_code == 404


# ─── edit ────────────────────────────────────────────────────────────────────

def _app(client_id="fix-me", **kw):
    c, secret = OAuthClient.register(
        name=kw.pop("name", "Fix Me"), redirect_uris=kw.pop("redirect_uris", ["https://a.example.org/cb"]),
        client_id=client_id, **kw)
    return c, secret


def _token(client_id, scope="loans:read borrow"):
    return AccessToken.issue(client_id=client_id, patron_email_hash=hash_email("p@example.org"), scope=scope)[0]


def test_edit_fixes_name_and_redirect_urls(http, admin):
    _app()
    r = http.patch(f"{BASE}/fix-me", headers=HDRS, json={
        "name": "Fixed", "redirect_uris": ["https://b.example.org/cb", "https://c.example.org/cb"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Fixed" and body["revoked_tokens"] == 0
    c = OAuthClient.get("fix-me")
    assert c.allows_redirect("https://b.example.org/cb") and c.allows_redirect("https://c.example.org/cb")
    assert not c.allows_redirect("https://a.example.org/cb"), "the old URL must stop being accepted"


def test_edit_does_not_touch_a_field_that_was_not_sent(http, admin):
    _app(scopes=["loans:read"])
    http.patch(f"{BASE}/fix-me", headers=HDRS, json={"name": "Only the name"})
    c = OAuthClient.get("fix-me")
    assert c.allowed_scopes() == {"loans:read"} and c.allows_redirect("https://a.example.org/cb")


def test_attack_taking_a_permission_away_revokes_tokens_that_carry_it(http, admin):
    """Otherwise 'I removed borrow' would stay untrue until the token expired."""
    _app()
    wide = _token("fix-me", "loans:read borrow")
    r = http.patch(f"{BASE}/fix-me", headers=HDRS, json={"scopes": ["loans:read"]})
    assert r.status_code == 200 and r.json()["revoked_tokens"] == 1
    assert AccessToken.authenticate(wide) is None


def test_adding_a_permission_or_changing_a_name_keeps_existing_tokens(http, admin):
    _app(scopes=["loans:read"])
    tok = _token("fix-me", "loans:read")
    r = http.patch(f"{BASE}/fix-me", headers=HDRS, json={"scopes": ["loans:read", "borrow"], "name": "Wider"})
    assert r.json()["revoked_tokens"] == 0
    assert AccessToken.authenticate(tok) is not None


@pytest.mark.parametrize("payload", [
    {},
    {"name": ""}, {"name": "x" * 101}, {"name": 5},
    {"redirect_uris": []}, {"redirect_uris": ["http://evil.example.org/cb"]},
    {"redirect_uris": ["https://good.example.org@evil.example.org/cb"]},
    {"redirect_uris": [f"https://a{i}.example.org/cb" for i in range(11)]},
    {"scopes": []}, {"scopes": ["admin:all"]}, {"scopes": "borrow"},
])
def test_attack_bad_edits_are_refused_and_change_nothing(http, admin, payload):
    _app()
    r = http.patch(f"{BASE}/fix-me", headers=HDRS, json=payload)
    assert r.status_code == 400
    c = OAuthClient.get("fix-me")
    assert c.name == "Fix Me" and c.allows_redirect("https://a.example.org/cb")


def test_the_id_and_the_type_cannot_be_edited(http, admin):
    _app()
    r = http.patch(f"{BASE}/fix-me", headers=HDRS, json={"client_id": "stolen", "is_confidential": False, "name": "ok"})
    assert r.status_code == 200
    assert OAuthClient.get("fix-me").is_confidential is True and OAuthClient.get("stolen") is None


def test_edit_unknown_client_is_a_404(http, admin):
    assert http.patch(f"{BASE}/nope", headers=HDRS, json={"name": "x"}).status_code == 404


# ─── reset secret ────────────────────────────────────────────────────────────

def test_reset_secret_gives_a_new_one_once_and_kills_the_old(http, admin):
    _, old = _app()
    r = http.post(f"{BASE}/fix-me/rotate-secret", headers=HDRS)
    assert r.status_code == 200
    new = r.json()["client_secret"]
    c = OAuthClient.get("fix-me")
    assert new and new != old and c.verify_secret(new) and not c.verify_secret(old)
    assert new not in http.get(BASE, headers=HDRS).text, "shown once, never listed"


def test_a_public_app_has_no_secret_to_reset(http, admin):
    _app("public-one", is_confidential=False)
    r = http.post(f"{BASE}/public-one/rotate-secret", headers=HDRS)
    assert r.status_code == 400 and "no secret" in r.json()["detail"]


# ─── remove ──────────────────────────────────────────────────────────────────

def test_remove_needs_the_app_to_be_off_first(http, admin):
    _app()
    r = http.delete(f"{BASE}/fix-me", headers=HDRS)
    assert r.status_code == 409 and "turn the app off" in r.json()["detail"]
    assert OAuthClient.get("fix-me") is not None


def test_remove_deletes_the_app_its_tokens_and_codes(http, admin):
    _app()
    tok = _token("fix-me")
    http.post(f"{BASE}/fix-me/disable", headers=HDRS)
    r = http.delete(f"{BASE}/fix-me", headers=HDRS)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert OAuthClient.find("fix-me") is None
    assert AccessToken.authenticate(tok) is None
    assert db.query(AccessToken).count() == 0


def test_attack_a_removed_id_that_is_registered_again_inherits_nothing(http, admin):
    _app()
    tok = _token("fix-me")
    http.post(f"{BASE}/fix-me/disable", headers=HDRS)
    http.delete(f"{BASE}/fix-me", headers=HDRS)
    _app("fix-me", redirect_uris=["https://new-owner.example.org/cb"])
    assert AccessToken.authenticate(tok) is None, "the old owner's token must not work for the new one"


def test_attack_a_built_in_app_cannot_be_removed_even_when_off(http, admin):
    ensure_default_clients()
    http.post(f"{BASE}/reader-archive-org/disable", headers=HDRS)
    r = http.delete(f"{BASE}/reader-archive-org", headers=HDRS)
    assert r.status_code == 409 and "built-in" in r.json()["detail"]
    assert OAuthClient.find("reader-archive-org") is not None


def test_remove_unknown_client_is_a_404(http, admin):
    assert http.delete(f"{BASE}/nope", headers=HDRS).status_code == 404


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", f"{BASE}/fix-me", {"name": "x"}),
    ("POST", f"{BASE}/fix-me/rotate-secret", None),
    ("DELETE", f"{BASE}/fix-me", None),
])
def test_attack_edit_reset_and_remove_need_the_admin_pair(http, method, path, body):
    _app()
    r = http.request(method, path, json=body)
    assert r.status_code in (401, 403)
    c = OAuthClient.get("fix-me")
    assert c is not None and c.name == "Fix Me"


# ─── the public API documentation must not map the admin surface ─────────────

def test_the_published_schema_leaves_out_every_admin_route(http):
    for url in ("/openapi.json", "/v1/api/openapi.json"):
        r = http.get(url)
        if r.status_code != 200:
            continue
        paths = list(r.json()["paths"])
        assert paths, "the public API should still be documented"
        leaked = [p for p in paths if "/admin" in p or p.endswith("/upload")]
        assert leaked == [], f"admin routes published at {url}: {leaked}"
        assert any(p.endswith("/oauth2/authorize") for p in paths), "public OAuth endpoints stay documented"


def test_leaving_a_route_out_of_the_docs_does_not_open_or_close_it(http):
    """The schema is documentation only; the gate is the route's own check."""
    assert http.get(BASE).status_code in (401, 403), "still closed to a stranger"
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        assert http.get(BASE, headers=HDRS).status_code == 200, "still open to the admin"
