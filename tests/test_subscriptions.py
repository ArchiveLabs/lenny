"""Members-only (subscription) access with Ghost (#280, #281, #282, #283).

Ghost's entitlement token is faked with a local RSA key served as the site's JWKS,
in the exact shape Ghost v6.77 issues (token-service.js `encodeEntitlementToken`).
"""
import datetime
import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from authlib.jose import JsonWebKey, JsonWebToken  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import ghost  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan, Subscription, SubscriptionRead  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

SITE = "https://hydra.example"
ISS = f"{SITE}/members/api"
ME = "member@example.com"
EDITION = 920111001
LAUNCH = "/v1/api/subscriptions/ghost/launch"
WEBHOOK = "/v1/api/subscriptions/ghost/webhook"
SECRET = "whsec-test"

KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
OTHER_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
JWKS = {"keys": [KEY.as_dict(is_private=False)]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    Base.metadata.create_all(engine)
    monkeypatch.setenv(ghost.URL_KEY, SITE)
    monkeypatch.setenv(ghost.SECRET_KEY, SECRET)
    monkeypatch.delenv(ghost.TIERS_KEY, raising=False)
    monkeypatch.setattr(ghost, "_jwks", lambda site: JWKS)
    yield
    db.remove()
    for table in ("subscription_reads", "subscriptions", "cache", "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


def make_item(subscription=True, encrypted=False):
    item = Item(openlibrary_edition=EDITION, encrypted=encrypted, subscription=subscription,
                formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    return item


def token(key=KEY, alg="RS512", **over):
    now = int(time.time())
    claims = {"sub": ME, "member_uuid": "m-1", "paid": True, "active_tier_ids": ["t-paid"],
              "scope": "members:entitlements:read", "jti": str(uuid.uuid4()),
              "iss": ISS, "aud": ISS, "iat": now, "exp": now + 300}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return JsonWebToken([alg]).encode({"alg": alg, "kid": "k1"}, claims, key).decode()


def client():
    return TestClient(app, base_url="https://testserver")


# ── token checks ─────────────────────────────────────────────────────────────

def test_valid_token_returns_claims():
    assert ghost.verify_entitlement(token())["sub"] == ME


@pytest.mark.parametrize("bad, code", [
    (lambda: token(key=OTHER_KEY), "invalid_token"),
    (lambda: token(iss="https://evil.example/members/api"), "invalid_token"),
    (lambda: token(aud="https://evil.example/members/api"), "invalid_token"),
    (lambda: token(exp=int(time.time()) - 120), "invalid_token"),
    (lambda: token(scope="members:identity"), "invalid_token"),   # Ghost's identity token, same key
    (lambda: token(jti=None), "invalid_token"),
    (lambda: token(paid=False), "not_paid"),
    (lambda: "not.a.jwt", "invalid_token"),
    (lambda: "", "invalid_token"),
])
def test_bad_tokens_are_refused(bad, code):
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(bad())
    assert e.value.code == code


def test_hs256_signed_with_the_public_key_is_refused():
    # Classic alg confusion: an HMAC token keyed with the published RSA key.
    pub = json.dumps(JWKS["keys"][0]).encode()
    forged = JsonWebToken(["HS256"]).encode({"alg": "HS256", "kid": "k1"},
                                            {"sub": ME, "iss": ISS, "aud": ISS, "paid": True,
                                             "scope": "members:entitlements:read", "jti": "x",
                                             "exp": int(time.time()) + 300}, pub).decode()
    with pytest.raises(ghost.GhostError):
        ghost.verify_entitlement(forged)


def test_token_is_single_use():
    t = token()
    ghost.verify_entitlement(t)
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(t)
    assert e.value.code == "token_reused"


def test_tier_allowlist(monkeypatch):
    monkeypatch.setenv(ghost.TIERS_KEY, "t-gold")
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(token())
    assert e.value.code == "tier_not_allowed"
    assert ghost.verify_entitlement(token(active_tier_ids=["t-gold"]))


def test_not_configured(monkeypatch):
    monkeypatch.setenv(ghost.URL_KEY, "")
    with pytest.raises(ghost.GhostError) as e:
        ghost.verify_entitlement(token())
    assert e.value.code == "not_configured"


# ── launch and the read gate ─────────────────────────────────────────────────

def test_launch_leases_sets_session_and_opens_reader():
    make_item()
    c = client()
    r = c.post(LAUNCH, data={"token": token(), "book": f"OL{EDITION}M"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/v1/api/items/{EDITION}/read"
    row = db.query(Subscription).one()
    assert row.patron_email_hash == hash_email(ME) and row.member_uuid == "m-1"
    assert ME not in json.dumps([row.provider, row.member_uuid, row.tier_ids])

    read = c.get(r.headers["location"], follow_redirects=False)
    assert read.status_code == 307 and "/read/manifest/" in read.headers["location"]
    assert read.headers["cache-control"] == "private, no-store"


def test_launch_refuses_a_bad_token_without_a_session():
    make_item()
    r = client().post(LAUNCH, data={"token": token(paid=False), "book": str(EDITION)}, follow_redirects=False)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert db.query(Subscription).count() == 0


def test_launch_unknown_book():
    r = client().post(LAUNCH, data={"token": token(), "book": "OL1M"}, follow_redirects=False)
    assert r.status_code == 404


def test_members_only_without_a_lease():
    make_item()
    r = client().get(f"/v1/api/items/{EDITION}/read", headers={"accept": "application/json"},
                     follow_redirects=False)
    assert r.status_code == 403 and r.json()["code"] == "subscription_required"
    page = client().get(f"/v1/api/items/{EDITION}/read", headers={"accept": "text/html"},
                        follow_redirects=False)
    assert page.status_code == 403 and SITE in page.text


def test_expired_lease_is_refused():
    make_item()
    c = client()
    c.post(LAUNCH, data={"token": token(), "book": str(EDITION)}, follow_redirects=False)
    db.query(Subscription).update({"expires_at": datetime.datetime.now(datetime.timezone.utc)
                                   - datetime.timedelta(seconds=1)})
    db.commit()
    r = c.get(f"/v1/api/items/{EDITION}/read", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 403


def test_open_and_borrowable_items_are_unchanged():
    make_item(subscription=False)
    r = client().get(f"/v1/api/items/{EDITION}/read", follow_redirects=False)
    assert r.status_code == 307 and "cache-control" not in r.headers


def test_manifest_fetch_counts_an_open(monkeypatch):
    from lenny.core.readium import ReadiumAPI
    monkeypatch.setattr(ReadiumAPI, "get_manifest", classmethod(lambda cls, *a: {"metadata": {}}))
    make_item()
    c = client()
    c.post(LAUNCH, data={"token": token(), "book": str(EDITION)}, follow_redirects=False)
    for _ in range(2):
        assert c.get(f"/v1/api/items/{EDITION}/readium/manifest.json",
                     headers={"user-agent": "Mozilla/5.0 (iPhone)"}).status_code == 200
    row = db.query(SubscriptionRead).one()
    assert (row.opens, row.device_class, row.patron_email_hash) == (2, "mobile", hash_email(ME))


# ── webhooks ─────────────────────────────────────────────────────────────────

def signed(payload, secret=SECRET, ts=None):
    body = json.dumps(payload).encode()
    ts = str(ts if ts is not None else int(time.time() * 1000))
    sig = hmac.new(secret.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
    return body, {"x-ghost-signature": f"sha256={sig}, t={ts}", "content-type": "application/json"}


def lease():
    Subscription.grant("ghost", hash_email(ME), "m-1", ["t-paid"])


def test_webhook_member_deleted_revokes():
    lease()
    body, headers = signed({"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}})
    r = client().post(WEBHOOK, content=body, headers=headers)
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert db.query(Subscription).count() == 0


def test_webhook_downgrade_to_free_revokes():
    lease()
    body, headers = signed({"member": {"current": {"uuid": "m-1", "email": ME, "status": "free"},
                                       "previous": {"status": "paid"}}})
    assert client().post(WEBHOOK, content=body, headers=headers).json()["revoked"] == 1


def test_webhook_unrelated_edit_keeps_access():
    lease()
    body, headers = signed({"member": {"current": {"uuid": "m-1", "email": ME, "status": "paid"},
                                       "previous": {"name": "Old name"}}})
    assert client().post(WEBHOOK, content=body, headers=headers).json()["revoked"] == 0
    assert Subscription.is_active(hash_email(ME))


@pytest.mark.parametrize("mutate", [
    lambda b, h: (b, {**h, "x-ghost-signature": h["x-ghost-signature"].replace("sha256=", "sha256=0")}),
    lambda b, h: (b + b" ", h),
    lambda b, h: (b, {"content-type": "application/json"}),
    lambda b, h: signed(json.loads(b), ts=int(time.time() * 1000) - 10 * 60 * 1000),
    lambda b, h: signed(json.loads(b), secret="wrong"),
])
def test_webhook_rejects_bad_signatures_and_never_410(mutate):
    lease()
    body, headers = mutate(*signed({"member": {"current": {}, "previous": {"uuid": "m-1"}}}))
    r = client().post(WEBHOOK, content=body, headers=headers)
    assert r.status_code == 401 and r.status_code != 410
    assert Subscription.is_active(hash_email(ME))


# ── admin: access_mode and settings ──────────────────────────────────────────

@pytest.fixture
def admin(monkeypatch):
    import lenny.routes.api as api_routes
    import lenny.routes.subscriptions as sub_routes
    monkeypatch.setattr(api_routes, "_require_admin", lambda request: None)
    monkeypatch.setattr(sub_routes, "_require_admin", lambda request: None)


def test_patch_access_mode(admin):
    make_item(subscription=False, encrypted=True)
    c = client()
    r = c.patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "subscription"})
    assert r.status_code == 200
    assert (r.json()["access_mode"], r.json()["encrypted"]) == ("subscription", False)
    r = c.patch(f"/v1/api/admin/items/{EDITION}", json={"encrypted": True})
    assert r.json()["access_mode"] == "borrowable"
    assert c.patch(f"/v1/api/admin/items/{EDITION}",
                   json={"access_mode": "open", "encrypted": True}).status_code == 400
    assert c.patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "secret"}).status_code == 422


def test_ghost_settings_shape(admin):
    r = client().get("/v1/api/admin/settings/ghost")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["site_url"] == SITE and body["webhook_secret"] == SECRET and body["lease_hours"] == 24
    assert "/v1/api/subscriptions/ghost/launch" in body["snippet"]
    assert body["webhook_url"].endswith(WEBHOOK)


def test_ghost_settings_reject_non_https(admin):
    r = client().put("/v1/api/admin/settings/ghost", json={"site_url": "http://hydra.example"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_site_url"


def test_stats(admin):
    item = make_item()
    SubscriptionRead.record(item.id, hash_email(ME), "Mozilla/5.0 (Macintosh)")
    SubscriptionRead.record(item.id, hash_email("b@example.com"), "Mozilla/5.0 (Android)")
    body = client().get("/v1/api/admin/stats/subscriptions").json()
    assert body["totals"] == {"books_read": 1, "patrons": 2, "opens": 2, "active_now": 2}
    assert {d["device_class"] for d in body["devices"]} == {"desktop", "mobile"}
    assert client().get("/v1/api/admin/stats/subscriptions?from=2026-13-01").status_code == 422


@pytest.mark.parametrize("data, mode", [
    ({"access_mode": "subscription"}, "subscription"),
    ({"access_mode": "open"}, "open"),
    ({"encrypted": "true"}, "borrowable"),
    ({}, "borrowable"),   # no field: the book keeps its mode
])
def test_reupload_access_mode(admin, monkeypatch, data, mode):
    from lenny.core.api import LennyAPI
    monkeypatch.setattr(LennyAPI, "upload_files", classmethod(lambda cls, *a, **k: FormatEnum.EPUB.value))
    monkeypatch.setattr(LennyAPI, "_item_s3_keys", classmethod(lambda cls, *a, **k: []), raising=False)
    make_item(subscription=False, encrypted=True)
    r = client().post(f"/v1/api/admin/items/{EDITION}/reupload", data=data,
                      files={"file": ("b.epub", b"x", "application/epub+zip")})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert Item.exists(EDITION).access_mode == mode


def test_reupload_rejects_both_fields(admin):
    make_item()
    r = client().post(f"/v1/api/admin/items/{EDITION}/reupload", data={"access_mode": "open", "encrypted": "true"},
                      files={"file": ("b.epub", b"x", "application/epub+zip")})
    assert r.status_code == 400


def test_core_update_keeps_modes_exclusive():
    from lenny.core.api import LennyAPI
    make_item(subscription=False, encrypted=True)
    assert LennyAPI.update_item(EDITION, subscription=True).access_mode == "subscription"
    assert LennyAPI.update_item(EDITION, encrypted=True).access_mode == "borrowable"


def test_members_only_is_not_publicly_readable():
    assert make_item().is_readable is False


def test_snippet_follows_a_ghost_under_a_path():
    s = ghost.snippet("https://lenny.example/v1/api/subscriptions/ghost/launch", "https://example.com/blog")
    assert '"https://example.com/blog/members/api/entitlements/"' in s
    assert '[data-lenny-book], a[href*="#lenny-book="]' in s  # HTML card and Button card


def test_changing_the_ghost_site_drops_old_leases(admin, monkeypatch):
    monkeypatch.setattr(ghost, "fetch_jwks", lambda site: JWKS)
    written = {}
    monkeypatch.setattr("lenny.core.ol_bootstrap.update_env_file", lambda path, u: written.update(u))
    lease()
    r = client().put("/v1/api/admin/settings/ghost", json={"site_url": "https://other.example"})
    assert r.status_code == 200 and written[ghost.URL_KEY] == "https://other.example"
    assert db.query(Subscription).count() == 0


def test_resaving_the_same_site_keeps_leases(admin, monkeypatch):
    monkeypatch.setattr(ghost, "fetch_jwks", lambda site: JWKS)
    monkeypatch.setattr("lenny.core.ol_bootstrap.update_env_file", lambda path, u: None)
    lease()
    assert client().put("/v1/api/admin/settings/ghost", json={"site_url": SITE}).status_code == 200
    assert db.query(Subscription).count() == 1


def test_grant_retries_after_losing_the_insert_race(monkeypatch):
    from sqlalchemy.exc import IntegrityError
    calls = {"n": 0}
    real_commit = db.commit

    def flaky_commit():  # the first commit loses the race to a concurrent launch
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return real_commit()

    with monkeypatch.context() as m:
        m.setattr(db, "commit", flaky_commit)
        assert Subscription.grant("ghost", hash_email(ME), "m-1").member_uuid == "m-1"
    assert calls["n"] == 2 and Subscription.is_active(hash_email(ME))


def test_token_minted_before_a_cancel_cannot_relaunch():
    make_item()
    before = token(iat=int(time.time()) - 10)
    lease()
    body, headers = signed({"member": {"current": {}, "previous": {"uuid": "m-1", "email": ME}}})
    assert client().post(WEBHOOK, content=body, headers=headers).json()["revoked"] == 1
    r = client().post(LAUNCH, data={"token": before, "book": str(EDITION)}, follow_redirects=False)
    assert r.status_code == 403 and "membership_changed" in r.text
    assert db.query(Subscription).count() == 0
    # Re-subscribing later mints a newer token, which works.
    after = token(iat=int(time.time()) + 1)
    assert client().post(LAUNCH, data={"token": after, "book": str(EDITION)},
                         follow_redirects=False).status_code == 303


def test_members_only_needs_login_but_is_never_lent():
    from lenny.core.exceptions import LoanNotRequiredError
    item = make_item()
    assert item.is_login_required is True and item.is_lendable is False
    with pytest.raises(LoanNotRequiredError):
        item.borrow(ME)


def test_readium_resources_are_gated_too():
    make_item()
    r = client().get(f"/v1/api/items/{EDITION}/readium/OEBPS/chapter1.xhtml",
                     headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 403 and r.json()["code"] == "subscription_required"


# ── member passes and the dashboard ──────────────────────────────────────────

def test_member_passes_list_and_end(admin):
    lease()
    Subscription.grant("ghost", hash_email("b@example.com"), "m-2", [])
    db.query(Subscription).filter_by(member_uuid="m-2").update(
        {"expires_at": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)})
    db.commit()
    body = client().get("/v1/api/admin/subscriptions").json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["member"] == hash_email(ME)[:8] and row["tier_ids"] == ["t-paid"]
    assert ME not in json.dumps(body) and hash_email(ME) not in json.dumps(body)
    assert client().delete(f"/v1/api/admin/subscriptions/{row['id']}").status_code == 204
    assert not Subscription.is_active(hash_email(ME))
    r = client().delete(f"/v1/api/admin/subscriptions/{row['id']}")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_dashboard(admin, monkeypatch):
    import lenny.routes.admin_dashboard as dash
    monkeypatch.setattr(dash, "_require_admin", lambda request: None)
    make_item()
    db.add(Item(openlibrary_edition=EDITION + 1, encrypted=True, formats=FormatEnum.EPUB))
    db.commit()
    body = client().get("/v1/api/admin/dashboard").json()
    assert body["books"] == {"total": 2, "open": 0, "borrowable": 1, "subscription": 1}
    assert body["loans"] == {"active": 0, "overdue": 0, "started_30d": 0}
    assert body["health"]["database"] == "ok" and body["health"]["reader"] == "down"  # no reader in tests
    # One broken part is null; the rest still answer.
    monkeypatch.setattr(dash, "_loans", lambda: 1 / 0)
    body = client().get("/v1/api/admin/dashboard").json()
    assert body["loans"] is None and body["books"]["total"] == 2


def test_loan_stats_by_day(admin, monkeypatch):
    import lenny.routes.admin_dashboard as dash
    from lenny.core.models import Loan
    monkeypatch.setattr(dash, "_require_admin", lambda request: None)
    item = make_item(subscription=False, encrypted=True)
    day = datetime.datetime(2026, 11, 10, 12, tzinfo=datetime.timezone.utc)
    db.add_all([
        Loan(item_id=item.id, patron_email_hash="a", created_at=day),
        Loan(item_id=item.id, patron_email_hash="b", created_at=day, returned_at=day + datetime.timedelta(days=1)),
        Loan(item_id=item.id, patron_email_hash="c", created_at=day - datetime.timedelta(days=60)),  # out of range
    ])
    db.commit()
    body = client().get("/v1/api/admin/stats/loans?from=2026-11-01&to=2026-11-14").json()
    assert body["by_day"] == [{"date": "2026-11-10", "started": 2, "returned": 0},
                              {"date": "2026-11-11", "started": 0, "returned": 1}]
    assert client().get("/v1/api/admin/stats/loans?from=2026-11-14&to=2026-11-01").json()["code"] == "invalid_range"


# ── the reader door, the members page and returns ────────────────────────────

def test_reader_access_check_for_nginx():
    make_item()
    db.add(Item(openlibrary_edition=EDITION + 1, formats=FormatEnum.EPUB))  # open
    db.commit()
    c = client()
    assert c.get(f"/v1/api/items/{EDITION}/access").status_code == 403
    assert c.get(f"/v1/api/items/{EDITION + 1}/access").status_code == 204
    assert c.get("/v1/api/items/1/access").status_code == 204          # unknown: the reader says not found
    c.post(LAUNCH, data={"token": token(), "book": str(EDITION)}, follow_redirects=False)
    assert c.get(f"/v1/api/items/{EDITION}/access").status_code == 204


def test_members_page_says_why(monkeypatch):
    make_item()
    html = {"accept": "text/html"}
    page = client().get(f"/v1/api/items/{EDITION}/read", headers=html)
    assert "This book is for members" in page.text and "Go to hydra.example" in page.text
    c = client()
    c.post(LAUNCH, data={"token": token(), "book": str(EDITION)}, follow_redirects=False)
    db.query(Subscription).update({"expires_at": datetime.datetime.now(datetime.timezone.utc)
                                   - datetime.timedelta(seconds=1)})
    db.commit()
    ended = c.get(f"/v1/api/items/{EDITION}/read", headers=html)
    assert ended.status_code == 403 and "Your reading pass has ended" in ended.text
    assert c.get(f"/v1/api/items/{EDITION}/read").json()["reason"] == "pass_ended"
    monkeypatch.setenv(ghost.URL_KEY, "")
    assert client().get(f"/v1/api/items/{EDITION}/read").json()["reason"] == "not_configured"


def test_returning_a_members_only_book_has_nothing_to_do():
    make_item()
    r = client().get(f"/v1/api/items/{EDITION}/return")
    assert r.status_code == 200 and r.json()["error"] == "members_only"
    page = client().get(f"/v1/api/items/{EDITION}/return", headers={"accept": "text/html"})
    assert page.status_code == 200 and "Nothing to return" in page.text


def test_nginx_asks_lenny_before_opening_the_reader():
    import pathlib
    conf = (pathlib.Path(__file__).resolve().parent.parent / "docker/nginx/conf.d/lenny.conf").read_text()
    reader = conf.split("location ~ ^/read/manifest/", 1)[1].split("}", 1)[0]
    assert "auth_request /_lenny_item_access;" in reader and "error_page 401 403" in reader
    assert "/v1/api/items/$lenny_book/access" in conf


def test_members_only_books_stay_out_of_opds(monkeypatch):
    monkeypatch.setattr("lenny.routes.api.LennyAPI.opds_feed", lambda **kw: {})
    make_item()
    open_book = Item(openlibrary_edition=EDITION + 1, formats=FormatEnum.EPUB)
    db.add(open_book)
    db.commit()
    assert [i.openlibrary_edition for i in Item.get_many(subscription=False)] == [EDITION + 1]
    assert Item.count(subscription=False) == 1
    assert client().get(f"/v1/api/opds/{EDITION}").status_code == 404
    assert client().get(f"/v1/api/opds/{EDITION + 1}").status_code == 200


def test_switching_off_pauses_reading_and_keeps_everything(admin, monkeypatch):
    monkeypatch.setattr("lenny.core.ol_bootstrap.update_env_file",
                        lambda path, u: [monkeypatch.setenv(k, v) for k, v in u.items()])
    admin = client()
    make_item()
    c = client()
    c.post(LAUNCH, data={"token": token(), "book": f"OL{EDITION}M"}, follow_redirects=False)
    json_get = dict(headers={"accept": "application/json"}, follow_redirects=False)
    assert c.get(f"/v1/api/items/{EDITION}/read", **json_get).status_code == 307

    r = admin.put("/v1/api/admin/settings/ghost", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["site_url"] == SITE
    r = c.get(f"/v1/api/items/{EDITION}/read", **json_get)
    assert r.status_code == 403 and r.json()["reason"] == "disabled"
    r = c.post(LAUNCH, data={"token": token(), "book": f"OL{EDITION}M"}, follow_redirects=False)
    assert r.status_code == 403 and "ghost_disabled" in r.text

    assert admin.put("/v1/api/admin/settings/ghost", json={"enabled": True}).json()["enabled"] is True
    assert c.get(f"/v1/api/items/{EDITION}/read", **json_get).status_code == 307
    assert admin.put("/v1/api/admin/settings/ghost", json={"enabled": "no"}).status_code == 422


def test_leaving_borrowable_returns_active_loans(admin, monkeypatch):
    monkeypatch.setattr("lenny.core.admin_loans._resolve_titles", lambda editions: {})
    item = make_item(subscription=False, encrypted=True)
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    db.add_all([Loan(item_id=item.id, patron_email_hash=hash_email(ME), due_date=soon),
                Loan(item_id=item.id, patron_email_hash=hash_email("b@example.com"), due_date=soon)])
    db.commit()
    c = client()
    active = f"/v1/api/admin/loans?status=active&item=OL{EDITION}M&limit=1"
    assert c.get(active).json()["total"] == 2
    assert c.get("/v1/api/admin/loans?item=nope").status_code == 400

    r = c.patch(f"/v1/api/admin/items/{EDITION}", json={"loan_duration_days": 7})
    assert r.json()["loans_returned"] == 0  # still borrowable: loans stay
    r = c.patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "subscription"})
    assert r.status_code == 200 and r.json()["loans_returned"] == 2
    assert c.get(active).json()["total"] == 0
    assert c.patch(f"/v1/api/admin/items/{EDITION}", json={"access_mode": "open"}).json()["loans_returned"] == 0
