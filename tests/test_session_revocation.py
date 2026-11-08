"""Logging out must end the session for good, not just clear the browser's copy (the token is
stateless: a copy someone already holds stays valid until it expires)."""

import os

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    try:
        db.execute(sqlalchemy.text("DELETE FROM cache"))
        db.commit()
    except Exception:
        db.rollback()
    db.remove()


def test_a_token_stops_working_after_logout():
    token = auth.create_session_cookie("reader@example.org")
    assert auth.verify_session_cookie(token) and auth.get_authenticated_email(token) == "reader@example.org"
    assert auth.revoke_session_cookie(token) is True
    assert auth.verify_session_cookie(token) is None
    assert auth.get_authenticated_email(token) is None


def test_logout_only_ends_that_session():
    mine, other = auth.create_session_cookie("a@example.org"), auth.create_session_cookie("a@example.org", ip="1.2.3.4")
    auth.revoke_session_cookie(mine)
    assert auth.verify_session_cookie(other) is not None


@pytest.mark.parametrize("junk", [None, "", "not-a-token", "abc.def.ghi"])
def test_junk_is_never_stored(junk):
    assert auth.revoke_session_cookie(junk) is False
    assert db.execute(sqlalchemy.text("SELECT count(*) FROM cache WHERE scope = 'revoked_session'")).scalar() == 0


@pytest.mark.parametrize("send", ["cookie", "bearer"])
def test_logout_route_revokes_the_token_it_was_sent_with(send):
    token = auth.create_session_cookie("reader@example.org")
    http = TestClient(app, base_url="https://testserver")
    kw = {"cookies": {"session": token}} if send == "cookie" else {"headers": {"Authorization": f"Bearer {token}"}}
    assert http.get("/v1/api/profile", **kw).status_code == 200
    assert http.post("/v1/api/logout", **kw).status_code == 200
    again = TestClient(app, base_url="https://testserver").get(
        "/v1/api/profile", **({"cookies": {"session": token}} if send == "cookie" else {"headers": {"Authorization": f"Bearer {token}"}}))
    assert again.status_code == 401, "a copy of the token must not survive logout"
