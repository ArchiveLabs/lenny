"""Integration tests for the OAuth 2.0 endpoints.

These drive the real FastAPI app over HTTP — real routing, real form parsing,
real responses. What they add over the unit tests is the wire contract: status
codes, `WWW-Authenticate` headers, and the redirect-versus-render distinction
that keeps `/authorize` from becoming an open redirector.
"""

import base64
import hashlib
import os
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "oauth2-endpoint-test-seed-32-char")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.core import auth  # noqa: E402
from lenny.configs import DB_URI  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import OAuthClient  # noqa: E402
from lenny.core.oauth2 import acceptable_redirect  # noqa: E402

REDIRECT = "https://consumer.example.org/callback"
PATRON_EMAIL = "patron@example.org"
TOKEN_URL = "/v1/api/oauth2/token"
AUTHORIZE_URL = "/v1/api/oauth2/authorize"


@pytest.fixture(autouse=True)
def fresh_db():
    """Clean slate per test.

    `loans` matters as much as the oauth tables: lending policy counts a
    patron's active loans and an item's outstanding copies, so a loan left
    behind by an earlier test silently changes what the next one is allowed to
    do. Leaving it out made the borrow-policy tests pass alone and fail in a
    group — order-dependence, not a real defect, but indistinguishable from one
    at 2am.
    """
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
    # Only the editions these tests invent; a real catalogue is left alone.
    try:
        db.execute(sqlalchemy.text(
            "DELETE FROM items WHERE openlibrary_edition >= 70000000"))
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
    """A logged-in patron.

    `create_session_cookie` binds the IP when given one; TestClient reports
    `testclient`, and passing None here leaves the cookie unbound so the check
    is a no-op. The IP binding itself is covered in the auth tests.
    """
    return auth.create_session_cookie(PATRON_EMAIL)


@pytest.fixture
def client():
    obj, secret = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    return obj, secret


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def basic(client_id, secret):
    return {"Authorization": "Basic " + base64.b64encode(
        f"{client_id}:{secret}".encode()).decode()}


