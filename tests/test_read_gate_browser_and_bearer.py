"""The read gate serves browsers a sign-in redirect and honours bearer tokens.

ArchiveLabs/lenny#253. `requires_item_auth` guards `/items/{id}/read`,
`/readium/manifest.json`, `/readium/{path}` and `/items/{id}/return`. Before the
fix it returned the OPDS authentication document on *every* failed check, with no
content negotiation and no bearer support, so:

  * a browser navigation (no `Authorization` header) that had lost its cookie
    rendered a tab full of raw JSON instead of a sign-in page, and
  * an OAuth2 token-bearing caller — the credential Open Library holds — looked
    anonymous, even though the four sibling routes already accepted it.

These tests pin the fixed behaviour at the HTTP boundary (status line, Location,
Content-Type, body): browsers are redirected into the real sign-in / borrow flow,
native OPDS clients still get the authentication document byte-for-byte, and a
scoped bearer token reads and returns. They run on sqlite (`TESTING`), in-process
via TestClient; the companion wire-level transcript against a running node lives
in the PR body, because the defect is a status code and a Content-Type on the
wire and a green in-process suite does not, by itself, prove the wire.
"""

import os

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "read-gate-browser-bearer-seed-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.api import LennyAPI, auth_document  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import AccessToken, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

PATRON = "reader@example.org"
OTHER = "stranger@example.org"
EDITION = 970001234  # the openlibrary_edition in the URL path
ITEM_ID = 1          # Loan rows key on Item.id, not the edition

READ = f"/v1/api/items/{EDITION}/read"
RETURN = f"/v1/api/items/{EDITION}/return"
BORROW = f"/v1/api/items/{EDITION}/borrow"

BROWSER = {"Accept": "text/html,application/xhtml+xml"}
OPDS = {"Accept": "application/opds+json"}


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
    """An encrypted item (is_login_required == encrypted), so the gate engages."""
    it = Item(id=ITEM_ID, openlibrary_edition=EDITION, encrypted=True,
              formats=FormatEnum.EPUB)
    db.add(it)
    db.commit()
    return it


def give_loan(email, hashed=False):
    digest = email if hashed else hash_email(email)
    db.add(Loan(item_id=ITEM_ID, patron_email_hash=digest))
    db.commit()


def cookie_for(email):
    """A valid first-party session cookie (plaintext-email, no IP binding)."""
    return {"session": auth.create_session_cookie(email)}


def token_for(email, scope):
    client, _ = OAuthClient.register(
        name="Reader", redirect_uris=["opds://test"], scopes=[scope],
        is_confidential=False)
    access, _r, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(email), scope=scope)
    return {"Authorization": f"Bearer {access}"}


def client():
    return TestClient(app, follow_redirects=False)


# ── Defect 1: browsers get a sign-in redirect, OPDS clients get the document ──

def test_browser_without_credentials_is_redirected_to_sign_in(item):
    """A browser with no cookie and no token must be sent into the auth flow,
    carrying the read path back so it lands in the reader after signing in —
    NOT handed a 401 wall of OPDS JSON."""
    r = client().get(READ, headers=BROWSER)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    # The node's own auth document advertises /oauth/authorize as rel=authenticate;
    # the browser branch sends the patron exactly where a native client would go.
    assert loc.startswith("/v1/api/oauth/authorize") or \
        loc.startswith("/v1/api/oauth/external/start"), loc
    # and the originally requested path round-trips as the post-login target.
    assert "read" in loc and str(EDITION) in loc, loc
    # the dead route must never appear.
    assert "/v1/api/authenticate" not in loc, loc


def test_native_opds_client_gets_the_auth_document_unchanged(item):
    """A native OPDS client (no text/html) still receives the OPDS authentication
    document, byte-for-byte what the library produces today."""
    r = client().get(READ, headers=OPDS)
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/opds-authentication+json")
    assert r.json() == auth_document(), "auth document body changed"


def test_opds_client_that_explicitly_refuses_html_still_gets_the_document(item):
    """An OPDS client that lists `text/html;q=0` (prefers OPDS, explicitly does
    not want html) must receive the authentication document, not a browser
    redirect. A substring `"text/html" in accept` check gets this wrong; content
    negotiation has to honour q-values."""
    r = client().get(READ, headers={"Accept": "application/opds+json, text/html;q=0"})
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/opds-authentication+json")
    assert r.json() == auth_document()


def test_wildcard_accept_gets_opds_not_a_browser_redirect(item):
    """A programmatic `fetch()` sends `*/*`; it must get the OPDS 401, never an
    HTML redirect — `*/*` is not a preference for html."""
    r = client().get(READ, headers={"Accept": "*/*"})
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/opds-authentication+json")


