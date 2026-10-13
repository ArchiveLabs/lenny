"""POST /v1/api/oauth2/return finalises a loan for a bearer token with `borrow`.

The mirror of POST /v1/api/oauth2/borrow. A consumer (Open Library) holds only
an access token and the patron's email *hash*, never the address, so the return
has to run through the hashed loan-lookup path — the place a careless
implementation (an `unborrow` that re-hashes an already-hashed value) silently
returns nothing.

`return_item` in api.py is a different surface: it is the OPDS route, whose
`@requires_item_auth()` wrapper resolves an OPDS client's session-cookie bearer
through `verify_session_cookie`. This file is about the OAuth2 surface, which is
what was missing.
"""

import os

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

ME = "return-patron@example.com"
OTHER = "other-return-patron@example.com"
EDITION = 910000999


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


def give_loan(email):
    """Create an active loan the way the hashed resource path does — the loan
    row is keyed on `hash_email(email)`, which is also what the token carries."""
    it = db.query(Item).filter(Item.openlibrary_edition == EDITION).first()
    it.borrow(hash_email(email), hashed=True)


def do_return(token=None, edition=EDITION):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app).post("/v1/api/oauth2/return", headers=headers,
                                data={"edition_id": edition})


def active_loans(email):
    db.expire_all()
    return db.query(Loan).filter(
        Loan.patron_email_hash == hash_email(email),
        Loan.returned_at == None,
    ).all()


def test_a_borrow_token_returns_a_loan_it_holds(item):
    give_loan(ME)
    assert len(active_loans(ME)) == 1, "precondition: the loan exists before return"
    r = do_return(token_for(ME, "borrow"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "returned"
    assert active_loans(ME) == [], "the loan must be finalised (returned_at set)"


def test_the_hashed_path_is_what_actually_runs(item):
    """The token carries `hash_email(ME)` and the loan row is stored under that
    same hash. An `unborrow` that forwarded the value as a plaintext email would
    hash it a second time and find nothing — a 404 on a loan that exists. This
    pins that the return reaches the specific loan row by its hash."""
    give_loan(ME)
    loan_id = active_loans(ME)[0].id
    r = do_return(token_for(ME, "borrow"))
    assert r.status_code == 200, r.text
    db.expire_all()
    refreshed = db.query(Loan).filter(Loan.id == loan_id).first()
    assert refreshed.returned_at is not None, "the exact loan row must be finalised"


def test_returning_a_loan_you_do_not_hold_is_404_and_changes_nothing(item):
    give_loan(OTHER)                         # OTHER holds the only copy
    r = do_return(token_for(ME, "borrow"))   # ME holds no loan on this edition
    assert r.status_code == 404, r.text
    # Assert the *no-active-loan* error specifically, not merely a 404 — an
    # absent route also 404s, which would pass this test for the wrong reason.
    assert r.json()["error"] == "no_active_loan", r.text
    assert len(active_loans(OTHER)) == 1, "another patron's loan must be untouched"


def test_a_read_only_token_cannot_return(item):
    """loans:read lets a consumer look; it must never let it act. The scope
    check must precede any state change."""
    give_loan(ME)
    r = do_return(token_for(ME, "loans:read"))
    assert r.status_code == 403
    assert r.json()["error"] == "insufficient_scope"
    assert len(active_loans(ME)) == 1, "an under-scoped token must not finalise the loan"


def test_a_made_up_token_cannot_return(item):
    give_loan(ME)
    r = do_return("not-a-real-token")
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"
    assert len(active_loans(ME)) == 1


def test_no_credentials_cannot_return(item):
    give_loan(ME)
    r = do_return()
    assert r.status_code == 401
    assert len(active_loans(ME)) == 1


def test_return_on_a_missing_edition_is_404(item):
    r = do_return(token_for(ME, "borrow"), edition=999999999)
    assert r.status_code == 404
    # The library-does-not-hold-it error, distinct from an absent route's 404.
    assert r.json()["error"] == "not_found", r.text


def test_a_disabled_client_cannot_return(item):
    give_loan(ME)
    token = token_for(ME, "borrow")
    OAuthClient.disable(AccessToken.authenticate(token).client_id)
    assert do_return(token).status_code == 401
    assert len(active_loans(ME)) == 1


def test_returning_twice_is_a_404_the_second_time(item):
    """Idempotency is not silently swallowed: once the loan is finalised there is
    no active loan to return, so a replay gets the same 404 as any other
    return-of-nothing rather than a second 200."""
    give_loan(ME)
    token = token_for(ME, "borrow")
    assert do_return(token).status_code == 200
    assert do_return(token).status_code == 404