def authorize_params(client_obj, challenge, **overrides):
    params = {
        "client_id": client_obj.client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "loans:read borrow",
        "state": "state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


def request_handle(app_client, client_obj, challenge, session, **overrides):
    """Render the consent screen and pull out its opaque request handle.

    The handle is the only thing the form carries, so a test that wants to
    approve something has to go through the screen the patron sees.
    """
    r = app_client.get(AUTHORIZE_URL,
                       params=authorize_params(client_obj, challenge, **overrides),
                       cookies={"session": session}, follow_redirects=False)
    assert r.status_code == 200, f"consent screen not rendered: {r.status_code} {r.text[:200]}"
    m = re.search(r'name="request" value="([^"]+)"', r.text)
    assert m, "consent form is missing its request handle"
    return m.group(1)


def consent(app_client, client_obj, challenge, session, decision="allow", **overrides):
    """Walk the consent screen and submit a decision."""
    handle = request_handle(app_client, client_obj, challenge, session, **overrides)
    return app_client.post(AUTHORIZE_URL,
                           data={"request": handle, "decision": decision},
                           cookies={"session": session}, follow_redirects=False)


def get_code(app_client, client_obj, challenge, session):
    r = consent(app_client, client_obj, challenge, session)
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


# ─────────────────────────────────────────────────────────────────────────────
# Metadata (RFC 8414)
# ─────────────────────────────────────────────────────────────────────────────

class TestMetadata:
    def test_served_at_the_origin_root(self, app_client):
        """A consumer given only a base URL must be able to discover the rest.
        That is what makes N independent nodes workable."""
        r = app_client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        meta = r.json()
        for key in ("issuer", "authorization_endpoint", "token_endpoint",
                    "revocation_endpoint"):
            assert meta[key], f"{key} missing from metadata"

    def test_attack_issuer_does_not_follow_the_host_header(self, app_client):
        """RFC 8414 §3.3 makes the issuer security-relevant: a consumer compares
        it against the URL it fetched and refuses on mismatch. Deriving it from
        the Host header lets anyone who can set that header — or seed a
        path-keyed cache — advertise an attacker-controlled token endpoint,
        where a lax client would POST its client_secret and code.

        The deployment's own configuration is the only trustworthy source, and
        Lenny already has one: `LennyAPI.make_url` builds every absolute URL in
        the OPDS feed and the auth document.
        """
        from lenny.core.api import LennyAPI
        configured = LennyAPI.make_url("").rstrip("/")
        r = app_client.get("/.well-known/oauth-authorization-server",
                           headers={"host": "evil.example.com"})
        meta = r.json()
        for key in ("issuer", "authorization_endpoint", "token_endpoint",
                    "revocation_endpoint"):
            assert "evil.example.com" not in meta[key], (
                f"{key} followed the Host header: {meta[key]!r}")
            assert meta[key].startswith(configured), (
                f"{key} is {meta[key]!r}, not derived from the configured "
                f"public URL {configured!r}")

    def test_iss_matches_the_advertised_issuer(self, app_client, client, session_cookie):
        """RFC 9207: the `iss` a client receives has to be the same identifier
        the metadata advertises, or the mix-up defence compares two different
        things and always fails."""
        meta = app_client.get("/.well-known/oauth-authorization-server").json()
        obj, _ = client
        _, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["iss"][0] == meta["issuer"]

    def test_misconfiguration_is_logged_not_silent(self, app_client, caplog):
        """A node whose configured public URL does not match how it was actually
        reached will publish endpoints nobody can use. That has to be loud —
        the whole reason the forwarded-IP default survived so long is that both
        of its failure modes were silent."""
        with caplog.at_level("WARNING", logger="lenny.routes.oauth2"):
            app_client.get("/.well-known/oauth-authorization-server",
                           headers={"host": "not-how-this-node-is-configured.example"})
        assert "LENNY_PROXY" in caplog.text or "LENNY_HOST" in caplog.text

    def test_advertises_only_what_is_implemented(self, app_client):
        meta = app_client.get("/.well-known/oauth-authorization-server").json()
        assert meta["response_types_supported"] == ["code"]
        assert set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"}
        assert meta["code_challenge_methods_supported"] == ["S256"]
        # The implicit grant is removed by OAuth 2.1; advertising it would invite
        # a client to use a flow this server deliberately does not support.
        assert "token" not in meta["response_types_supported"]
        assert "plain" not in meta["code_challenge_methods_supported"]


# ─────────────────────────────────────────────────────────────────────────────
# Authorization endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorize:
    def test_unknown_client_is_shown_not_redirected(self, app_client):
        r = app_client.get(AUTHORIZE_URL, params={
            "client_id": "nope", "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge": "x", "code_challenge_method": "S256"},
            follow_redirects=False)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client"

    def test_attack_unregistered_redirect_is_not_redirected_to(self, app_client, client):
        """RFC 6749 §4.1.2.1. Redirecting an error to an unvalidated URI would
        make this endpoint an open redirector — the error must be rendered."""
        obj, _ = client
        evil = "https://evil.example.com/steal"
        r = app_client.get(AUTHORIZE_URL, params={
            "client_id": obj.client_id, "redirect_uri": evil,
            "response_type": "code", "code_challenge": "x",
            "code_challenge_method": "S256"}, follow_redirects=False)
        assert r.status_code == 400
        assert "location" not in {k.lower() for k in r.headers}
        assert evil not in r.text

    def test_anonymous_patron_is_sent_to_login(self, app_client, client):
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           follow_redirects=False)
        assert r.status_code == 303
        assert "/v1/api/oauth/authorize" in r.headers["location"]

    def test_logged_in_patron_sees_consent_with_scopes(self, app_client, client, session_cookie):
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        assert r.status_code == 200
        assert "Open Library" in r.text
        # Scopes are described in words, not just symbols.
        assert "on loan" in r.text

    @pytest.mark.parametrize("override,expected", [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"scope": "admin:all"}, "invalid_scope"),
    ])
    def test_bad_requests_redirect_the_error_once_the_uri_is_trusted(
            self, app_client, client, override, expected):
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL,
                           params=authorize_params(obj, challenge, **override),
                           follow_redirects=False)
        assert r.status_code == 303
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"][0] == expected
        assert q["state"][0] == "state-123", "state must survive an error"

    def test_deny_redirects_with_access_denied(self, app_client, client, session_cookie):
        obj, _ = client
        _, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie, decision="deny")
        assert r.status_code == 303
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"][0] == "access_denied"

    def test_allow_returns_a_code_and_the_state(self, app_client, client, session_cookie):
        obj, _ = client
        _, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie)
        assert r.status_code == 303
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["code"][0]
        assert q["state"][0] == "state-123"

    def test_attack_consent_without_a_session_refused(
            self, app_client, client, session_cookie):
        """A handle alone must not be enough — the POST verifies the patron
        independently, so a stolen handle is useless without their session."""
        obj, _ = client
        _, challenge = pkce()
        handle = request_handle(app_client, obj, challenge, session_cookie)
        r = app_client.post(AUTHORIZE_URL,
                            data={"request": handle, "decision": "allow"},
                            follow_redirects=False)
        assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Token endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestToken:
    def test_full_exchange(self, app_client, client, session_cookie):
        obj, secret = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"] and body["refresh_token"]
        assert body["expires_in"] > 0
        # RFC 6749 §5.1 — tokens must not be cached by intermediaries.
        assert r.headers["cache-control"] == "no-store"

    def test_client_credentials_may_be_form_fields(self, app_client, client, session_cookie):
        obj, secret = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier,
            "client_id": obj.client_id, "client_secret": secret})
        assert r.status_code == 200

    def test_attack_wrong_client_secret_refused(self, app_client, client, session_cookie):
        obj, _ = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, "wrong"), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier})
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"

    def test_attack_missing_verifier_refused(self, app_client, client, session_cookie):
        """Without this, an intercepted code alone would be enough — which is
        the entire attack PKCE exists to stop."""
        obj, secret = client
        _, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_attack_wrong_verifier_refused(self, app_client, client, session_cookie):
        obj, secret = client
        _, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": secrets.token_urlsafe(64)})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_attack_code_replay_refused_and_revokes_the_token(
            self, app_client, client, session_cookie):
        obj, secret = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        first = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier}).json()

        second = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier})
        assert second.status_code == 400

        # RFC 6749 §4.1.2: reuse implies leakage, so the issued token dies too.
        probe = app_client.get("/v1/api/oauth2/loans", headers={
            "Authorization": f"Bearer {first['access_token']}"})
        assert probe.status_code == 401

    def test_attack_code_redeemed_by_another_client_refused(
            self, app_client, client, session_cookie):
        obj, _ = client
        other, other_secret = OAuthClient.register(
            name="Other", redirect_uris=[REDIRECT], scopes=["loans:read"])
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        r = app_client.post(TOKEN_URL, headers=basic(other.client_id, other_secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_unsupported_grant_refused(self, app_client, client):
        obj, secret = client
        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret),
                            data={"grant_type": "password",
                                  "username": "x", "password": "y"})
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    def test_refresh_rotates_and_kills_the_old_token(
            self, app_client, client, session_cookie):
        obj, secret = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        first = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier}).json()

        r = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
        assert r.status_code == 200
        assert r.json()["refresh_token"] != first["refresh_token"]

        again = app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
        assert again.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Protected resources
