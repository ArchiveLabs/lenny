"""`login_hint`: what it is allowed to do, and what must still be impossible.

RFC 6749 §3.1.2.1. Open Library has already proved the patron's address, so it
passes it along and the patron is spared retyping it.

Since #236 the hint only pre-filled the email box, because acting on it would
turn an unauthenticated GET into an outbound mailer aimed at a caller-chosen
address. That reasoning was right about the *bare* login route and too broad
about the consumer flow, so the surface is now split in two and only one half
can send:

  * `/v1/api/oauth/authorize?login_hint=…` — the public OPDS login route.
    Pre-fill only, exactly as before. Nothing here may ever send mail.

  * the same route carrying `ctx`, a signed authorization context that only
    `/v1/api/oauth2/authorize` can mint. May send, and only when every one of
    these holds: the signature verifies, the request is a real top-level
    browser navigation, and the context's send id has not already been spent.

The tests below are written against those conditions one at a time, because a
single green "it sends" would not say which of them is load-bearing.
"""

import os
import re
from urllib.parse import urlencode, urlparse, parse_qs

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "login-hint-test-seed-32-chars-ok")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.cache import CacheEntry  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402
from lenny.routes.oauth import _login_hint  # noqa: E402

REDIRECT = "https://openlibrary.org/borrow/lenny/callback"
PATRON = "patron@example.org"

# What a browser sends when a person follows a link or is redirected to a page.
# `Sec-Fetch-*` are forbidden header names: page JavaScript cannot set them, so
# an `<img src>` or a `fetch()` cannot forge this shape from someone's browser.
NAVIGATION = {
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "sec-fetch-site": "cross-site",
}


@pytest.fixture(autouse=True)
def lending_on(monkeypatch):
    """The OTP page 503s unless lending is configured (`_require_lending`).

    Only the gate is stubbed. `OTP.issue` is left real wherever a test is
    asserting that nothing was sent, or the assertion would be vacuous.
    """
    monkeypatch.setattr("lenny.routes.oauth._require_lending", lambda: None)


@pytest.fixture(autouse=True)
def clean_cache():
    """Send throttles and spent send-ids live in `cache`.

    Without this they leak between tests and the fifth test in a file starts
    life rate-limited — which fails in a way that looks like the feature is
    broken rather than like the fixture is.
    """
    Base.metadata.create_all(engine)
    db.query(CacheEntry).delete()
    db.commit()
    yield
    db.query(CacheEntry).delete()
    db.commit()
    db.remove()


@pytest.fixture
def client():
    Base.metadata.create_all(engine)
    obj, _ = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    yield obj
    db.query(OAuthClient).delete()
    db.commit()


@pytest.fixture
def outbox(monkeypatch):
    """Every address `OTP.issue` was asked to mail, in order."""
    sent = []
    monkeypatch.setattr("lenny.core.auth.OTP.issue",
                        classmethod(lambda cls, email, ip: sent.append(email)))
    return sent


def authorize_url(client, **extra):
    params = {
        "client_id": client.client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "loans:read borrow",
        "state": "xyz",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    params.update(extra)
    return "/v1/api/oauth2/authorize?" + urlencode(params)


def login_location(client, **extra):
    """The `/oauth/authorize` URL that `/oauth2/authorize` bounces an
    unauthenticated patron to. Taken from the real redirect rather than built
    by hand, so the signed context under test is one the server actually
    minted."""
    c = TestClient(app, follow_redirects=False)
    r = c.get(authorize_url(client, **extra))
    assert r.status_code in (302, 303), f"expected a bounce, got {r.status_code}"
    return r.headers["location"]


class TestItSurvivesTheRoundTrip:
    def test_authorize_bounces_to_login_with_a_signed_context(self, client):
        location = login_location(client, login_hint=PATRON)
        assert "/v1/api/oauth/authorize" in location
        ctx = parse_qs(urlparse(location).query).get("ctx")
        assert ctx, "no signed context was minted for the login hop"
        # The address must not ride along in the clear beside the signed blob:
        # a second, unsigned copy is one an attacker could edit.
        assert "patron%40example.org" not in location

    def test_the_login_page_prefills_the_address(self, client):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize",
                          "login_hint": PATRON})
        assert r.status_code == 200
        assert f'value="{PATRON}"' in r.text

    def test_no_hint_leaves_the_field_empty(self, client):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize"})
        assert r.status_code == 200
        assert 'value=""' in r.text