def test_opds_body_is_byte_identical_to_a_direct_auth_document_render(item):
    """Belt-and-braces on 'unchanged': the decorator's 401 body is byte-for-byte
    what a plain `JSONResponse(auth_document())` renders (same serializer the
    route has always used), so nothing in the new browser branch leaked into the
    OPDS path."""
    from fastapi.responses import JSONResponse
    expected = JSONResponse(content=auth_document()).body
    r = client().get(READ, headers=OPDS)
    assert r.content == expected, "OPDS auth-document bytes changed"


# ── Both failure branches: unauthenticated AND unauthorized (no active loan) ──

def test_unauthorized_browser_is_redirected_to_borrow(item):
    """Signed in (cookie) but no active loan: a browser goes to the borrow flow,
    not a JSON wall. This is the branch most likely to be skipped."""
    r = client().get(READ, headers=BROWSER, cookies=cookie_for(PATRON))
    assert r.status_code == 303, r.text
    assert r.headers["location"] == BORROW, r.headers["location"]


def test_unauthorized_opds_still_gets_the_auth_document(item):
    """Signed in, no loan, native OPDS client → the authentication document."""
    r = client().get(READ, headers=OPDS, cookies=cookie_for(PATRON))
    assert r.status_code == 401, r.text
    assert r.json() == auth_document()


# ── Defect 2: a scoped bearer token reads (and returns) ──

def test_bearer_token_with_a_loan_reaches_the_reader(item):
    """A loans:read token whose patron holds the loan reaches the reader (307)."""
    give_loan(PATRON)
    r = client().get(READ, headers=token_for(PATRON, "loans:read"))
    assert r.status_code == 307, r.text
    assert "/read/manifest/" in r.headers["location"], r.headers["location"]


def test_bearer_token_without_the_loans_read_scope_is_refused(item):
    """A token missing loans:read must not read, even with a live loan."""
    give_loan(PATRON)
    r = client().get(READ, headers={**token_for(PATRON, "borrow"), **OPDS})
    assert r.status_code == 401, r.text
    assert r.json() == auth_document()


def test_bearer_token_for_another_patron_cannot_read_this_loan(item):
    """The loan belongs to PATRON; OTHER's token must not open it."""
    give_loan(PATRON)
    r = client().get(READ, headers={**token_for(OTHER, "loans:read"), **OPDS})
    assert r.status_code == 401, r.text


def test_made_up_token_is_refused(item):
    give_loan(PATRON)
    r = client().get(READ, headers={"Authorization": "Bearer not-a-real-token", **OPDS})
    assert r.status_code == 401, r.text


# ── return_item: borrow scope, and the hashed-identity threading ──

def test_return_with_a_borrow_token_finalises_the_loan(item):
    """A borrow-scoped token returns a loan held under its OWN email hash. This
    only passes if the decorator threads email_hashed=True through to
    unborrow(); passing the hash as plaintext would re-hash it and 404."""
    give_loan(PATRON, hashed=True)  # mirror how a token's identity is stored
    # loan stored under hash_email(PATRON); token carries that same hash
    db.query(Loan).delete(); db.commit()
    give_loan(PATRON)  # == Loan(patron_email_hash=hash_email(PATRON))
    r = client().get(RETURN, headers=token_for(PATRON, "borrow"))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(Loan).filter(
        Loan.item_id == ITEM_ID, Loan.returned_at.isnot(None)).count() == 1


def test_return_with_a_read_only_token_is_refused(item):
    """Returning is an action: loans:read may look but not return."""
    give_loan(PATRON)
    r = client().get(RETURN, headers={**token_for(PATRON, "loans:read"), **OPDS})
    assert r.status_code == 401, r.text
    db.expire_all()
    assert db.query(Loan).filter(
        Loan.item_id == ITEM_ID, Loan.returned_at.is_(None)).count() == 1


# ── The discriminator guard ada-c8 required (red via the default-False mutation) ──

def test_auth_check_refuses_to_run_without_an_explicit_hashed_flag(item):
    """auth_check must make a forgotten discriminator a loud TypeError, never a
    silent plaintext comparison against a hash. Red-first is shown by mutating
    the signature back to `email_hashed=False`: this call then returns an
    'unauthorized' dict instead of raising, and this test goes red."""
    with pytest.raises(TypeError):
        LennyAPI.auth_check(item, email=hash_email(PATRON))  # email_hashed omitted