# ─────────────────────────────────────────────────────────────────────────────

class TestProtectedResources:
    def _token(self, app_client, client, session_cookie, scope="loans:read borrow"):
        obj, secret = client
        verifier, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie, scope=scope)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        return app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier}).json()["access_token"]

    def test_loans_requires_a_bearer_token(self, app_client):
        r = app_client.get("/v1/api/oauth2/loans")
        assert r.status_code == 401
        # RFC 6750 §3 — the challenge tells a client what to do next.
        assert "www-authenticate" in {k.lower() for k in r.headers}

    def test_attack_garbage_token_refused(self, app_client):
        r = app_client.get("/v1/api/oauth2/loans",
                           headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_token"

    def test_loans_returns_a_list(self, app_client, client, session_cookie):
        token = self._token(app_client, client, session_cookie)
        r = app_client.get("/v1/api/oauth2/loans",
                           headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert isinstance(r.json()["loans"], list)

    def test_attack_insufficient_scope_refused(self, app_client, client, session_cookie):
        """A token granted only loans:read must not be able to borrow."""
        token = self._token(app_client, client, session_cookie, scope="loans:read")
        r = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"edition_id": 12345})
        assert r.status_code == 403
        assert r.json()["error"] == "insufficient_scope"

    def test_attack_revoked_token_refused(self, app_client, client, session_cookie):
        token = self._token(app_client, client, session_cookie)
        obj, secret = client
        app_client.post("/v1/api/oauth2/revoke", headers=basic(obj.client_id, secret),
                        data={"token": token})
        r = app_client.get("/v1/api/oauth2/loans",
                           headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_revocation_does_not_disclose_whether_a_token_existed(
            self, app_client, client):
        """RFC 7009 §2.2 — always 200, so revocation cannot be used as an oracle."""
        obj, secret = client
        r = app_client.post("/v1/api/oauth2/revoke", headers=basic(obj.client_id, secret),
                            data={"token": "never-issued"})
        assert r.status_code == 200

    def test_attack_revocation_requires_client_authentication(self, app_client):
        """RFC 7009 §2.1. Anonymous revocation is a free DoS on any token whose
        value leaks into a log or a downstream service."""
        r = app_client.post("/v1/api/oauth2/revoke", data={"token": "anything"})
        assert r.status_code == 401

    def test_attack_a_client_cannot_revoke_another_clients_token(
            self, app_client, client, session_cookie):
        """RFC 7009 §5: revocation is scoped to the tokens you were issued."""
        token = self._token(app_client, client, session_cookie)
        attacker, attacker_secret = OAuthClient.register(
            name="Throwaway", redirect_uris=[REDIRECT], scopes=["loans:read"])
        r = app_client.post("/v1/api/oauth2/revoke",
                            headers=basic(attacker.client_id, attacker_secret),
                            data={"token": token})
        assert r.status_code == 200, "must not disclose that the token exists"
        probe = app_client.get("/v1/api/oauth2/loans",
                               headers={"Authorization": f"Bearer {token}"})
        assert probe.status_code == 200, (
            "an unrelated client revoked a token it was never issued")


    def test_borrow_on_a_missing_edition_is_404(self, app_client, client, session_cookie):
        token = self._token(app_client, client, session_cookie)
        r = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"edition_id": 999999999})
        assert r.status_code == 404

    def test_tokens_are_scoped_to_one_patron(self, app_client, client):
        """The endpoint takes no patron parameter, so a leaked token is worth
        one person's loans rather than everyone's. This pins that shape."""
        import inspect
        from lenny.routes import oauth2 as routes
        sig = inspect.signature(routes.loans)
        assert set(sig.parameters) == {"request"}, (
            "loans() must not accept a patron selector")