class TestTheCodeIsSentOnArrival:
    """The thing that was asked for: no email step when the consumer already
    told us the address."""

    def test_a_navigated_arrival_sends_and_asks_for_the_code(self, client, outbox):
        c = TestClient(app, follow_redirects=False)
        r = c.get(login_location(client, login_hint=PATRON), headers=NAVIGATION)
        assert r.status_code == 200
        assert outbox == [PATRON], f"expected one code to {PATRON}, sent {outbox}"
        # The patron is asked for the code, not for the address. The visible
        # email box is `id="email"`; otp_redeem keeps the address only as a
        # hidden field, so that id is the thing that must be gone.
        assert 'id="otpForm"' in r.text, "the code-entry form was not rendered"
        assert 'id="email"' not in r.text, "still asking for the address"
        assert PATRON in r.text

    def test_the_page_says_where_the_code_went(self, client, outbox):
        c = TestClient(app, follow_redirects=False)
        r = c.get(login_location(client, login_hint=PATRON), headers=NAVIGATION)
        body = re.sub(r"<[^>]+>", "", r.text)
        assert "sent" in body.lower() and PATRON in body


class TestOnlyARealNavigationMaySend:
    """`Sec-Fetch-*` is what separates "a person followed a link" from "a page
    somewhere embedded this URL". Both arrive as a GET carrying a valid
    context; only the first may send."""

    @pytest.mark.parametrize("headers,why", [
        ({}, "no Sec-Fetch signal at all (curl, or an old browser)"),
        ({"sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}, "an <img src>"),
        ({"sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}, "a fetch()/XHR"),
        ({"sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}, "an <iframe>"),
        ({"sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
          "sec-purpose": "prefetch"}, "a browser prefetch"),
    ])
    def test_a_non_navigation_falls_back_to_the_email_form(
            self, client, outbox, headers, why):
        c = TestClient(app, follow_redirects=False)
        r = c.get(login_location(client, login_hint=PATRON), headers=headers)
        assert outbox == [], f"{why} sent mail to {outbox}"
        # Degrades to the pre-fill screen rather than failing: a patron on a
        # browser with no Sec-Fetch support must still be able to sign in.
        assert r.status_code == 200
        assert f'value="{PATRON}"' in r.text


class TestItCannotSendMail:
    """The properties #236 established, which splitting the surface must not
    give up."""

    def test_attack_the_bare_login_route_never_sends(self, client, outbox):
        """An `<img src>` or a link to the public OPDS login route, with a
        hint and no signed context. This is the open-relay case."""
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize",
                          "login_hint": "victim@example.org"},
                  headers=NAVIGATION)
        assert outbox == [], f"the bare login route sent mail to {outbox}"
        assert r.status_code == 200

    @pytest.mark.parametrize("forged", [
        "garbage",
        "eyJoIjoidmljdGltQGV4YW1wbGUub3JnIn0.aaaaaa.bbbbbbbbbbbbbbbbbbbbbbbb",
        "",
    ])
    def test_attack_a_forged_context_never_sends(self, client, outbox, forged):
        """The context is the only thing standing between a GET and the
        mailer, so an unsigned or tampered one must buy nothing."""
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize",
                          "ctx": forged, "login_hint": "victim@example.org"},
                  headers=NAVIGATION)
        assert outbox == [], f"a forged context sent mail to {outbox}"
        assert r.status_code == 200

    def test_attack_replaying_one_context_sends_exactly_one_code(self, client, outbox):
        """A reload, a back button, or a replayed URL must not re-send. The
        send id is spent on first use, so the second arrival renders the same
        page without mailing again."""
        location = login_location(client, login_hint=PATRON)
        c = TestClient(app, follow_redirects=False)
        first = c.get(location, headers=NAVIGATION)
        second = c.get(location, headers=NAVIGATION)
        assert outbox == [PATRON], f"replay sent {len(outbox)} codes: {outbox}"
        assert first.status_code == 200 and second.status_code == 200
        # The second arrival still has to be usable — it is a patron pressing
        # reload, not an attacker.
        assert 'name="otp"' in second.text

    def test_attack_a_fresh_signin_drops_the_hint(self, client, outbox):
        """"Not you? Use a different account" exists to get away from the
        hinted address. Carrying it through — let alone mailing it — would
        send the patron straight back to the account they just rejected."""
        location = login_location(client, login_hint=PATRON,
                                  prompt="select_account")
        c = TestClient(app, follow_redirects=False)
        r = c.get(location, headers=NAVIGATION)
        assert outbox == [], f"a fresh sign-in mailed {outbox}"
        assert r.status_code == 200
        assert f'value="{PATRON}"' not in r.text


class TestTheRecipientIsRateLimited:
    """Per-address, so it holds however many sources the caller spreads over —
    which is the half nginx's per-IP zone structurally cannot do."""

    def test_a_single_address_cannot_be_mailed_without_limit(self, client, monkeypatch):
        from lenny.core import auth
        from lenny.core.exceptions import RateLimitError

        monkeypatch.setattr(auth.OTP, "_post",
                            classmethod(lambda cls, *a, **k: {"success": True}))
        for i in range(auth.EMAIL_REQUEST_LIMIT):
            auth.OTP.issue(PATRON, "1.2.3.4")
        with pytest.raises(RateLimitError):
            auth.OTP.issue(PATRON, "1.2.3.4")

    def test_the_limit_is_per_address_not_global(self, client, monkeypatch):
        from lenny.core import auth
        from lenny.core.exceptions import RateLimitError

        monkeypatch.setattr(auth.OTP, "_post",
                            classmethod(lambda cls, *a, **k: {"success": True}))
        for i in range(auth.EMAIL_REQUEST_LIMIT):
            auth.OTP.issue(PATRON, "1.2.3.4")
        # A different patron is unaffected.
        auth.OTP.issue("someone-else@example.org", "1.2.3.4")
        with pytest.raises(RateLimitError):
            auth.OTP.issue(PATRON, "1.2.3.4")

    def test_the_limit_folds_case_like_delivery_does(self, client, monkeypatch):
        """A change of case is the same mailbox and must not buy a fresh budget.
        The grant keys on hash_email (.strip().lower()); if the send cap keyed
        on the raw string instead, `PATRON@...` would get its own 5 sends after
        `patron@...` was exhausted — a per-recipient cap case-variants walk
        straight through."""
        from lenny.core import auth
        from lenny.core.exceptions import RateLimitError

        monkeypatch.setattr(auth.OTP, "_post",
                            classmethod(lambda cls, *a, **k: {"success": True}))
        for i in range(auth.EMAIL_REQUEST_LIMIT):
            auth.OTP.issue(PATRON.lower(), "1.2.3.4")
        with pytest.raises(RateLimitError):
            auth.OTP.issue(PATRON.upper(), "1.2.3.4")


class TestTheHintIsShapeChecked:
    @pytest.mark.parametrize("bad", [
        "", "   ", "not-an-email", "@example.org", "patron@",
        "patron@example.org\nBcc: victim@example.org",   # header-ish junk
        "a" * 250 + "@example.org",                      # over 254
    ])
    def test_junk_is_dropped_rather_than_rendered(self, bad):
        assert _login_hint(bad) is None

    def test_a_real_address_survives(self):
        assert _login_hint(f"  {PATRON}  ") == PATRON
