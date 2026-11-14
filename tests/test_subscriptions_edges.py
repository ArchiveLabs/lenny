"""Edge cases for members-only access with Ghost: the boundaries test_subscriptions.py leaves open.

Grouped by the trust boundary each one guards: the entitlement token, the launch,
the member cookie, Ghost's webhooks, admin settings, access-mode changes, stats,
and the promise that a member's email is never stored.
"""
import base64
import datetime
import json
import time
import types

import pytest
import sqlalchemy

from tests.test_subscriptions import (  # noqa: F401  (env is an autouse fixture)
    EDITION, ISS, LAUNCH, ME, SECRET, SITE, WEBHOOK, JWKS, admin, client, env, lease, make_item, signed, token,
)
from lenny.core import auth, ghost
from lenny.core.db import session as db
from lenny.core.models import FormatEnum, Item, Loan, Subscription, SubscriptionRead
from lenny.core.utils import hash_email

NOW = lambda: datetime.datetime.now(datetime.timezone.utc)  # noqa: E731
JSON = {"accept": "application/json"}


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _launch(c, book=EDITION, **claims):
    return c.post(LAUNCH, data={"token": token(**claims), "book": str(book)}, follow_redirects=False)


def _read(c, book=EDITION, **kw):
    return c.get(f"/v1/api/items/{book}/read", headers=JSON, follow_redirects=False, **kw)


def _settings_writes_to_env(monkeypatch):
    """Admin saves land in the process env, which is where ghost._setting falls back to."""
    written = {}

    def write(path, updates):
        written.update(updates)
        for k, v in updates.items():
            monkeypatch.setenv(k, v)
    monkeypatch.setattr("lenny.core.ol_bootstrap.update_env_file", write)
    return written