class TestConsentIntegrity:
    """The consent screen is where a patron grants access, so what it says and
    what it accepts both matter. Both found by adversarial review."""

    def test_consent_form_carries_only_an_opaque_handle(
            self, app_client, client, session_cookie):
        """The POST must not re-accept client_id, scope or code_challenge as
        form fields. Anything the form carries is attacker-shapeable, and
        re-validating six fields duplicates the GET's logic besides."""
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        for leaked in ('name="code_challenge"', 'name="scope"', 'name="client_id"'):
            assert leaked not in r.text, (
                f"{leaked} is settable by whoever submits the form")

    def test_attack_handle_from_another_patron_is_refused(
            self, app_client, client, session_cookie):
        """CSRF. An attacker can obtain a valid handle by starting their own
        authorization, then try to get a victim's browser to submit it. Binding
        the handle to the patron who was shown the screen defeats that."""
        obj, _ = client
        _, challenge = pkce()
        attacker_handle = request_handle(
            app_client, obj, challenge,
            auth.create_session_cookie("attacker@example.org"))

        r = app_client.post(AUTHORIZE_URL,
                            data={"request": attacker_handle, "decision": "allow"},
                            cookies={"session": session_cookie},
                            follow_redirects=False)
        assert r.status_code == 403, (
            "a handle issued to one patron was accepted from another")

    def test_attack_forged_handle_is_refused(self, app_client, session_cookie):
        r = app_client.post(AUTHORIZE_URL,
                            data={"request": "not-a-real-handle", "decision": "allow"},
                            cookies={"session": session_cookie},
                            follow_redirects=False)
        assert r.status_code == 400

    def test_handle_still_produces_a_code(self, app_client, client, session_cookie):
        obj, _ = client
        _, challenge = pkce()
        handle = request_handle(app_client, obj, challenge, session_cookie)
        r = app_client.post(AUTHORIZE_URL,
                            data={"request": handle, "decision": "allow"},
                            cookies={"session": session_cookie},
                            follow_redirects=False)
        assert r.status_code == 303
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["code"][0]
        assert q["state"][0] == "state-123"

    def test_consent_shows_where_the_patron_will_be_sent(
            self, app_client, client, session_cookie):
        """Registration is open and `client_name` is unverified, so a client can
        call itself "Open Library". The redirect origin is the one thing the
        attacker cannot fake, so the patron must see it (RFC 7591 §5)."""
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        assert "consumer.example.org" in r.text, (
            "the consent screen does not show where the patron will be sent")

    def test_consent_says_the_library_vetted_this_client(
            self, app_client, client, session_cookie):
        """Registration is operator-only, so the name shown was vetted. Telling
        a patron the opposite would undersell the guarantee and train them to
        ignore the warning that does matter — the destination address."""
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        body = r.text.lower()
        assert "this library registered this application" in body
        assert "registered itself" not in body, (
            "stale copy from when anyone could self-register")

    def test_consent_does_not_promise_a_revocation_that_does_not_exist(
            self, app_client, client, session_cookie):
        """There is no patron-facing disconnect: `/oauth2/revoke` needs the
        client's own credentials, and `oauth2-disable` cuts every patron off at
        once. Promising "revoke at any time" is the same class of bug as the
        self-registration copy above — the screen describing a control the
        system does not have. If a connected-apps page is ever built, this test
        is the one to change.
        """
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        # Collapse whitespace: the sentence is wrapped across source lines, and
        # where the HTML happens to break is not what this test is about.
        body = " ".join(r.text.lower().split())
        assert "revoke this at any time" not in body
        assert "ask your librarian" in body, (
            "the patron is not told what they can actually do")


