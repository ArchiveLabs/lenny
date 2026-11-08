"""GHSA-rf64-p839-cgwq — the consent-screen CORS guard must key on the resolved
route, not the observed URL path, and must DENY by removing the CORS headers
rather than setting `Access-Control-Allow-Origin: null`.

Two defects are covered:

1. **Fails open on a different mount.** The old guard compared
   `request.url.path == "/v1/api/oauth2/authorize"`. A mount prefix, an ASGI
   root_path, or a trailing-slash redirect changes the observed path, the
   compare misses, and the permissive app-wide CORS headers go out unguarded
   with no signal. `test_guard_keys_on_resolved_route_not_observed_path` drives
   the *real* oauth2 router mounted under a *different* prefix and shows the old
   key misses while the route the router matched still identifies the endpoint.

2. **`ACAO: null` is a grant, not a denial.** `null` is a real origin that
   sandboxed iframes and `data:` documents send, so setting it grants exactly
   that attacker-reachable origin. A denial is the *absence* of the header.

Every assertion here has been watched fail against the pre-fix source — see the
PR body's red-first record.
"""

import base64
import hashlib
import os
import secrets

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "cors-guard-test-seed-32-chars-aa")

from fastapi import FastAPI  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import _serves_consent_screen  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402
from lenny.routes import oauth2 as oauth2_routes  # noqa: E402

