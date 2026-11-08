"""Tests for get_authenticated_identity — the OAuth2 bearer-token fallback
that lets an `ol`-mode consumer (e.g. Open Library's backend, which presents
a bearer token rather than a Lenny session cookie for everything after the
initial browser login) get a personalized single-item OPDS response.

Before this, /opds/{book_id} only recognized a session cookie via
get_authenticated_email; a bearer token always looked unauthenticated to it,
so a patron's own loan never showed up as read/return links for that
consumer, even though the token was perfectly valid.
"""

import os

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import Item, Loan, FormatEnum  # noqa: E402
from lenny.core.oauth2 import AccessToken, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402
from lenny.routes.api import get_authenticated_identity  # noqa: E402

EMAIL = "bearer-test-patron@example.com"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_clients", "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


class FakeRequest:
    """Just enough of a Request for get_authenticated_identity's calls."""
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.client = None
        self.cookies = {}


def _issue_token(scope="loans:read"):
    client, _secret = OAuthClient.register(
        name="Test Consumer", redirect_uris=["opds://test"], scopes=[scope],
    )
    access_token, _refresh, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(EMAIL), scope=scope,
    )
    return access_token


# ─── get_authenticated_identity — unit behavior ──────────────────────────────

def test_cookie_session_wins_and_skips_bearer_lookup():
    from unittest.mock import patch

    with patch("lenny.routes.api.get_authenticated_email", return_value=EMAIL), \
         patch("lenny.routes.api.AccessToken.authenticate") as mock_auth:
        identity, is_hashed = get_authenticated_identity(FakeRequest(), "some-cookie-value")

    assert identity == EMAIL
    assert is_hashed is False
    mock_auth.assert_not_called()


def test_no_session_no_token_returns_none():
    identity, is_hashed = get_authenticated_identity(FakeRequest(), None)
    assert identity is None
    assert is_hashed is False


def test_valid_bearer_token_returns_patron_hash():
    token = _issue_token()
    identity, is_hashed = get_authenticated_identity(FakeRequest(), token)
    assert identity == hash_email(EMAIL)
    assert is_hashed is True


def test_bearer_token_missing_required_scope_is_rejected():
    token = _issue_token(scope="borrow")  # no loans:read
    identity, is_hashed = get_authenticated_identity(FakeRequest(), token)
    assert identity is None
    assert is_hashed is False


def test_garbage_token_returns_none():
    identity, is_hashed = get_authenticated_identity(FakeRequest(), "not-a-real-token")
    assert identity is None
    assert is_hashed is False


# ─── End-to-end: /opds/{book_id} with only a Bearer header, no cookie ────────

def test_opds_item_personalizes_for_a_bearer_authenticated_loan():
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from lenny.app import app

    item = Item(openlibrary_edition=910000001, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(item_id=item.id, patron_email_hash=hash_email(EMAIL)))
    db.commit()

    token = _issue_token()

    with patch("lenny.core.api.build_post_borrow_publication",
               return_value={"metadata": {"title": "personalized"}}) as mock_build:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/v1/api/opds/910000001",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_build.assert_called_once()
    assert resp.json() == {"metadata": {"title": "personalized"}}


def test_opds_item_not_personalized_for_a_different_patrons_token():
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from lenny.app import app

    item = Item(openlibrary_edition=910000002, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(item_id=item.id, patron_email_hash=hash_email(EMAIL)))
    db.commit()

    other_client, _ = OAuthClient.register(
        name="Other Consumer", redirect_uris=["opds://test"], scopes=["loans:read"],
    )
    other_token, _refresh, _row = AccessToken.issue(
        client_id=other_client.client_id,
        patron_email_hash=hash_email("someone-else@example.com"),
        scope="loans:read",
    )

    with patch("lenny.core.api.build_post_borrow_publication") as mock_build:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/v1/api/opds/910000002",
            headers={"Authorization": f"Bearer {other_token}"},
        )

    assert resp.status_code == 200
    # Not this patron's loan — must never reach the personalized branch.
    mock_build.assert_not_called()