@pytest.mark.skipif(
    not DB_URI.startswith("postgresql"),
    reason="lending policy needs Postgres: Loan/Item use BigInteger primary keys, "
           "which SQLite does not autoincrement, and SELECT FOR UPDATE — the "
           "thing that makes the per-copy check safe — is a no-op there")
class TestBorrowRespectsLendingPolicy:
    """Round 2 found /oauth2/borrow calling Loan.create directly, bypassing
    Item.borrow — which is the only place lending policy lives. All four of its
    guarantees were skipped. No test covered a *successful* borrow, which is why
    it survived."""

    def _token(self, app_client, client, session_cookie):
        obj, secret = client
        verifier, challenge = pkce()
        code = get_code(app_client, obj, challenge, session_cookie)
        return app_client.post(TOKEN_URL, headers=basic(obj.client_id, secret), data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier}).json()["access_token"]

    def _item(self, edition, encrypted=True):
        """A catalogued item.

        The id is explicit because `Item.id` is `BigInteger`, which Postgres
        maps to BIGSERIAL but SQLite does not autoincrement — only INTEGER
        PRIMARY KEY does. Production is Postgres, so the model is left alone;
        supplying an id here keeps the same tests running on both.
        """
        from lenny.core.models import FormatEnum, Item
        if existing := Item.exists(edition):
            return existing
        item = Item(id=edition, openlibrary_edition=edition, encrypted=encrypted,
                    formats=list(FormatEnum)[0])
        db.add(item)
        db.commit()
        return item

    def test_a_borrow_actually_succeeds(self, app_client, client, session_cookie):
        """The happy path had no coverage at all."""
        self._item(70000001)
        token = self._token(app_client, client, session_cookie)
        r = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"edition_id": 70000001})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "borrowed"

    def test_attack_open_access_item_cannot_be_borrowed(
            self, app_client, client, session_cookie):
        """`Item.borrow` raises LoanNotRequiredError for an unencrypted item.
        Creating a loan for one is a phantom loan against a book that needs none."""
        self._item(70000002, encrypted=False)
        token = self._token(app_client, client, session_cookie)
        r = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"edition_id": 70000002})
        assert r.status_code == 400, f"open-access item was lent: {r.status_code}"

    def test_attack_loan_limit_is_enforced(self, app_client, client, session_cookie,
                                           monkeypatch):
        """Without Item.borrow the per-patron concurrent limit does not exist."""
        from lenny import configs
        monkeypatch.setattr(configs, "get_loan_limit", lambda: 2)
        for edition in (70000010, 70000011, 70000012, 70000013):
            self._item(edition)
        token = self._token(app_client, client, session_cookie)
        codes = [app_client.post("/v1/api/oauth2/borrow",
                                 headers={"Authorization": f"Bearer {token}"},
                                 data={"edition_id": e}).status_code
                 for e in (70000010, 70000011, 70000012, 70000013)]
        assert codes.count(201) <= 2, f"loan limit ignored: {codes}"
        assert 429 in codes, f"expected a rate/limit refusal, got {codes}"

    def test_attack_a_single_copy_cannot_be_lent_twice(
            self, app_client, client, session_cookie):
        """Two patrons, one copy. Without the per-item check both get it, and
        available_copies goes negative — so the OPDS feed lies about
        availability too."""
        item = self._item(70000020)
        token_a = self._token(app_client, client, session_cookie)
        other_session = auth.create_session_cookie("second-patron@example.org")
        token_b = self._token(app_client, client, other_session)

        a = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token_a}"},
                            data={"edition_id": 70000020})
        b = app_client.post("/v1/api/oauth2/borrow",
                            headers={"Authorization": f"Bearer {token_b}"},
                            data={"edition_id": 70000020})
        assert a.status_code == 201
        assert b.status_code == 409, (
            f"a single copy was lent to two patrons at once: {b.status_code}")
        assert item.num_lendable_total == 1


