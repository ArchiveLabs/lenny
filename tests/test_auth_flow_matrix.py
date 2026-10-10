"""Several different flows share these screens. Changing one must not move
the others.

Send-on-arrival and the "who is asking" block both hang off `ctx`, a signed
value that only `/v1/api/oauth2/authorize` mints. Every flow that does not go
through that endpoint therefore cannot produce one, and must render exactly
what it rendered before. These tests say so per flow rather than leaving it as
an argument about the code.

The OTP screens exist in ONE of the three lending modes. `external` hands off
to the identity provider before any OTP page is reached, and `none` refuses
patron login outright, so "the OTP page" is not a thing those modes have.
"""

import os
from urllib.parse import urlencode

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "flow-matrix-test-seed-32-chars!!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.cache import CacheEntry  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402

PATRON = "patron@example.org"
NAVIGATION = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
WEB_REDIRECT = "https://openlibrary.org/borrow/lenny/callback"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    Base.metadata.create_all(engine)
    db.query(CacheEntry).delete()
    db.query(OAuthClient).delete()
    db.commit()
    monkeypatch.setattr("lenny.core.auth.OTP.issue",
                        classmethod(lambda cls, e, i: None))
    yield
    db.query(CacheEntry).delete()
    db.query(OAuthClient).delete()
    db.commit()
    db.remove()


@pytest.fixture
def lending_on(monkeypatch):
    monkeypatch.setattr("lenny.routes.oauth._require_lending", lambda: None)


def register(**kw):
    kw.setdefault("name", "Open Library")
    kw.setdefault("redirect_uris", [WEB_REDIRECT])
    kw.setdefault("scopes", ["loans:read", "borrow"])
    obj, _ = OAuthClient.register(**kw)
    return obj


def authorize_url(client, redirect=WEB_REDIRECT, **extra):
    params = {
        "client_id": client.client_id, "redirect_uri": redirect,
        "response_type": "code", "scope": "loans:read borrow", "state": "xyz",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    params.update(extra)
    return "/v1/api/oauth2/authorize?" + urlencode(params)


# ── cells 1 and 2: `ol` mode, with and without a hint ────────────────────────

class TestOlModeConsumerFlow:
    def test_with_a_hint_the_patron_lands_on_the_code_page(self, lending_on):
        client = register()
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(authorize_url(client, login_hint=PATRON))
        page = c.get(bounce.headers["location"], headers=NAVIGATION)
        assert page.status_code == 200
        assert 'id="otpForm"' in page.text

    def test_without_a_hint_the_patron_is_still_asked_for_an_email(self, lending_on):
        """A consumer that sends no `login_hint` must get the email step. The
        new path is additive — it cannot be the only way in."""
        client = register()
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(authorize_url(client))
        page = c.get(bounce.headers["location"], headers=NAVIGATION)
        assert page.status_code == 200
        assert 'id="email"' in page.text, "the email step disappeared"
        assert 'id="otpForm"' not in page.text


# ── cell 3: already signed in ────────────────────────────────────────────────

class TestAnExistingSessionSkipsTheOtpEntirely:
    def test_a_live_session_goes_straight_to_consent(self, lending_on):
        client = register()
        c = TestClient(app, follow_redirects=False)
        r = c.get(authorize_url(client),
                  cookies={"session": auth.create_session_cookie(PATRON)})
        assert r.status_code == 200
        assert 'name="request"' in r.text, "not the consent screen"
        assert 'id="otpForm"' not in r.text and 'id="email"' not in r.text

    def test_a_live_session_with_a_hint_still_goes_straight_to_consent(self, lending_on):
        """The hint must not drag a signed-in patron back through sign-in, nor
        mail them a code they have no use for."""
        client = register()
        sent = []
        import lenny.core.auth as a
        a.OTP.issue = classmethod(lambda cls, e, i: sent.append(e))
        c = TestClient(app, follow_redirects=False)
        r = c.get(authorize_url(client, login_hint=PATRON),
                  cookies={"session": auth.create_session_cookie(PATRON)},
                  headers=NAVIGATION)
        assert r.status_code == 200
        assert 'name="request"' in r.text
        assert sent == [], f"mailed a signed-in patron: {sent}"


# ── cell 4: external identity provider ───────────────────────────────────────

class TestExternalModeHasNoOtpStep:
    def test_the_login_route_hands_off_before_any_otp_page(self, monkeypatch):
        """With OIDC configured, `/oauth/authorize` redirects to the provider.
        No OTP page is rendered, so nothing added here is reachable."""
        monkeypatch.setattr("lenny.routes.api._external_auth_ready", lambda: True)
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/"},
                  headers=NAVIGATION)
        assert r.status_code == 302
        assert "/v1/api/oauth/external/start" in r.headers["location"]

    def test_a_hint_and_a_context_cannot_force_an_otp_send(self, monkeypatch):
        """Even carrying everything the new path wants, external mode must
        still hand off rather than mail anything."""
        monkeypatch.setattr("lenny.routes.api._external_auth_ready", lambda: True)
        sent = []
        import lenny.core.auth as a
        a.OTP.issue = classmethod(lambda cls, e, i: sent.append(e))
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/",
                          "login_hint": PATRON, "ctx": "anything"},
                  headers=NAVIGATION)
        assert r.status_code == 302
        assert sent == [], f"external mode sent mail: {sent}"


