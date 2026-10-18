"""The OAuth2 session handoff: turn a grant into a browser session so a patron
can read (ArchiveLabs/lenny#254).

    POST /v1/api/oauth2/session   mint a single-use ticket  (bearer, scope `borrow`)
    GET  /v1/api/oauth2/session   redeem it, set `session` cookie, 303 to `redir`

These are unit/integration tests over SQLite via TestClient — they cover the
auth logic, single-use (sequentially), expiry, patron binding, redir validation,
scope, and that the token never reaches a log or the response body. The
genuinely-concurrent double-submit needs real Postgres and lives in
tests/test_oauth2_concurrency.py; SQLite serialises writes, so a double-spend is
structurally invisible here (see that module's docstring).

Red-first note: the security properties are proved to fail against a mutant in
the PR body; where a test would otherwise be unfalsifiable (e.g. "a caller-
supplied identity is ignored"), it is written so a good-faith identity parameter
added later makes it go red, per the review guidance.
"""

import datetime
import os

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import (  # noqa: E402
    SESSION_TICKET_TTL,
    AccessToken,
    OAuthClient,
    SessionTicket,
    _hash,
    _now,
)
from lenny.core.utils import hash_email  # noqa: E402

ME = "reader-patron@example.com"
OTHER = "other-patron@example.com"
SESSION = "/v1/api/oauth2/session"
EDITION = 910777001
READ_PATH = f"/v1/api/items/{EDITION}/read"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_session_tickets", "oauth_access_tokens", "oauth_clients",
                  "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


# ── helpers ──────────────────────────────────────────────────────────────────

def token_for(email, scope="borrow"):
    client, _ = OAuthClient.register(
        name="Reader", redirect_uris=["opds://test"],
        scopes=["loans:read", "borrow"], is_confidential=False)
    access, _r, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(email), scope=scope)
    return access


def mint(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app).post(SESSION, headers=headers)


def mint_ticket(email=ME, scope="borrow"):
    r = mint(token_for(email, scope))
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def redeem(ticket, redir=READ_PATH, headers=None, **extra):
    params = {}
    if ticket is not None:
        params["ticket"] = ticket
    if redir is not None:
        params["redir"] = redir
    params.update(extra)
    return TestClient(app).get(SESSION, params=params, headers=headers or {},
                               follow_redirects=False)


# Sec-Fetch header sets (forbidden headers a page cannot forge).
NAV_HEADERS = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
SUBRESOURCE_HEADERS = {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}


def set_cookie_header(resp, name="session"):
    """The raw Set-Cookie line for `name`, or None. (The cookie is Secure, so it
    won't land in response.cookies over http — inspect the header directly.)"""
    for k, v in resp.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{name}="):
            return v
    return None


def session_identity_value(resp):
    """The raw `session` cookie value a redeem set."""
    header = set_cookie_header(resp)
    assert header, "expected a session cookie"
    return header.split(";", 1)[0][len("session="):]


def session_identity(resp):
    """Decode the `session` cookie a redeem set, without the IP check."""
    return auth.verify_session_cookie(session_identity_value(resp))


def make_request(cookie_value, host="testclient"):
    return Request({
        "type": "http", "method": "POST", "path": "/v1/api/oauth2/authorize",
        "headers": [(b"cookie", f"session={cookie_value}".encode())],
        "client": (host, 12345), "query_string": b"", "scheme": "http",
        "server": ("testserver", 80),
    })


# ── mint: scope and authentication ───────────────────────────────────────────

def test_mint_requires_the_borrow_scope():
    """A session confers full browser authority (borrow/return, not just read),
    so a read-only token must not be able to mint one."""
    r = mint(token_for(ME, "loans:read"))
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "insufficient_scope"


def test_mint_refuses_an_absent_or_invalid_token():
    assert mint(None).status_code == 401
    assert mint("not-a-real-token").status_code == 401