class TestConsentHandleIsSingleUse:
    """Round 2: the handle carried no nonce and no single-use marker, so one
    consent click authorised an unbounded number of grants for 600 seconds, and
    clicking Not now did not invalidate anything."""

    def test_attack_a_handle_cannot_be_replayed(
            self, app_client, client, session_cookie):
        obj, _ = client
        _, challenge = pkce()
        handle = request_handle(app_client, obj, challenge, session_cookie)
        first = app_client.post(AUTHORIZE_URL,
                                data={"request": handle, "decision": "allow"},
                                cookies={"session": session_cookie},
                                follow_redirects=False)
        assert first.status_code == 303

        second = app_client.post(AUTHORIZE_URL,
                                 data={"request": handle, "decision": "allow"},
                                 cookies={"session": session_cookie},
                                 follow_redirects=False)
        assert second.status_code != 303 or "code" not in parse_qs(
            urlparse(second.headers.get("location", "")).query), (
            "one consent click produced a second authorization code")

    def test_attack_denial_is_final(self, app_client, client, session_cookie):
        """A patron who declines must not have that decision undone by replaying
        the same handle with decision=allow."""
        obj, _ = client
        _, challenge = pkce()
        handle = request_handle(app_client, obj, challenge, session_cookie)
        denied = app_client.post(AUTHORIZE_URL,
                                 data={"request": handle, "decision": "deny"},
                                 cookies={"session": session_cookie},
                                 follow_redirects=False)
        assert "error=access_denied" in denied.headers["location"]

        retry = app_client.post(AUTHORIZE_URL,
                                data={"request": handle, "decision": "allow"},
                                cookies={"session": session_cookie},
                                follow_redirects=False)
        q = parse_qs(urlparse(retry.headers.get("location", "")).query)
        assert "code" not in q, "a declined request was later approved by replay"


