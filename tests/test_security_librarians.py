"""Security regressions for the librarian account routes."""

import os

import pytest
from fastapi.testclient import TestClient

from tests.test_librarians import PASSWORD, clean_table, make  # noqa: F401

os.environ.setdefault("TESTING", "true")


@pytest.fixture
def api(monkeypatch):
    from lenny.app import app
    from lenny.core import auth
    monkeypatch.setattr(auth, "ADMIN_INTERNAL_SECRET", "s3cret")
    client = TestClient(app, raise_server_exceptions=False)

    def call(method, path, row=None, **kw):
        headers = {"X-Admin-Internal-Secret": "s3cret",
                   "Authorization": "Bearer " + auth.issue_admin_token(row.id if row else None)}
        return client.request(method, "/v1/api" + path, headers=headers, **kw)
    return call


def test_a_librarian_cannot_disable_their_own_account(api):
    from lenny.core import librarians
    root = make()
    r = api("POST", f"/admin/librarians/{root.id}/disable", root, json={"disabled": True})
    assert r.status_code == 400 and r.json()["detail"] == "You cannot disable your own account."
    assert librarians.get_active(root.id)
    # re-enabling yourself is not a disable
    assert api("POST", f"/admin/librarians/{root.id}/disable", root, json={"disabled": False}).status_code == 200


# ── malformed input must be refused, never a 500 ──────────────────────────

BAD = [["x"], {"a": 1}, 7, True, "bad\x00nul"]


@pytest.mark.parametrize("bad", BAD)
def test_login_with_wrong_types_is_a_plain_401(api, bad):
    from lenny.app import app
    make()
    c = TestClient(app, raise_server_exceptions=False)
    h = {"X-Admin-Internal-Secret": "s3cret"}
    for body in ({"username": bad, "password": PASSWORD}, {"username": "root", "password": bad}):
        assert c.post("/v1/api/admin/auth", headers=h, json=body).status_code == 401


@pytest.mark.parametrize("bad", BAD[:4])
def test_account_routes_refuse_wrong_types(api, bad):
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    base = {"name": "Bob", "email": "bob@example.org", "password": PASSWORD}
    for field in ("name", "email", "password"):
        assert api("POST", "/admin/librarians", root, json={**base, field: bad}).status_code == 400
    for field in ("email", "password"):
        assert api("PUT", f"/admin/librarians/{ann.id}", root, json={field: bad}).status_code == 400


def test_flags_must_be_real_booleans(api):
    from lenny.core import librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    # "false" is a truthy string: it must not disable, nor promote
    assert api("POST", f"/admin/librarians/{ann.id}/disable", root, json={"disabled": "false"}).status_code == 400
    assert librarians.get_active(ann.id)
    r = api("PUT", f"/admin/librarians/{ann.id}", root, json={"is_super": "false", "current_password": PASSWORD})
    assert r.status_code == 400 and not librarians.get(ann.id).is_super
    new = {"name": "Bob", "email": "bob@example.org", "password": PASSWORD, "is_super": "no"}
    assert api("POST", "/admin/librarians", root, json=new).status_code == 400


def test_an_out_of_range_id_is_a_404ish_400_not_a_500(api):
    root = make()
    assert api("PUT", "/admin/librarians/99999999999999999999", root, json={"email": "a@b.org"}).status_code == 400
    assert api("POST", "/admin/librarians/99999999999999999999/disable", root, json={}).status_code == 400


def test_a_huge_password_is_refused_cheaply(api):
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    assert api("PUT", f"/admin/librarians/{ann.id}", root, json={"password": "x" * 100_000}).status_code == 400


# ── guessing the password ─────────────────────────────────────────────────

def test_wrong_passwords_are_throttled_per_librarian(api):
    from lenny.core import librarians
    make()
    for _ in range(librarians.MAX_FAILURES):
        assert librarians.authenticate("root", "wrong password!!") is None
    with pytest.raises(librarians.TooManyAttempts):
        librarians.authenticate("root", PASSWORD)


def test_step_up_guessing_through_put_is_throttled(api):
    from lenny.core import librarians
    ann = make("ann", "ann@example.org", is_super=False)
    codes = [api("PUT", f"/admin/librarians/{ann.id}", ann,
                 json={"password": "a brand new passphrase", "current_password": f"guess number {i}"}).status_code
             for i in range(librarians.MAX_FAILURES + 2)]
    assert codes[-1] == 429
    # even the right password is refused while locked
    assert api("PUT", f"/admin/librarians/{ann.id}", ann,
               json={"password": "a brand new passphrase", "current_password": PASSWORD}).status_code == 429


def test_login_throttle_answers_429(api):
    from lenny.app import app
    from lenny.core import librarians
    make()
    c = TestClient(app, raise_server_exceptions=False)
    h = {"X-Admin-Internal-Secret": "s3cret"}
    for _ in range(librarians.MAX_FAILURES):
        c.post("/v1/api/admin/auth", headers=h, json={"username": "root", "password": "wrong password!!"})
    assert c.post("/v1/api/admin/auth", headers=h, json={"username": "root", "password": PASSWORD}).status_code == 429


# ── a session dies when its password changes ──────────────────────────────

def test_old_tokens_stop_working_when_the_password_changes(api):
    from lenny.core import auth, librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    stolen = auth.issue_admin_token(ann.id)
    assert auth.verify_admin_token(stolen)
    assert api("PUT", f"/admin/librarians/{ann.id}", root, json={"password": "reset by the super user"}).status_code == 200
    assert not auth.verify_admin_token(stolen)


def test_changing_your_own_password_hands_back_a_fresh_token(api):
    from lenny.core import auth
    ann = make("ann", "ann@example.org", is_super=False)
    old = auth.issue_admin_token(ann.id)
    r = api("PUT", f"/admin/librarians/{ann.id}", ann,
            json={"password": "a brand new passphrase", "current_password": PASSWORD})
    assert r.status_code == 200 and not auth.verify_admin_token(old)
    assert auth.admin_librarian(r.json()["token"]).id == ann.id


def _all_routes(app):
    """Every concrete route on `app`, whatever FastAPI wraps them in.

    Older FastAPI hands back one flat APIRoute per endpoint. Newer releases
    wrap each ``include_router`` in an opaque router object with no ``.path``,
    so walking ``app.routes`` finds nothing and the ``seen`` floor below is
    what notices. Expanding those wrappers keeps this test looking at the same
    routes on both.
    """
    for route in app.routes:
        expand = getattr(route, "effective_route_contexts", None)
        if expand is None:
            yield route
        else:
            yield from expand()


# ── every /admin route wants the secret AND a token ───────────────────────

def test_every_admin_route_refuses_half_a_pair(api):
    from lenny.app import app
    from lenny.core import auth
    make()
    token = {"Authorization": "Bearer " + auth.issue_admin_token()}
    secret = {"X-Admin-Internal-Secret": "s3cret"}
    c = TestClient(app, raise_server_exceptions=False)
    seen = 0
    for route in _all_routes(app):
        path = getattr(route, "path", "")
        if "/admin/" not in path and not path.endswith("/upload"):
            continue
        url = path
        for part in ("{book_id}", "{librarian_id}", "{loan_id}", "{batch_id}", "{row_id}", "{job_id}",
                     "{client_id}", "{olid}"):
            url = url.replace(part, "1")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            for headers in ({}, token, secret, {**secret, "Authorization": "Bearer junk"}):
                r = c.request(method, url, headers=headers, json={}, files=None)
                assert r.status_code in (401, 403, 422), (method, path, headers.keys(), r.status_code)
            seen += 1
    assert seen > 40
