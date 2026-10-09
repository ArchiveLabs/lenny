"""GET /items/{id}/borrow accepts an OAuth2 bearer token with the `borrow` scope.

A reading app that finished the PKCE flow calls the borrow link with its access
token. The endpoint only understood the login cookie, so the book was never
borrowed even though the patron had approved exactly that on the consent screen.
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
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import AccessToken, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

ME = "borrow-patron@example.com"
OTHER = "other-patron@example.com"
EDITION = 910000888


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


@pytest.fixture
def item():
    it = Item(openlibrary_edition=EDITION, encrypted=True, formats=FormatEnum.EPUB)
    db.add(it)
    db.commit()
    return it


def token_for(email, scope):
    client, _ = OAuthClient.register(
        name="Reader", redirect_uris=["opds://test"], scopes=[scope], is_confidential=False)
    access, _r, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(email), scope=scope)
    return access


def borrow(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # The response is a publication built from an Open Library lookup; the test is
    # about who may borrow, so keep it off the network.
    with patch("lenny.routes.api.build_post_borrow_publication", return_value={"metadata": {"title": "x"}}):
        return TestClient(app).get(f"/v1/api/items/{EDITION}/borrow", headers=headers,
                                   follow_redirects=False)


def loans_of(email):
    db.expire_all()
    return db.query(Loan).filter(Loan.patron_email_hash == hash_email(email)).all()


def test_a_token_with_the_borrow_scope_borrows_the_book(item):
    r = borrow(token_for(ME, "borrow"))
    assert r.status_code == 200, r.text
    assert len(loans_of(ME)) == 1, "the patron approved borrowing, so the loan must exist"


def test_borrowing_twice_does_not_create_a_second_loan(item):
    token = token_for(ME, "borrow")
    assert borrow(token).status_code == 200
    assert borrow(token).status_code == 200
    assert len(loans_of(ME)) == 1


def test_attack_a_read_only_token_cannot_borrow(item):
    """loans:read lets an app look; it must never let it act."""
    r = borrow(token_for(ME, "loans:read"))
    assert r.status_code == 401
    assert loans_of(ME) == []


def test_attack_a_made_up_token_cannot_borrow(item):
    assert borrow("not-a-real-token").status_code == 401
    assert loans_of(ME) == []


def test_attack_no_credentials_cannot_borrow(item):
    assert borrow().status_code == 401


def test_attack_a_token_borrows_only_for_its_own_patron(item):
    borrow(token_for(ME, "borrow"))
    assert loans_of(OTHER) == []


def test_attack_a_disabled_client_cannot_borrow(item):
    token = token_for(ME, "borrow")
    OAuthClient.disable(AccessToken.authenticate(token).client_id)
    assert borrow(token).status_code == 401
    assert loans_of(ME) == []


def test_the_per_patron_limit_still_applies_to_a_token(item, monkeypatch):
    """Lending policy lives in Item.borrow, which the token path now reaches."""
    from lenny import configs
    monkeypatch.setattr(configs, "get_loan_limit", lambda: 0)
    assert borrow(token_for(ME, "borrow")).status_code == 403
    assert loans_of(ME) == []