def _lendable(edition=EDITION + 1):
    item = Item(openlibrary_edition=edition, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    return item


# ── the entitlement token ────────────────────────────────────────────────────

@pytest.mark.parametrize("make, code", [
    # alg "none": no signature at all.
    (lambda: f"{_b64({'alg': 'none'})}.{_b64({'sub': ME, 'iss': ISS, 'aud': ISS, 'paid': True, 'jti': 'n', 'scope': 'members:entitlements:read', 'exp': int(time.time()) + 300})}.", "invalid_token"),  # noqa: E501
    (lambda: token(alg="RS256"), "invalid_token"),        # right key, algorithm Ghost doesn't use
    (lambda: token(paid="true"), "not_paid"),             # only a JSON true counts
    (lambda: token(paid=1), "not_paid"),
    (lambda: "a" * 9000, "invalid_token"),                # oversized: refused before parsing
    (lambda: token(sub=None), "invalid_token"),
    (lambda: token(exp=None), "invalid_token"),
    (lambda: token(iss=None), "invalid_token"),
    (lambda: token(aud=None), "invalid_token"),
    (lambda: token(iat=int(time.time()) + 3600), "invalid_token"),  # minted in the future
    (lambda: token(iss=f"{SITE}/members/api/", aud=f"{SITE}/members/api/"), "invalid_token"),  # near-miss issuer
])
def test_more_bad_tokens(make, code):
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(make())
    assert e.value.code == code


def test_small_clock_skew_is_tolerated_but_not_more():
    ghost.verify_entitlement(token(exp=int(time.time()) - 20))   # within the 30 s leeway
    with pytest.raises(ghost.GhostError):
        ghost.verify_entitlement(token(exp=int(time.time()) - 60))


def test_replay_check_fails_closed_when_the_cache_is_down(monkeypatch):
    import lenny.core.cache as cache

    def broken(*a, **k):
        raise sqlalchemy.exc.OperationalError("select", {}, Exception("db down"))
    monkeypatch.setattr(cache.db, "query", broken)
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(token())
    assert e.value.code == "token_reused"


def test_tier_allowlist_trims_and_needs_any_overlap(monkeypatch):
    monkeypatch.setenv(ghost.TIERS_KEY, " t-gold , t-paid ")
    assert ghost.allowed_tiers() == ["t-gold", "t-paid"]
    assert ghost.verify_entitlement(token(active_tier_ids=["t-other", "t-paid"]))
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(token(active_tier_ids=[]))
    assert e.value.code == "tier_not_allowed"


# ── the launch ───────────────────────────────────────────────────────────────

def test_launch_is_post_only_so_the_token_never_rides_in_a_url():
    make_item()
    assert client().get(f"{LAUNCH}?token={token()}&book={EDITION}").status_code == 405
    assert db.query(Subscription).count() == 0


@pytest.mark.parametrize("book", ["", "abc", "../1", "OL", "OLxM", "-5", "1; drop table items"])
def test_launch_refuses_junk_book_ids(book):
    make_item()
    r = client().post(LAUNCH, data={"token": token(), "book": book}, follow_redirects=False)
    assert r.status_code == 404 and "set-cookie" not in r.headers
    assert db.query(Subscription).count() == 0


@pytest.mark.parametrize("book", [str(EDITION), f"OL{EDITION}M"])
def test_launch_accepts_both_id_forms(book):
    make_item()
    r = client().post(LAUNCH, data={"token": token(), "book": book}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/v1/api/items/{EDITION}/read"


def test_launch_cookie_is_locked_down_and_never_echoes_the_token():
    make_item()
    t = token()
    r = client().post(LAUNCH, data={"token": t, "book": str(EDITION)}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.MEMBER_COOKIE}=") and "session=" not in cookie
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag.lower() in cookie.lower()
    assert t not in cookie and t not in r.headers["location"] and t not in r.text


def test_launch_without_a_ghost_site_explains_and_grants_nothing(monkeypatch):
    make_item()
    monkeypatch.setenv(ghost.URL_KEY, "")
    r = _launch(client())
    assert r.status_code == 403 and "connected to a publisher" in r.text and "set-cookie" not in r.headers


def test_launching_again_renews_one_pass_and_refreshes_tiers():
    make_item()
    c = client()
    _launch(c)
    db.query(Subscription).update({"expires_at": NOW() + datetime.timedelta(minutes=5)})
    db.commit()
    _launch(c, active_tier_ids=["t-gold"])
    db.expire_all()
    row = db.query(Subscription).one()
    assert row.tier_ids == "t-gold"
    assert row.expires_at.replace(tzinfo=datetime.timezone.utc) > NOW() + datetime.timedelta(hours=23)


# ── the member cookie ────────────────────────────────────────────────────────

def _member_client(patron=None, ip="testclient"):
    c = client()
    c.cookies.set(auth.MEMBER_COOKIE, auth.create_member_cookie(patron or hash_email(ME), ip))
    return c


def test_member_cookie_positive_control():
    make_item()
    lease()
    assert _read(_member_client()).status_code == 307


def test_tampered_member_cookie_is_refused():
    make_item()
    lease()
    good = auth.create_member_cookie(hash_email(ME), "testclient")
    c = client()
    c.cookies.set(auth.MEMBER_COOKIE, good[:-2] + ("AA" if not good.endswith("AA") else "BB"))
    assert _read(c).status_code == 403


def test_member_cookie_is_bound_to_the_ip_it_was_issued_to():
    make_item()
    lease()
    assert _read(_member_client(ip="10.9.9.9")).status_code == 403


def test_member_cookie_expires(monkeypatch):
    import itsdangerous.timed
    make_item()
    lease()
    old = time.time() - auth.COOKIE_TTL - 60
    with monkeypatch.context() as m:
        m.setattr(itsdangerous.timed, "time", types.SimpleNamespace(time=lambda: old))
        stale = auth.create_member_cookie(hash_email(ME), "testclient")
    c = client()
    c.cookies.set(auth.MEMBER_COOKIE, stale)
    assert _read(c).status_code == 403


def test_a_member_cookie_only_carries_its_own_member():
    make_item()
    Subscription.grant("ghost", hash_email("someone-else@example.com"), "m-9", ["t-paid"])
    assert _read(_member_client()).status_code == 403   # ME has no pass of their own


def test_a_session_cookie_is_never_read_as_a_member_cookie():
    make_item()
    lease()
    c = client()
    c.cookies.set(auth.MEMBER_COOKIE, auth.create_hashed_session_cookie(hash_email(ME), "testclient"))
    assert _read(c).status_code == 403


def test_a_library_login_with_a_pass_still_reads_members_only_books():
    make_item()
    lease()
    c = client()
    c.cookies.set("session", auth.create_session_cookie(ME, "testclient"))
    assert _read(c).status_code == 307


def test_member_cookie_as_a_bearer_token_proves_nothing():
    make_item()
    _lendable()
    lease()
    member = auth.create_member_cookie(hash_email(ME), "testclient")
    bearer = {"authorization": f"Bearer {member}", **JSON}
    c = client()
    assert c.get(f"/v1/api/items/{EDITION}/read", headers=bearer, follow_redirects=False).status_code == 403
    c.post(f"/v1/api/items/{EDITION + 1}/borrow", headers=bearer, follow_redirects=False)
    assert db.query(Loan).count() == 0


def test_a_members_only_book_cannot_be_borrowed_through_the_route():
    make_item()
    c = client()
    c.cookies.set("session", auth.create_session_cookie(ME, "testclient"))
    c.post(f"/v1/api/items/{EDITION}/borrow", headers=JSON, follow_redirects=False)
    assert db.query(Loan).count() == 0


def test_reading_responses_for_members_only_books_are_never_cached():
    from lenny.core.readium import ReadiumAPI
    make_item()
    lease()
    c = _member_client()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(ReadiumAPI, "get_manifest", classmethod(lambda cls, *a: {"metadata": {}}))
        r = c.get(f"/v1/api/items/{EDITION}/readium/manifest.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"


def test_nginx_door_follows_the_member_cookie():
    make_item()
    lease()
    assert _member_client().get(f"/v1/api/items/{EDITION}/access").status_code == 204
    assert _member_client(ip="10.9.9.9").get(f"/v1/api/items/{EDITION}/access").status_code == 403


# ── Ghost's webhooks ─────────────────────────────────────────────────────────

def _deliver(payload, **kw):
    body, headers = signed(payload, **kw)
    return client().post(WEBHOOK, content=body, headers=headers)


@pytest.mark.parametrize("payload", [
    [1], "member", 5, None, {}, {"member": "x"}, {"member": {"current": "x"}}, {"member": {"previous": [1]}},
])
def test_webhook_with_an_unexpected_shape_is_a_400_not_a_crash(payload):
    lease()
    r = _deliver(payload)
    assert r.status_code == 400 and r.json()["code"] == "bad_payload"
    assert Subscription.is_active(hash_email(ME))


def test_webhook_body_that_is_not_json():
    body = b"not json"
    ts = str(int(time.time() * 1000))
    import hashlib
    import hmac
    sig = hmac.new(SECRET.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
    r = client().post(WEBHOOK, content=body, headers={"x-ghost-signature": f"sha256={sig}, t={ts}"})
    assert r.status_code == 400 and r.json()["code"] == "bad_payload"


@pytest.mark.parametrize("header", ["garbage", "sha256=", "t=123", "sha256=abc, t=notanumber", "sha256=abc,t=",
                                    "=, =", "sha256=" + "0" * 64 + ", t=" + "9" * 30])
def test_malformed_signature_headers_are_401_not_500(header):
    lease()
    r = client().post(WEBHOOK, content=b"{}", headers={"x-ghost-signature": header})
    assert r.status_code == 401


def test_webhook_from_the_future_is_refused():
    lease()
    r = _deliver({"member": {"current": {}, "previous": {"uuid": "m-1"}}},
                 ts=int(time.time() * 1000) + 10 * 60 * 1000)
    assert r.status_code == 401 and Subscription.is_active(hash_email(ME))


def test_webhook_without_a_secret_is_404_never_410(monkeypatch):
    monkeypatch.setenv(ghost.SECRET_KEY, "")
    r = _deliver({"member": {"current": {}, "previous": {"uuid": "m-1"}}})
    assert r.status_code == 404 and r.json()["code"] == "not_configured"


@pytest.mark.parametrize("status", ["paid", "comped", "gift"])
def test_paying_statuses_keep_access(status):
    lease()
    r = _deliver({"member": {"current": {"uuid": "m-1", "email": ME, "status": status}, "previous": {"status": "paid"}}})
    assert r.json()["revoked"] == 0 and Subscription.is_active(hash_email(ME))


def test_email_change_retires_only_the_old_address():
    new = "new-address@example.com"
    lease()
    Subscription.grant("ghost", hash_email(new), "m-1", ["t-paid"])
    r = _deliver({"member": {"current": {"uuid": "m-1", "email": new, "status": "paid"},
                             "previous": {"email": ME}}})
    assert r.json()["revoked"] == 1
    assert not Subscription.is_active(hash_email(ME)) and Subscription.is_active(hash_email(new))


def test_email_change_by_case_only_is_not_a_change():
    lease()
    r = _deliver({"member": {"current": {"uuid": "m-1", "email": ME.upper(), "status": "paid"},
                             "previous": {"email": ME}}})
    assert r.json()["revoked"] == 0


def test_cancel_finds_the_pass_by_ghost_id_even_after_an_email_change():
    Subscription.grant("ghost", hash_email("address-ghost-no-longer-sends@example.com"), "m-1", ["t-paid"])
    r = _deliver({"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}})
    assert r.json()["revoked"] == 1 and db.query(Subscription).count() == 0


@pytest.mark.parametrize("tiers, revoked", [
    ([{"id": "t-paid"}], 1),           # moved off the only allowed tier
    ([{"id": "t-gold"}], 0),
    ("t-gold", 0),                     # not a list: ignore rather than guess
])
def test_tier_changes_under_an_allowlist(monkeypatch, tiers, revoked):
    monkeypatch.setenv(ghost.TIERS_KEY, "t-gold")
    lease()
    r = _deliver({"member": {"current": {"uuid": "m-1", "email": ME, "status": "paid", "tiers": tiers},
                             "previous": {"tiers": []}}})
    assert r.json()["revoked"] == revoked


def test_tiers_absent_from_the_event_keep_access_under_an_allowlist(monkeypatch):
    monkeypatch.setenv(ghost.TIERS_KEY, "t-gold")
    lease()
    r = _deliver({"member": {"current": {"uuid": "m-1", "email": ME, "status": "paid"}, "previous": {"name": "x"}}})
    assert r.json()["revoked"] == 0


def test_webhooks_keep_working_while_reading_is_switched_off(monkeypatch):
    monkeypatch.setenv(ghost.ENABLED_KEY, "false")
    lease()
    r = _deliver({"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}})
    assert r.status_code == 200 and r.json()["revoked"] == 1


def test_redelivery_is_harmless():
    lease()
    payload = {"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}}
    assert _deliver(payload).json()["revoked"] == 1
    r = _deliver(payload)
    assert r.status_code == 200 and r.json()["revoked"] == 0


def test_last_webhook_only_counts_verified_deliveries(admin):
    settings = lambda: client().get("/v1/api/admin/settings/ghost").json()  # noqa: E731
    assert settings()["last_webhook_at"] is None
    client().post(WEBHOOK, content=b"{}", headers={"x-ghost-signature": "sha256=00, t=1"})
    assert settings()["last_webhook_at"] is None
    _deliver({"member": {"current": {"uuid": "m-1", "email": ME, "status": "paid"}, "previous": {}}})
    assert settings()["last_webhook_at"] is not None


# ── admin settings ───────────────────────────────────────────────────────────

ADMIN_ROUTES = [
    ("get", "/v1/api/admin/settings/ghost"),
    ("put", "/v1/api/admin/settings/ghost"),
    ("post", "/v1/api/admin/settings/ghost/webhook-secret"),
    ("get", "/v1/api/admin/stats/subscriptions"),
    ("get", "/v1/api/admin/stats/subscriptions/books"),
    ("get", "/v1/api/admin/subscriptions"),
    ("delete", "/v1/api/admin/subscriptions/1"),
    ("get", "/v1/api/admin/dashboard"),
    ("get", "/v1/api/admin/stats/loans"),
    ("patch", f"/v1/api/admin/items/{EDITION}"),
    ("get", f"/v1/api/admin/loans?item=OL{EDITION}M"),
]


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_every_admin_route_needs_an_admin(method, path):
    lease()
    make_item()
    kw = {"json": {"enabled": False, "access_mode": "open"}} if method in ("put", "patch") else {}
    r = getattr(client(), method)(path, **kw)
    assert r.status_code in (401, 403), (path, r.status_code)
    assert SECRET not in r.text
    assert Subscription.is_active(hash_email(ME)) and Item.exists(EDITION).subscription


@pytest.mark.parametrize("body, code", [
    ([1, 2], "bad_request"),
    ({}, "bad_request"),
    ({"site_url": 5}, "invalid_site_url"),
    ({"allowed_tier_ids": "t-paid"}, "invalid_tiers"),
    ({"allowed_tier_ids": [1]}, "invalid_tiers"),
    ({"allowed_tier_ids": ["a,b"]}, "invalid_tiers"),
    ({"allowed_tier_ids": [" "]}, "invalid_tiers"),
    ({"enabled": "false"}, "invalid_enabled"),
])
def test_settings_validation(admin, monkeypatch, body, code):
    written = _settings_writes_to_env(monkeypatch)
    r = client().put("/v1/api/admin/settings/ghost", json=body)
    assert r.status_code == 422 and r.json()["code"] == code and written == {}


@pytest.mark.parametrize("url", [
    "https://e.com/</script><script>alert(1)",
    'https://e.com/a"b',
    "https://e .com",
    "https://e.com:99999",
    "https://e.com/?x=1",
    "https://e.com/#x",
    "https://user@e.com",
    "http://e.com",
    "javascript:alert(1)",
    "https://",
])
def test_site_urls_that_could_break_the_snippet_are_refused(url):
    with pytest.raises(ghost.GhostError):
        ghost.normalize_site_url(url)


@pytest.mark.parametrize("url, expected", [
    ("https://example.com/", "https://example.com"),
    ("  https://example.com  ", "https://example.com"),
    ("https://example.com/blog/", "https://example.com/blog"),
    ("https://sub.example.co.uk:8443", "https://sub.example.co.uk:8443"),
])
def test_site_urls_that_are_fine(url, expected):
    assert ghost.normalize_site_url(url) == expected


def test_snippet_cannot_be_closed_early_by_its_urls():
    s = ghost.snippet("https://lenny.example/</script><script>alert(1)//", "https://example.com")
    assert s.count("</script>") == 1 and s.endswith("</script>")


def test_clearing_the_site_disconnects_and_ends_every_pass(admin, monkeypatch):
    _settings_writes_to_env(monkeypatch)
    lease()
    r = client().put("/v1/api/admin/settings/ghost", json={"site_url": ""})
    assert r.status_code == 200 and r.json()["site_url"] is None and r.json()["snippet"] is None
    assert db.query(Subscription).count() == 0
    assert _deliver({"member": {"current": {}, "previous": {"uuid": "m-1"}}}).status_code == 404


def test_first_site_save_creates_a_secret_and_later_saves_keep_it(admin, monkeypatch):
    monkeypatch.setattr(ghost, "fetch_jwks", lambda site: JWKS)
    monkeypatch.setenv(ghost.SECRET_KEY, "")
    written = _settings_writes_to_env(monkeypatch)
    client().put("/v1/api/admin/settings/ghost", json={"site_url": SITE})
    first = written.get(ghost.SECRET_KEY)
    assert first and len(first) >= 32
    written.clear()
    client().put("/v1/api/admin/settings/ghost", json={"site_url": SITE})
    assert ghost.SECRET_KEY not in written and ghost.webhook_secret() == first


@pytest.mark.parametrize("code", ["ghost_unreachable", "ghost_no_jwks"])
def test_a_site_that_isnt_ghost_is_refused_and_nothing_is_saved(admin, monkeypatch, code):
    def fail(site):
        raise ghost.GhostError(code)
    monkeypatch.setattr(ghost, "fetch_jwks", fail)
    written = _settings_writes_to_env(monkeypatch)
    lease()
    r = client().put("/v1/api/admin/settings/ghost", json={"site_url": "https://other.example"})
    assert r.status_code == 422 and r.json()["code"] == code
    assert written == {} and Subscription.is_active(hash_email(ME))


def test_a_failed_save_keeps_every_pass(admin, monkeypatch):
    monkeypatch.setattr(ghost, "fetch_jwks", lambda site: JWKS)

    def disk_full(path, updates):
        raise OSError("disk full")
    monkeypatch.setattr("lenny.core.ol_bootstrap.update_env_file", disk_full)
    lease()
    r = client().put("/v1/api/admin/settings/ghost", json={"site_url": "https://other.example"})
    assert r.status_code == 500 and r.json()["code"] == "env_write_failed"
    assert Subscription.is_active(hash_email(ME))


def test_rotating_the_secret_retires_the_old_one_at_once(admin, monkeypatch):
    _settings_writes_to_env(monkeypatch)
    lease()
    new = client().post("/v1/api/admin/settings/ghost/webhook-secret").json()["webhook_secret"]
    assert new and new != SECRET
    payload = {"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}}
    assert _deliver(payload).status_code == 401                 # Ghost still on the old secret
    assert _deliver(payload, secret=new).json()["revoked"] == 1


def test_switch_off_also_refuses_a_library_login_with_a_pass(monkeypatch):
    make_item()
    lease()
    monkeypatch.setenv(ghost.ENABLED_KEY, "false")
    c = client()
    c.cookies.set("session", auth.create_session_cookie(ME, "testclient"))
    r = _read(c)
    assert r.status_code == 403 and r.json()["reason"] == "disabled"
    assert c.get(f"/v1/api/items/{EDITION}/access").status_code == 403


# ── access modes and loans ───────────────────────────────────────────────────

@pytest.mark.parametrize("access_mode, encrypted, flags", [
    (None, None, (False, False)),               # nothing named: open
    ("subscription", None, (False, True)),
    ("borrowable", None, (True, False)),
    (None, True, (True, False)),                # legacy flag still works
    (None, False, (False, False)),
])
def test_upload_access_flags(access_mode, encrypted, flags):
    from lenny.routes.api import _access_flags
    assert _access_flags(access_mode, encrypted) == flags


def test_upload_rejects_an_unknown_access_mode():
    from fastapi import HTTPException
    from lenny.routes.api import _access_flags
    with pytest.raises(HTTPException) as e:
        _access_flags("secret", None)
    assert e.value.status_code == 422


def _loans(item, **states):
    rows = {}
    for name, (due, returned) in states.items():
        rows[name] = Loan(item_id=item.id, patron_email_hash=hash_email(f"{name}@example.com"),
                          due_date=due, returned_at=returned)
    db.add_all(rows.values())
    db.commit()
    return rows


def test_only_active_loans_are_returned_on_a_mode_change(admin):
    item = make_item(subscription=False, encrypted=True)
    day = datetime.timedelta(days=1)
    loans = _loans(item, active=(NOW() + day, None), endless=(None, None),
                   overdue=(NOW() - day, None), done=(NOW() + day, NOW() - day))
    done_at = loans["done"].returned_at
    r = client().patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "open"})
    assert r.json()["loans_returned"] == 2
    db.expire_all()
    assert loans["active"].returned_at is not None and loans["endless"].returned_at is not None
    assert loans["overdue"].returned_at is None                    # already ended on its own
    assert loans["done"].returned_at.replace(tzinfo=None) == done_at.replace(tzinfo=None)


def test_moving_to_borrowable_keeps_loans_and_clears_members_only(admin):
    item = make_item()
    _loans(item, active=(NOW() + datetime.timedelta(days=1), None))
    r = client().patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "borrowable"})
    assert r.json()["loans_returned"] == 0 and r.json()["access_mode"] == "borrowable"
    assert db.query(Loan).filter(Loan.returned_at.is_(None)).count() == 1


def test_reupload_as_members_only_returns_active_loans(admin, monkeypatch):
    from lenny.core.api import LennyAPI
    monkeypatch.setattr(LennyAPI, "upload_files", classmethod(lambda cls, *a, **k: FormatEnum.EPUB.value))
    monkeypatch.setattr(LennyAPI, "_item_s3_keys", classmethod(lambda cls, *a, **k: []))
    item = make_item(subscription=False, encrypted=True)
    _loans(item, active=(NOW() + datetime.timedelta(days=1), None))
    r = client().post(f"/v1/api/admin/items/{EDITION}/reupload", data={"access_mode": "subscription"},
                      files={"file": ("b.epub", b"x", "application/epub+zip")})
    assert r.status_code == 200
    assert db.query(Loan).filter(Loan.returned_at.is_(None)).count() == 0


def test_a_failed_mode_change_returns_no_loans(admin, monkeypatch):
    item = make_item(subscription=False, encrypted=True)
    _loans(item, active=(NOW() + datetime.timedelta(days=1), None))
    real_commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(sqlalchemy.exc.OperationalError("c", {}, None)))
    r = client().patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "open"})
    monkeypatch.setattr(db, "commit", real_commit)
    assert r.status_code == 500
    db.expire_all()
    assert Item.exists(EDITION).encrypted is True
    assert db.query(Loan).filter(Loan.returned_at.is_(None)).count() == 1


@pytest.mark.parametrize("item_param", [str(EDITION), f"OL{EDITION}M", f"ol{EDITION}m"])
def test_admin_loans_filter_by_book(admin, monkeypatch, item_param):
    monkeypatch.setattr("lenny.core.admin_loans._resolve_titles", lambda editions: {})
    item = make_item(subscription=False, encrypted=True)
    other = _lendable()
    _loans(item, a=(NOW() + datetime.timedelta(days=1), None))
    db.add(Loan(item_id=other.id, patron_email_hash="x", due_date=NOW() + datetime.timedelta(days=1)))
    db.commit()
    body = client().get(f"/v1/api/admin/loans?status=active&item={item_param}").json()
    assert body["total"] == 1 and body["items"][0]["edition_key"] == f"OL{EDITION}M"


# ── the subscription model ───────────────────────────────────────────────────

def test_revoke_with_nothing_to_match_touches_nothing():
    lease()
    assert Subscription.revoke("ghost", None, []) == 0 and Subscription.revoke("ghost", "", [None]) == 0
    assert Subscription.is_active(hash_email(ME))


def test_revocations_stay_within_their_provider():
    lease()
    Subscription.grant("other", hash_email(ME), "m-1", [])
    assert Subscription.revoke("ghost", "m-1", []) == 1
    assert Subscription.revoke_all("ghost") == 0
    assert db.query(Subscription).filter_by(provider="other").count() == 1


def test_seen_tiers_count_only_live_passes():
    lease()
    Subscription.grant("ghost", hash_email("b@example.com"), "m-2", ["t-paid", "t-gold"])
    Subscription.grant("ghost", hash_email("c@example.com"), "m-3", ["t-gold"], lease_seconds=-1)
    assert Subscription.seen_tiers("ghost") == [{"id": "t-paid", "members": 2}, {"id": "t-gold", "members": 1}]


def test_lease_is_twenty_four_hours():
    lease()
    left = db.query(Subscription).one().expires_at.replace(tzinfo=datetime.timezone.utc) - NOW()
    assert datetime.timedelta(hours=23, minutes=59) < left <= datetime.timedelta(hours=24)


@pytest.mark.parametrize("ua, device", [
    ("Mozilla/5.0 (iPad; CPU OS 17_0)", "tablet"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile", "mobile"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", "mobile"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)", "desktop"),
    ("", "desktop"),
    (None, "desktop"),
])
def test_device_classes(ua, device):
    assert SubscriptionRead.device_class_of(ua) == device


def test_reads_are_counted_once_per_book_member_device_and_day():
    item = make_item()
    for ua in ("Mozilla/5.0 (iPhone)", "Mozilla/5.0 (iPhone)", "Mozilla/5.0 (Macintosh)"):
        SubscriptionRead.record(item.id, hash_email(ME), ua)
    rows = {r.device_class: r.opens for r in db.query(SubscriptionRead)}
    assert rows == {"mobile": 2, "desktop": 1}


def test_a_broken_stats_write_never_blocks_reading(monkeypatch):
    from lenny.core.readium import ReadiumAPI
    monkeypatch.setattr(ReadiumAPI, "get_manifest", classmethod(lambda cls, *a: {"metadata": {}}))
    make_item()
    lease()
    real = db.execute

    def failing(stmt, *a, **k):
        if "subscription_reads" in str(stmt):
            raise sqlalchemy.exc.OperationalError("insert", {}, Exception("locked"))
        return real(stmt, *a, **k)
    monkeypatch.setattr(db, "execute", failing)
    r = _member_client().get(f"/v1/api/items/{EDITION}/readium/manifest.json")
    assert r.status_code == 200


# ── stats ────────────────────────────────────────────────────────────────────

def test_book_stats_rank_page_and_survive_open_library_being_down(admin, monkeypatch):
    from lenny.core.api import LennyAPI
    a, b = make_item(), Item(openlibrary_edition=EDITION + 1, subscription=True, formats=FormatEnum.EPUB)
    db.add(b)
    db.commit()
    for _ in range(3):
        SubscriptionRead.record(b.id, hash_email(ME), "Mozilla/5.0 (Macintosh)")
    SubscriptionRead.record(a.id, hash_email("x@example.com"), "Mozilla/5.0 (Macintosh)")
    SubscriptionRead.record(a.id, hash_email("y@example.com"), "Mozilla/5.0 (Macintosh)")

    def down(*a, **k):
        raise ConnectionError("Open Library down")
    monkeypatch.setattr(LennyAPI, "_enrich_items", classmethod(down))
    by = lambda sort: client().get(f"/v1/api/admin/stats/subscriptions/books?sort={sort}").json()  # noqa: E731
    assert [i["olid"] for i in by("opens")["items"]] == [f"OL{EDITION + 1}M", f"OL{EDITION}M"]
    assert [i["olid"] for i in by("patrons")["items"]] == [f"OL{EDITION}M", f"OL{EDITION + 1}M"]
    assert by("opens")["items"][0]["title"] is None and by("opens")["total"] == 2
    page2 = client().get("/v1/api/admin/stats/subscriptions/books?page=2&page_size=1").json()
    assert page2["total"] == 2 and len(page2["items"]) == 1


@pytest.mark.parametrize("query, code", [
    ("sort=title", "invalid_sort"),
    ("from=2025-01-01&to=2026-06-01", "invalid_range"),     # more than a year
    ("from=2026-11-14&to=2026-11-01", "invalid_range"),
    ("from=yesterday", "invalid_range"),
])
def test_book_stats_validation(admin, query, code):
    r = client().get(f"/v1/api/admin/stats/subscriptions/books?{query}")
    assert r.status_code == 422 and r.json()["code"] == code


# ── OPDS ─────────────────────────────────────────────────────────────────────

def test_opds_search_never_asks_about_members_only_books(monkeypatch):
    from lenny.core.api import LennyAPI
    asked = []
    monkeypatch.setattr("lenny.core.api._guarded_search",
                        lambda **kw: asked.append(kw["lenny_ids"]) or types.SimpleNamespace(records=[]))
    make_item()
    db.add(Item(openlibrary_edition=EDITION + 1, formats=FormatEnum.EPUB))
    db.commit()
    LennyAPI.search_feed(query="anything")
    assert asked and all(EDITION not in ids and EDITION + 1 in ids for ids in asked)


def test_opds_feed_count_and_page_skip_members_only_books():
    make_item()
    for n in range(1, 4):
        db.add(Item(openlibrary_edition=EDITION + n, formats=FormatEnum.EPUB))
    db.commit()
    assert Item.count(subscription=False) == 3 and Item.count() == 4
    assert EDITION not in [i.openlibrary_edition for i in Item.get_many(limit=10, subscription=False)]


# ── privacy ──────────────────────────────────────────────────────────────────

def test_a_members_email_is_stored_nowhere(admin, monkeypatch):
    from lenny.core.readium import ReadiumAPI
    monkeypatch.setattr(ReadiumAPI, "get_manifest", classmethod(lambda cls, *a: {"metadata": {}}))
    make_item()
    c = client()
    _launch(c)
    c.get(f"/v1/api/items/{EDITION}/readium/manifest.json")
    _deliver({"member": {"current": {"uuid": "m-1", "email": ME, "status": "paid"}, "previous": {"name": "x"}}})
    stored = []
    for table in ("subscriptions", "subscription_reads", "cache"):
        stored += [str(tuple(row)) for row in db.execute(sqlalchemy.text(f"SELECT * FROM {table}"))]
    dump = "\n".join(stored).lower()
    assert stored and ME.lower() not in dump and "member@" not in dump
    for path in ("/v1/api/admin/subscriptions", "/v1/api/admin/stats/subscriptions",
                 "/v1/api/admin/stats/subscriptions/books", "/v1/api/admin/settings/ghost"):
        monkeypatch.setattr("lenny.core.api.LennyAPI._enrich_items", classmethod(lambda cls, *a, **k: {}))
        body = client().get(path).text
        assert ME not in body and hash_email(ME) not in body, path


# ── Open Library being down ──────────────────────────────────────────────────

def test_open_library_down_never_blocks_a_member(monkeypatch):
    """Launch, the reader door and the manifest use only Lenny's own data: no outbound call at all."""
    import socket
    from lenny.core.readium import ReadiumAPI
    monkeypatch.setattr(ReadiumAPI, "get_manifest", classmethod(lambda cls, *a: {"metadata": {}}))

    def no_network(*a, **k):
        raise OSError("network is down")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    import httpx
    with pytest.raises(httpx.ConnectError):  # the block bites: Open Library is unreachable here
        httpx.get("https://openlibrary.org/search.json")
    make_item()
    c = client()
    r = c.post(LAUNCH, data={"token": token(), "book": f"OL{EDITION}M"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get(r.headers["location"], follow_redirects=False).status_code == 307
    assert c.get(f"/v1/api/items/{EDITION}/access").status_code == 204
    assert c.get(f"/v1/api/items/{EDITION}/readium/manifest.json").status_code == 200