class TestBrowserFacingHeaders:
    """Round 2: the app-wide credentialed CORS wildcard reflects any origin, and
    the consent screen had no framing defence."""

    def test_attack_consent_is_not_readable_cross_origin(
            self, app_client, client, session_cookie):
        """`allow_origin_regex='.*'` + `allow_credentials` reflects the caller's
        origin. On a cookie-authenticated page that renders a consent handle,
        that lets evil.example read the handle with a credentialed fetch and
        POST it back — an authorization code with no click. Only SameSite=Lax
        stands in the way today, and the OAuth code does not own that cookie."""
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie},
                           headers={"Origin": "https://evil.example"},
                           follow_redirects=False)
        allowed = r.headers.get("access-control-allow-origin")
        credentialed = r.headers.get("access-control-allow-credentials")
        assert not (allowed == "https://evil.example" and credentialed == "true"), (
            "consent page is readable cross-origin with credentials")

    def test_consent_cannot_be_framed(self, app_client, client, session_cookie):
        """RFC 6749 §10.13 / RFC 9700 §4.16 — clickjacking on the one screen
        where a patron grants access."""
        obj, _ = client
        _, challenge = pkce()
        r = app_client.get(AUTHORIZE_URL, params=authorize_params(obj, challenge),
                           cookies={"session": session_cookie}, follow_redirects=False)
        headers = {k.lower(): v for k, v in r.headers.items()}
        assert headers.get("x-frame-options") == "DENY" or \
            "frame-ancestors 'none'" in headers.get("content-security-policy", ""), \
            "the consent screen can be framed"


class TestSpecConformance:
    def test_authorization_response_carries_iss(self, app_client, client, session_cookie):
        """RFC 9207 / RFC 9700 §4.4 — mix-up defence. It matters here precisely
        because a consumer is expected to talk to many independent nodes."""
        obj, _ = client
        _, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert "iss" in q, "no iss in the authorization response"

    def test_metadata_advertises_iss_support(self, app_client):
        meta = app_client.get("/.well-known/oauth-authorization-server").json()
        assert meta.get("authorization_response_iss_parameter_supported") is True

    def test_invalid_client_carries_www_authenticate(self, app_client):
        """RFC 6749 §5.2: a 401 for a client that tried Basic auth MUST carry
        the challenge."""
        r = app_client.post(TOKEN_URL, headers=basic("nope", "nope"),
                            data={"grant_type": "refresh_token", "refresh_token": "x"})
        assert r.status_code == 401
        assert "www-authenticate" in {k.lower() for k in r.headers}

    @pytest.mark.parametrize("uri", [
        "https://app.example/cb#frag",              # RFC 6749 §3.1.2
        "https://app.example/cb\r\nX-Injected: 1",  # not URI syntax
        "http://app.example/cb",                    # plaintext, non-loopback
    ])
    def test_attack_malformed_redirect_uris_refused(self, uri):
        """A fragment would put the authorization code after the '#', where it
        never reaches the client's server. Checked by the same function the
        operator CLI calls, so a bad URI cannot be registered at all."""
        assert acceptable_redirect(uri) is False


