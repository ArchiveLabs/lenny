"""The auth screens are read by patrons, not by implementers.

Two separate complaints, both from walking the live borrow flow:

  * The sign-in pages led with a badge reading "OAuth Mode" — the name of a
    protocol, shown to someone who wants a book.

  * The order was backwards. The patron was asked for an email, then for a
    one-time code, and only after spending both steps did a consent screen
    appear explaining that something wanted to borrow on their behalf. Effort
    and a credential were spent before the reason was given.

These tests pin the fix at the level the complaint was made: what a patron
actually sees on the page, with the markup stripped out.
"""

import os
import re
from urllib.parse import urlencode

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "plain-speech-test-seed-32-chars!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.cache import CacheEntry  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402

REDIRECT = "https://openlibrary.org/borrow/lenny/callback"
PATRON = "patron@example.org"
NAVIGATION = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}

# Words that describe the machinery rather than the patron's situation. Each
# has been seen rendered on one of these pages.
JARGON = ["oauth", "auth mode", "direct auth", "bearer", "access_token",
          "client_id", "scope", "pkce", "one-time password", "otp"]


def visible(html):
    """What a patron reads: tags, scripts, styles and comments removed."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


@pytest.fixture(autouse=True)
def lending_on(monkeypatch):
    monkeypatch.setattr("lenny.routes.oauth._require_lending", lambda: None)


@pytest.fixture(autouse=True)
def clean_cache():
    Base.metadata.create_all(engine)
    db.query(CacheEntry).delete()
    db.commit()
    yield
    db.query(CacheEntry).delete()
    db.commit()
    db.remove()


@pytest.fixture(autouse=True)
def no_mail(monkeypatch):
    monkeypatch.setattr("lenny.core.auth.OTP.issue",
                        classmethod(lambda cls, email, ip: None))


@pytest.fixture
def client():
    Base.metadata.create_all(engine)
    obj, _ = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    yield obj
    db.query(OAuthClient).delete()
    db.commit()


def authorize_url(client, **extra):
    params = {
        "client_id": client.client_id, "redirect_uri": REDIRECT,
        "response_type": "code", "scope": "loans:read borrow", "state": "xyz",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    params.update(extra)
    return "/v1/api/oauth2/authorize?" + urlencode(params)


def login_page(client, **extra):
    """The sign-in screen as a patron coming from Open Library sees it."""
    c = TestClient(app, follow_redirects=False)
    bounce = c.get(authorize_url(client, **extra))
    return c.get(bounce.headers["location"], headers=NAVIGATION)


def consent_page(client):
    c = TestClient(app, follow_redirects=False)
    return c.get(authorize_url(client),
                 cookies={"session": auth.create_session_cookie(PATRON)})


class TestNoImplementationDetailOnScreen:
    def test_the_email_step_names_no_protocol(self, client):
        text = visible(login_page(client).text).lower()
        found = [w for w in JARGON if w in text]
        assert not found, f"the sign-in page still says {found}"

    def test_the_code_step_names_no_protocol(self, client):
        text = visible(login_page(client, login_hint=PATRON).text).lower()
        found = [w for w in JARGON if w in text]
        assert not found, f"the code page still says {found}"

    def test_the_consent_step_names_no_protocol(self, client):
        """The consent screen prints each scope's technical name as a tag
        beside its description, which is deliberate and stays — so this checks
        the prose around it rather than the whole page."""
        html = consent_page(client).text
        html = re.sub(r"(?s)<code.*?</code>", " ", html)
        text = visible(html).lower()
        found = [w for w in JARGON if w in text]
        assert not found, f"the consent page still says {found}"

    def test_the_dead_auth_mode_badge_is_gone_from_the_templates(self):
        """`auth_mode` was never once placed in these templates' context, so
        the "Direct Auth" branch could not be reached and every patron got the
        "OAuth Mode" one. A branch that cannot be taken is not a feature."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "lenny" / "templates"
        offenders = [p.name for p in root.glob("*.html")
                     if "{% if auth_mode" in p.read_text()]
        assert not offenders, f"dead auth_mode branch still in {offenders}"


class TestTheReasonComesBeforeTheCredential:
    def test_the_email_step_says_who_is_asking_and_what_for(self, client):
        text = visible(login_page(client).text)
        assert "Open Library" in text, "the page never names the consumer"
        assert "Borrow and return books on your behalf" in text
        assert "See which books you have on loan" in text

    def test_the_code_step_says_who_is_asking_and_what_for(self, client):
        """With a hint this is the patron's FIRST screen, so it is the one
        that most needs the reason on it."""
        text = visible(login_page(client, login_hint=PATRON).text)
        assert "Open Library" in text
        assert "Borrow and return books on your behalf" in text

    def test_the_reason_is_rendered_above_the_credential_field(self, client):
        """Order on the page, not merely presence: the explanation has to come
        first or it is a footnote."""
        html = login_page(client).text
        assert html.index("Open Library") < html.index('id="email"'), \
            "the credential field comes before the explanation"

    def test_the_code_page_puts_the_reason_above_the_code_field(self, client):
        html = login_page(client, login_hint=PATRON).text
        assert html.index("Open Library") < html.index('id="otp"'), \
            "the code field comes before the explanation"

    def test_the_patron_is_told_a_choice_is_still_coming(self, client):
        """The surprise was meeting a consent screen after believing the job
        was done. Saying so up front is the whole fix."""
        for page in (login_page(client),
                     login_page(client, login_hint=PATRON)):
            text = visible(page.text)
            assert "choose whether to allow" in text, \
                "the page does not warn that consent is still to come"

    def test_signing_in_is_not_described_as_granting_anything(self, client):
        text = visible(login_page(client).text)
        assert "doesn't grant anything on its own" in text


class TestOtherFlowsKeepThePlainPage:
    """`client_name` rides in the signed context, so only the consumer flow
    has one. Everything else must look exactly as it did."""

    def test_a_bare_opds_signin_shows_no_consumer_block(self, client):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/"},
                  headers=NAVIGATION)
        assert r.status_code == 200
        text = visible(r.text)
        assert "Open Library" not in text
        assert "wants to borrow books for you" not in text
        assert "Sign in to Lenny" in text

    def test_a_forged_client_name_cannot_be_injected_by_url(self, client):
        """The name is read out of the signed context, never the query, or a
        link could make this library vouch for anyone."""
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/",
                          "client_name": "Totally Legitimate Bank",
                          "client_id": "totally-legitimate-bank"},
                  headers=NAVIGATION)
        assert "Totally Legitimate Bank" not in r.text