def test_mint_returns_a_ticket_and_never_the_token():
    token = token_for(ME, "borrow")
    r = mint(token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"] and isinstance(body["ticket"], str)
    assert body["expires_in"] == SESSION_TICKET_TTL
    assert token not in r.text, "the access token must never appear in the response"
    assert r.headers.get("cache-control") == "no-store"


def test_mint_does_not_log_the_access_token(caplog):
    token = token_for(ME, "borrow")
    with caplog.at_level("DEBUG"):
        assert mint(token).status_code == 200
    assert token not in caplog.text, "the access token must appear in no log line"


def test_the_stored_ticket_holds_no_token_and_no_address():
    ticket = mint_ticket(ME)
    row = db.query(SessionTicket).filter(SessionTicket.ticket_hash == _hash(ticket)).one()
    # Only the hash of the ticket is stored, and the patron hash — never plaintext.
    assert row.ticket_hash != ticket
    assert row.patron_email_hash == hash_email(ME)
    assert ME not in (row.scope or "")


# ── redeem: cookie, single-use, expiry ───────────────────────────────────────

def test_redeem_sets_the_session_cookie_with_authorize_decision_flags():
    r = redeem(mint_ticket(ME))
    assert r.status_code == 303
    assert r.headers["location"] == READ_PATH
    header = set_cookie_header(r)
    assert header, "a session cookie must be set"
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header
    assert "Path=/" in header
    assert f"Max-Age={auth.COOKIE_TTL}" in header


def test_a_ticket_is_single_use():
    """Smoke test only. On SQLite (one shared connection) the second redemption's
    atomic claim returns rowcount 0 whether or not the `WHERE redeemed_at IS NULL`
    guard is present, so this cannot distinguish a guarded redeem from an
    unguarded one — it passes either way. The authoritative single-use proof,
    including the concurrent double-submit, is in tests/test_oauth2_concurrency.py
    against real Postgres, where forcing the claim to succeed turns both the
    sequential and concurrent tests red. Kept here as a fast end-to-end that the
    burned-ticket path refuses and sets no cookie."""
    ticket = mint_ticket(ME)
    first = redeem(ticket)
    assert first.status_code == 303 and set_cookie_header(first)
    second = redeem(ticket)
    assert second.status_code != 303, "a burned ticket must not establish a session"
    assert set_cookie_header(second) is None


def test_an_expired_ticket_is_refused():
    ticket = mint_ticket(ME)
    row = db.query(SessionTicket).filter(SessionTicket.ticket_hash == _hash(ticket)).one()
    row.expires_at = _now() - datetime.timedelta(seconds=1)
    db.commit()
    r = redeem(ticket)
    assert r.status_code != 303
    assert set_cookie_header(r) is None


# ── patron binding ───────────────────────────────────────────────────────────

def test_the_session_is_bound_to_the_tickets_patron():
    """A ticket minted for A establishes a session for A, and a ticket minted for
    B establishes one for B — both tickets are real, so this test can fail."""
    a = session_identity(redeem(mint_ticket(ME)))
    b = session_identity(redeem(mint_ticket(OTHER)))
    assert a["patron_email_hash"] == hash_email(ME)
    assert b["patron_email_hash"] == hash_email(OTHER)
    assert a["patron_email_hash"] != b["patron_email_hash"]


def test_a_caller_supplied_identity_is_ignored():
    """The redeem path derives identity ONLY from the ticket. Sending a correct
    address and a correctly-computed hash for a DIFFERENT patron alongside a valid
    ticket must change nothing. Written so that if an identity parameter is ever
    honoured in good faith, this goes red."""
    ticket = mint_ticket(ME)
    r = redeem(ticket, email=OTHER, patron_email_hash=hash_email(OTHER))
    assert r.status_code == 303
    assert session_identity(r)["patron_email_hash"] == hash_email(ME)


def test_identity_without_a_ticket_establishes_no_session():
    """A valid-looking identity and no ticket (or a burned/expired one) must set
    no session. Catches a future refactor that lets identity substitute for a
    ticket."""
    no_ticket = redeem(None, email=ME, patron_email_hash=hash_email(ME))
    assert no_ticket.status_code != 303
    assert set_cookie_header(no_ticket) is None

    burned = mint_ticket(ME)
    assert redeem(burned).status_code == 303
    reused = redeem(burned, email=ME, patron_email_hash=hash_email(ME))
    assert reused.status_code != 303
    assert set_cookie_header(reused) is None


# ── redir validation (path-only, same-origin) ────────────────────────────────

@pytest.mark.parametrize("bad", [
    "https://evil.example.org/x",      # absolute URL
    "http://evil.example.org/x",
    "//evil.example.org/x",            # protocol-relative
    "/\\evil.example.org/x",           # backslash protocol-relative
    "javascript:alert(1)",             # scheme, not a path
    "/path\\to",                       # backslash
    "/path with space",                # raw whitespace
    "relative/path",                   # not absolute
    "",                                # empty
    None,                              # missing
])
def test_redeem_rejects_an_unsafe_redir(bad):
    """Enumerated bypasses, not one-and-generalise. A valid ticket plus a bad
    redir must refuse and set no cookie — and must not spend the ticket."""
    ticket = mint_ticket(ME)
    r = redeem(ticket, redir=bad)
    assert r.status_code != 303, f"{bad!r} should be refused"
    assert set_cookie_header(r) is None
    # The ticket was not burned, so it still works with a good redir.
    assert redeem(ticket, redir="/v1/api/items/1/read").status_code == 303


def test_redeem_accepts_a_same_origin_path():
    r = redeem(mint_ticket(ME), redir="/v1/api/items/42/read")
    assert r.status_code == 303
    assert r.headers["location"] == "/v1/api/items/42/read"


# ── top-level-navigation gate (login-CSRF / fixation hardening) ───────────────

def test_redeem_refuses_an_embedded_cross_site_request():
    """Setting a session cookie must only happen for a person deliberately
    following the link. A browser that sends Sec-Fetch headers marking the
    request as an embedded/background sub-resource (an <img>, iframe, fetch) must
    be refused — otherwise a third party could silently establish a session in
    someone else's browser (ArchiveLabs/lenny#254)."""
    r = redeem(mint_ticket(ME), headers=SUBRESOURCE_HEADERS)
    assert r.status_code != 303, "an embedded cross-site request must not set a session"
    assert set_cookie_header(r) is None


def test_redeem_refuses_a_speculative_prefetch():
    r = redeem(mint_ticket(ME), headers={**NAV_HEADERS, "sec-purpose": "prefetch"})
    assert r.status_code != 303
    assert set_cookie_header(r) is None


def test_redeem_allows_a_top_level_navigation():
    r = redeem(mint_ticket(ME), headers=NAV_HEADERS)
    assert r.status_code == 303
    assert set_cookie_header(r)


def test_redeem_fails_open_when_sec_fetch_absent():
    """A pre-Sec-Fetch browser (which an attacker does not get to choose for a
    victim) still reads — the guard must not break old browsers."""
    r = redeem(mint_ticket(ME), headers={})
    assert r.status_code == 303
    assert set_cookie_header(r)


# ── client liveness at redeem ─────────────────────────────────────────────────

def test_a_disabled_client_cannot_redeem_its_ticket():
    """A ticket is single-use and ~60s, but a consumer can be disabled in that
    window; a disabled client's ticket must not still mint a session."""
    ticket = mint_ticket(ME)
    row = db.query(SessionTicket).filter(SessionTicket.ticket_hash == _hash(ticket)).one()
    OAuthClient.disable(row.client_id)
    db.commit()
    r = redeem(ticket)  # no Sec-Fetch headers → passes the nav gate (fail-open)
    assert r.status_code != 303, "a disabled client's ticket must not establish a session"
    assert set_cookie_header(r) is None


# ── containment: a derived session cannot authorize a further grant ──────────

def test_a_hash_session_cannot_be_the_resource_owner_at_authorize():
    """DELIBERATE containment (#254): _authenticated_patron returns the patron for
    a real (plaintext) login but None for a ticket-derived hash session, so a
    session minted from a grant cannot, in turn, authorize a NEW grant to another
    consumer — consent always traces to a real sign-in. The plaintext case is the
    control that proves the function would see a session; make _authenticated_patron
    hash-aware and this test goes red."""
    from lenny.routes.oauth2 import _authenticated_patron
    email_cookie = auth.create_session_cookie(ME, "testclient")
    hash_cookie = auth.create_hashed_session_cookie(hash_email(ME), "testclient")
    assert _authenticated_patron(make_request(email_cookie)) == ME
    assert _authenticated_patron(make_request(hash_cookie)) is None


# ── outcome: the derived session actually personalises the patron ────────────

def test_the_derived_session_authenticates_the_patron_for_reading():
    """The point of the feature: after redeeming, the hash session resolves to the
    patron through get_authenticated_identity — flagged hashed — and the loan the
    reader path checks is found via that hashed identity. This is exactly the
    identity plumbing the reader hops use; if the identity layer failed to
    recognise the hash cookie it would return (None, False) and this goes red.

    The reader hops themselves (/items/{id}/read and the readium manifest) also
    need #253's auth_check and are exercised in the real-HTTP wire suite; here we
    prove the plumbing network-free, plus that a cookie-gated endpoint treats the
    session as authenticated (200, not the 401 auth-document)."""
    from lenny.routes.api import get_authenticated_identity

    item = Item(openlibrary_edition=EDITION, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    Loan.create(item.id, hash_email(ME), hashed=True)
    db.commit()

    value = session_identity_value(redeem(mint_ticket(ME)))
    req = make_request(value)
    identity, hashed = get_authenticated_identity(req, session=value)
    assert (identity, hashed) == (hash_email(ME), True)
    assert Loan.exists(item.id, identity, hashed=hashed) is not None, \
        "the reader path's loan check must find the loan via the hashed identity"

    # And through the real stack: a cookie-gated endpoint authenticates the
    # session rather than returning the 401 auth document.
    r = TestClient(app).get("/v1/api/shelf", cookies={"session": value})
    assert r.status_code == 200, r.text