# ── cell 5: lending disabled ─────────────────────────────────────────────────

class TestNoneModeRefusesBeforeRenderingAnything:
    def test_patron_login_is_refused_not_rendered(self, monkeypatch):
        """`_require_lending` is left real here. Whatever it does, it must
        happen before an OTP form or a send."""
        monkeypatch.setattr("lenny.routes.api._external_auth_ready", lambda: False)
        sent = []
        import lenny.core.auth as a
        a.OTP.issue = classmethod(lambda cls, e, i: sent.append(e))
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/",
                          "login_hint": PATRON},
                  headers=NAVIGATION)
        assert sent == [], f"lending-disabled node sent mail: {sent}"
        assert 'id="otpForm"' not in r.text


# ── cells 6 and 7: other registered consumers ────────────────────────────────

class TestOtherConsumersShareTheScreensUnharmed:
    def test_a_public_pkce_client_gets_the_same_treatment(self, lending_on):
        """Book Server style: public client, no secret, PKCE only. Nothing in
        the new path depends on client confidentiality."""
        client = register(name="Book Server", is_confidential=False,
                          client_id="reader-archive-org")
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(authorize_url(client, login_hint=PATRON))
        page = c.get(bounce.headers["location"], headers=NAVIGATION)
        assert page.status_code == 200
        assert 'id="otpForm"' in page.text
        assert "Book Server" in page.text, "the page names the wrong consumer"

    def test_a_native_app_on_a_private_use_scheme(self, lending_on):
        """A reading app registered on its own URI scheme shares these
        screens. If the scheme is not registrable here the flow does not
        exist, so the test says which it found rather than guessing."""
        from lenny.core.oauth2 import acceptable_redirect
        native = "com.example.reader:/oauth/callback"
        if not acceptable_redirect(native):
            pytest.skip(f"private-use scheme not accepted: {native}")
        client = register(name="Reader App", redirect_uris=[native],
                          is_confidential=False)
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(authorize_url(client, redirect=native, login_hint=PATRON))
        page = c.get(bounce.headers["location"], headers=NAVIGATION)
        assert page.status_code == 200
        assert 'id="otpForm"' in page.text


# ── cell 8: the legacy OPDS implicit flow ────────────────────────────────────

class TestLegacyOpdsImplicitIsUntouched:
    def test_a_native_opds_reader_sees_the_plain_sign_in(self, lending_on):
        """No `ctx` can exist here — this flow never visits `/oauth2/authorize`
        — so the page must be the one it has always been."""
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/authorize",
                  params={"redirect_uri": "opds://authorize/"},
                  headers=NAVIGATION)
        assert r.status_code == 200
        assert 'id="email"' in r.text
        assert "wants to borrow books for you" not in r.text

    def test_the_auth_document_still_advertises_the_flow(self):
        c = TestClient(app, follow_redirects=False)
        r = c.get("/v1/api/oauth/implicit")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/opds-authentication+json")
