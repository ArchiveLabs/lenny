"""`login_hint` pre-fills the email box; it must never send mail by itself.

RFC 6749 §3.1.2.1. Open Library knows who the patron is, so it passes the
address along and the patron is spared typing one they just proved they own.

The security property is the one worth pinning: `/oauth2/authorize` and the
login page behind it are unauthenticated GETs, so acting on the hint by
sending a code would turn a link — or an `<img src>`, or a browser prefetch —
into an outbound mailer aimed at an address the caller chose. The hint may
only ever pre-fill a field.
"""

import os

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "login-hint-test-seed-32-chars-ok")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402
from lenny.routes.oauth import _login_hint  # noqa: E402

REDIRECT = "https://openlibrary.org/borrow/lenny/callback"
PATRON = "patron@example.org"


@pytest.fixture(autouse=True)
def lending_on(monkeypatch):
    """The OTP page 503s unless lending is configured (`_require_lending`).

    These tests are about what the page renders, not about the lending gate,
    so stub it — but only the gate. `OTP.issue` is left real so the no-mail
    test below is proving something.
    """
    monkeypatch.setattr("lenny.routes.oauth._require_lending", lambda: None)


@pytest.fixture
def client():
    Base.metadata.create_all(engine)
    obj, _ = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    return obj


def authorize_url(client, **extra):
    from urllib.parse import urlencode
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


class TestItSurvivesTheRoundTrip:
    def test_authorize_forwards_the_hint_to_login(self, client):
        """An unauthenticated patron is bounced to the OTP page; the hint has
        to survive that bounce or it is useless."""
        c = TestClient(app, follow_redirects=False)
        r = c.get(authorize_url(client, login_hint=PATRON))
        assert r.status_code in (302, 303)
        location = r.headers["location"]
        assert "/v1/api/oauth/authorize" in location
        assert "login_hint" in location and "patron%40example.org" in location

    def test_the_login_page_prefills_the_address(self, client):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize",
                          "login_hint": PATRON})
        assert r.status_code == 200
        assert f'value="{PATRON}"' in r.text
        assert PATRON in r.text

    def test_no_hint_leaves_the_field_empty(self, client):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "/v1/api/oauth2/authorize"})
        assert r.status_code == 200
        assert 'value=""' in r.text


class TestItCannotSendMail:
    def test_attack_a_get_with_a_hint_sends_nothing(self, client, monkeypatch):
        """The property this whole design turns on.

        If arriving with a hint sent the code, a link in an email or an
        `<img src>` on any page would mail a one-time code to whatever address
        the attacker put in the URL, with no interaction from anyone.
        """
        sent = []
        monkeypatch.setattr("lenny.core.auth.OTP.issue",
                            classmethod(lambda cls, email, ip: sent.append(email)))
        c = TestClient(app, follow_redirects=True)
        c.get(authorize_url(client, login_hint="victim@example.org"))
        c.get("/v1/api/oauth/authorize",
              params={"redirect_uri": "/v1/api/oauth2/authorize",
                      "login_hint": "victim@example.org"})
        assert sent == [], f"a GET carrying a hint sent mail to {sent}"


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