class TestNativeAppClients:
    """RFC 8252 §7.1 — private-use URI scheme redirects.

    Lenny lends to reading apps, and production already hands native OPDS
    readers `opds://authorize/` through the older flow. Accepting only https
    and loopback meant no native client could register here at all — the OAuth
    server refused exactly the clients Lenny exists to serve.
    """

    @pytest.mark.parametrize("uri", [
        "opds://authorize/",
        "com.example.reader://oauth/callback",
    ])
    def test_native_schemes_are_registrable(self, uri):
        """An operator can register a reading app. Lenny already hands native
        OPDS readers `opds://authorize/` through the older flow, so refusing
        these would exclude the clients Lenny exists to serve."""
        assert acceptable_redirect(uri) is True

    @pytest.mark.parametrize("uri", [
        "myapp://cb",          # single-label scheme: not a domain anyone owns
        "javascript://cb",
        "data://cb",
        "file:///etc/passwd",
    ])
    def test_attack_undotted_or_dangerous_schemes_refused(self, uri):
        """RFC 8252 §7.1 wants a scheme based on a domain the client controls.
        A single-label scheme is squattable by any other app on the device."""
        assert acceptable_redirect(uri) is False

    def test_public_client_needs_no_secret(self):
        """A native app cannot keep one; PKCE is what authenticates it."""
        obj, secret = OAuthClient.register(
            name="Thorium", redirect_uris=["opds://authorize/"],
            scopes=["loans:read"], is_confidential=False)
        assert secret is None
        assert obj.verify_secret(None) is True

    def test_public_client_completes_the_flow_with_pkce_alone(
            self, app_client, session_cookie):
        """PKCE is what protects a client with no secret — and it is already
        mandatory here, which is why public clients are safe."""
        obj, _ = OAuthClient.register(
            name="Thorium", redirect_uris=["opds://authorize/"],
            scopes=["loans:read"], is_confidential=False)

        verifier, challenge = pkce()
        r = consent(app_client, obj, challenge, session_cookie,
                    redirect_uri="opds://authorize/", scope="loans:read")
        assert r.status_code == 200, (
            "a custom-scheme redirect should render, not 303 — a browser will "
            "not reliably follow one")
        m = re.search(r"opds://authorize/\?code=([A-Za-z0-9_\-]+)", r.text)
        assert m, f"no code handed to the native client: {r.text[:300]}"

        token = app_client.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": m.group(1),
            "redirect_uri": "opds://authorize/", "code_verifier": verifier,
            "client_id": obj.client_id})
        assert token.status_code == 200, token.text
        assert token.json()["scope"] == "loans:read"

    def test_metadata_advertises_public_clients(self, app_client):
        meta = app_client.get("/.well-known/oauth-authorization-server").json()
        assert "none" in meta["token_endpoint_auth_methods_supported"]

    def test_metadata_does_not_advertise_registration(self, app_client):
        """RFC 8414: advertising an endpoint that does not exist sends a
        consumer following discovery to a 404."""
        meta = app_client.get("/.well-known/oauth-authorization-server").json()
        assert "registration_endpoint" not in meta

    def test_the_registration_endpoint_is_gone(self, app_client):
        """Clients are an operator decision, not a public self-service."""
        r = app_client.post("/v1/api/oauth2/register", json={
            "client_name": "Anyone", "redirect_uris": ["https://anyone.example/cb"]})
        assert r.status_code in (404, 405), (
            "open registration is still reachable")