REDIRECT = "https://consumer.example.org/callback"
PATRON_EMAIL = "patron@example.org"
AUTHORIZE_URL = "/v1/api/oauth2/authorize"
EVIL = "https://evil.example"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    import sqlalchemy
    for table in ("oauth_access_tokens", "oauth_authorization_codes",
                  "oauth_clients", "loans"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


@pytest.fixture
def app_client():
    from lenny.app import app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def session_cookie():
    return auth.create_session_cookie(PATRON_EMAIL)


@pytest.fixture
def client():
    obj, secret = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    return obj, secret


def _pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authorize_params(client_obj, challenge):
    return {
        "client_id": client_obj.client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "loans:read borrow",
        "state": "state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


# ── Defect 2: deny is the ABSENCE of the headers, never "null" ────────────────

class TestConsentIsNotGrantedToAnyOrigin:

    def test_consent_response_carries_no_cross_origin_grant(
            self, app_client, client, session_cookie):
        """A credentialed cross-origin fetch of the consent screen must get no
        CORS grant at all. Both headers CORSMiddleware reflects must be gone."""
        obj, _ = client
        _, challenge = _pkce()
        r = app_client.get(AUTHORIZE_URL, params=_authorize_params(obj, challenge),
                           cookies={"session": session_cookie},
                           headers={"Origin": EVIL}, follow_redirects=False)
        assert r.status_code == 200, r.text[:200]
        headers = {k.lower() for k in r.headers}
        assert "access-control-allow-origin" not in headers, (
            "consent response still advertises a cross-origin grant: "
            f"{r.headers.get('access-control-allow-origin')!r}")
        assert "access-control-allow-credentials" not in headers

    def test_null_is_not_used_as_a_denial(
            self, app_client, client, session_cookie):
        """`ACAO: null` is a grant to the null origin (sandboxed iframes,
        `data:` docs), not a denial. It must never be the chosen value."""
        obj, _ = client
        _, challenge = _pkce()
        r = app_client.get(AUTHORIZE_URL, params=_authorize_params(obj, challenge),
                           cookies={"session": session_cookie},
                           headers={"Origin": EVIL}, follow_redirects=False)
        assert r.headers.get("access-control-allow-origin") != "null"

    def test_the_post_decision_response_is_also_guarded(
            self, app_client, client, session_cookie):
        """The POST that mints the authorization code puts it in the redirect's
        Location — the response that most needs to be unreadable cross-origin."""
        import re
        obj, _ = client
        _, challenge = _pkce()
        page = app_client.get(AUTHORIZE_URL, params=_authorize_params(obj, challenge),
                              cookies={"session": session_cookie}, follow_redirects=False)
        handle = re.search(r'name="request" value="([^"]+)"', page.text)
        assert handle, "consent form missing its request handle"
        r = app_client.post(AUTHORIZE_URL,
                            data={"request": handle.group(1), "decision": "allow"},
                            cookies={"session": session_cookie},
                            headers={"Origin": EVIL}, follow_redirects=False)
        assert r.status_code == 303, r.text[:200]
        headers = {k.lower() for k in r.headers}
        assert "access-control-allow-origin" not in headers
        assert "access-control-allow-credentials" not in headers


# ── Defect 1: key on the resolved route, not the observed path ────────────────

class TestGuardKeysOnResolvedRoute:

    def test_guard_keys_on_resolved_route_not_observed_path(
            self, client, session_cookie):
        """Mount the REAL oauth2 router under a DIFFERENT prefix than the literal
        the old guard hardcoded. The consent screen still serves, but the
        observed path no longer equals "/v1/api/oauth2/authorize" — so the old
        string compare fails open. `_serves_consent_screen`, keyed on the route
        the router matched, still recognises it.
        """
        obj, _ = client
        _, challenge = _pkce()

        deployed = FastAPI()
        deployed.templates = Jinja2Templates(directory="lenny/templates")
        deployed.include_router(oauth2_routes.router, prefix="/deployed/elsewhere")

        captured = {}

        @deployed.middleware("http")
        async def _capture(request, call_next):
            response = await call_next(request)
            captured["observed_path"] = request.url.path
            captured["old_literal_match"] = (
                request.url.path == "/v1/api/oauth2/authorize")
            captured["new_route_match"] = _serves_consent_screen(request.scope)
            return response

        tc = TestClient(deployed, raise_server_exceptions=True)
        r = tc.get("/deployed/elsewhere/oauth2/authorize",
                   params=_authorize_params(obj, challenge),
                   cookies={"session": session_cookie}, follow_redirects=False)

        assert r.status_code == 200, r.text[:200]
        # The consent screen is served under a non-default mount...
        assert captured["observed_path"] == "/deployed/elsewhere/oauth2/authorize"
        # ...so the OLD key would have missed and failed open:
        assert captured["old_literal_match"] is False
        # ...but the NEW key, on the resolved route, still catches it:
        assert captured["new_route_match"] is True

    def test_trailing_slash_does_not_expose_a_consent_body(self, app_client):
        """A trailing slash is a 307 redirect, not the consent page: the router
        resolves no route (so the guard correctly does not fire) and the body
        carries no consent handle. This documents the real behaviour — the slash
        case does not serve a page the guard would need to protect.

        Note (red-first record): this assertion also passes against the pre-fix
        source, which likewise 307s, so it proves documentation, not the guard.
        """
        r = app_client.get(AUTHORIZE_URL + "/", follow_redirects=False)
        assert r.status_code in (307, 308), r.status_code
        assert 'name="request"' not in r.text


# NO COMPLETENESS TEST HERE — ON PURPOSE.
#
# An automated completeness guard was written here (a static transitive
# template-usage analysis asserting every consent-rendering oauth2 route was in
# `_CONSENT_SCREEN_ENDPOINTS`) and REMOVED after adversarial review. It failed
# closed in the wrong direction: a future route returning the authorization code
# via a non-template response (HTMLResponse/RedirectResponse) renders no `.html`
# literal, so the analysis saw nothing and the test passed GREEN while the route
# would ship unguarded — a control reporting green over the exact blind spot it
# existed to catch, which is worse than none because it stops the next person
# looking. The guarded set is therefore a documented MANUAL allowlist (see the
# comment on `_CONSENT_SCREEN_ENDPOINTS` in lenny/app.py). The durable, enforced
# fix is to default-deny CORS for the whole router and allowlist the safe
# exceptions; that is an app-wide change tracked as a separate follow-up to
# GHSA-rf64-p839-cgwq, not landed in this PR.
