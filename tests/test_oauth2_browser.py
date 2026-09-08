"""End-to-end browser tests for the OAuth 2.0 consent journey.

What these add over the integration tests: a real browser, real cookies, real
redirects. The consent screen is the one part of this flow a patron actually
sees, and the one part TestClient cannot honestly exercise — it does not follow
a 303 across origins, run the form, or carry a `Set-Cookie` the way Chrome does.

Run against a live server (see docs/oauth2-testing.md):

    pytest tests/test_oauth2_browser.py --lenny http://127.0.0.1:8097

Skipped unless --lenny is supplied, so the normal suite stays hermetic.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

CALLBACK = "http://127.0.0.1:8092/callback"


@pytest.fixture(scope="module")
def base_url(pytestconfig):
    url = pytestconfig.getoption("--lenny", default=None)
    if not url:
        pytest.skip("needs --lenny pointing at a running node")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(scope="module", autouse=True)
def callback_server():
    """A stand-in for the consumer's redirect endpoint.

    Without something listening, the browser lands on `chrome-error://` and the
    redirect URL — the thing under test — is lost. A real consumer has this
    endpoint, so the test should too.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>callback received</body></html>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8092), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def page(browser, base_url, patron_session):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": "session", "value": patron_session, "url": base_url}])
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture(scope="module")
def patron_session(base_url, registered):
    """A logged-in patron's cookie, minted directly.

    Going through the OTP screen would mean sending a real email; the point of
    these tests is the consent journey, not OTP delivery, which has its own
    coverage.

    The cookie is signed with `LENNY_SEED`, so it only verifies if this process
    and the server share one. `pytest.ini` sets `LENNY_SEED` via pytest-env, and
    that value wins inside pytest regardless of the shell — so a server started
    with a different seed rejects every cookie minted here and the flow falls
    through to the login screen. That surfaces as an unrelated-looking
    `lending_not_configured`, so check it here and say so plainly.
    """
    import os

    import httpx
    from lenny.core import auth

    cookie = auth.create_session_cookie("patron@example.org")
    probe = httpx.get(
        authorize_url(base_url, registered["client_id"], "x" * 43, "probe"),
        cookies={"session": cookie}, follow_redirects=False, timeout=30)
    if probe.status_code != 200:
        pytest.fail(
            "The server did not accept a session cookie minted by this test.\n"
            f"  LENNY_SEED here: {os.environ.get('LENNY_SEED')!r}\n"
            "  Start the server under test with that same seed, e.g.\n"
            f"    LENNY_SEED={os.environ.get('LENNY_SEED')!r} "
            "uvicorn lenny.app:app --port 8097\n"
            f"  (authorize returned HTTP {probe.status_code})")
    return cookie


@pytest.fixture(scope="module")
def registered():
    """A client the way an operator creates one — `lenny oauth2-register`.

    There is no registration endpoint to call: who may connect is the node
    operator's decision, so the test makes it the same way the CLI does.
    """
    from lenny.core.oauth2 import OAuthClient
    client, secret = OAuthClient.register(
        name="Open Library", redirect_uris=[CALLBACK],
        scopes=["loans:read", "borrow"])
    return {"client_id": client.client_id, "client_secret": secret}


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(base_url, client_id, challenge, state, scope="loans:read borrow"):
    from urllib.parse import urlencode
    return f"{base_url}/v1/api/oauth2/authorize?" + urlencode({
        "client_id": client_id, "redirect_uri": CALLBACK,
        "response_type": "code", "scope": scope, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


class TestConsentJourney:
    def test_patron_sees_what_they_are_approving(self, page, base_url, registered):
        """The consent screen must name the client and describe the scopes in
        words. A patron cannot meaningfully consent to `loans:read`."""
        _, challenge = pkce()
        page.goto(authorize_url(base_url, registered["client_id"], challenge, "s1"),
                  wait_until="domcontentloaded")
        body = page.inner_text("body")
        assert "Open Library" in body
        assert "on loan" in body, "scopes must be shown in plain language"
        assert page.query_selector("button[value='allow']")
        assert page.query_selector("button[value='deny']")
        # The patron must be able to see where they are about to be sent — the
        # one claim a lookalike client cannot fake.
        assert "127.0.0.1:8092" in body

    def test_allow_redirects_back_with_a_code(self, page, base_url, registered):
        _, challenge = pkce()
        state = secrets.token_urlsafe(8)
        page.goto(authorize_url(base_url, registered["client_id"], challenge, state),
                  wait_until="domcontentloaded")

        page.click("button[value='allow']")
        page.wait_for_url(f"{CALLBACK}*", timeout=10000)

        q = parse_qs(urlparse(page.url).query)
        assert q["code"][0]
        assert q["state"][0] == state, "state must round-trip for CSRF binding"

    def test_deny_returns_access_denied_and_no_code(self, page, base_url, registered):
        _, challenge = pkce()
        page.goto(authorize_url(base_url, registered["client_id"], challenge, "s3"),
                  wait_until="domcontentloaded")
        page.click("button[value='deny']")
        page.wait_for_url(f"{CALLBACK}*", timeout=10000)
        q = parse_qs(urlparse(page.url).query)
        assert q.get("error", [""])[0] == "access_denied"
        assert "code" not in q, "a denial must not hand out a code"


class TestAnonymousPatron:
    def test_is_sent_to_login_first(self, browser, base_url, registered):
        """No session: the patron must be authenticated before being asked to
        consent, or the consent means nothing."""
        ctx = browser.new_context()
        pg = ctx.new_page()
        _, challenge = pkce()
        pg.goto(authorize_url(base_url, registered["client_id"], challenge, "s4"),
                wait_until="domcontentloaded")
        assert "/oauth/authorize" in pg.url, f"expected the login screen, got {pg.url}"
        ctx.close()


class TestOpenRedirect:
    def test_unregistered_redirect_is_rendered_not_followed(
            self, page, base_url, registered):
        """The browser must never be sent to an unvalidated redirect_uri —
        otherwise /authorize is an open redirector wearing an OAuth costume."""
        from urllib.parse import urlencode
        _, challenge = pkce()
        evil = "https://evil.example.com/steal"
        url = f"{base_url}/v1/api/oauth2/authorize?" + urlencode({
            "client_id": registered["client_id"], "redirect_uri": evil,
            "response_type": "code", "state": "s5",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        page.goto(url, wait_until="domcontentloaded")
        assert urlparse(page.url).hostname != "evil.example.com", (
            "the browser was sent to the unregistered redirect_uri")
        assert "invalid_request" in page.inner_text("body")
