"""
Unit tests for lenny.core.external_auth.

Covers:
  - OAuthConfig.from_env() env loading
  - PKCEHelper: verifier/challenge generation
  - OIDCProvider: authorization URL construction, email extraction
  - ExternalAuthService: is_enabled(), initiate_flow(), complete_flow()
  - ExternalAuthService.toggle() / save_config() — auth.env atomic write
  - ExternalAuthService.apply_in_process() — live config update
  - Route: GET /oauth/external/start  (disabled vs. configured)
  - Route: GET /oauth/external/callback (CSRF mismatch, success, provider error)

All OIDC HTTP calls are mocked; no real provider is required.
"""

import asyncio
import base64
import hashlib
import os
import secrets
import tempfile
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_config(
    *,
    enabled: bool = True,
    client_id: str = "test-client",
    client_secret: str = "test-secret",
    discovery_url: str = "https://provider.example.com",
    redirect_uri: str = "https://lenny.example.com/v1/api/oauth/external/callback",
    scopes: Optional[list] = None,
    flow: str = "pkce",
):
    from lenny.core.external_auth import OAuthConfig
    return OAuthConfig(
        enabled=enabled,
        client_id=client_id,
        client_secret=client_secret,
        discovery_url=discovery_url,
        redirect_uri=redirect_uri,
        scopes=scopes or ["openid", "email", "profile"],
        flow=flow,
    )


_FAKE_DISCOVERY = {
    "issuer": "https://provider.example.com",
    "authorization_endpoint": "https://provider.example.com/authorize",
    "token_endpoint": "https://provider.example.com/token",
    "jwks_uri": "https://provider.example.com/.well-known/jwks.json",
}


# ─────────────────────────────────────────────────────────────────────────────
# OAuthConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestOAuthConfig:
    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.setenv("LENNY_EXTERNAL_AUTH_ENABLED", "false")
        monkeypatch.delenv("OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("OAUTH_DISCOVERY_URL", raising=False)
        monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)
        monkeypatch.delenv("OAUTH_SCOPES", raising=False)
        monkeypatch.delenv("OAUTH_FLOW", raising=False)

        # Reload configs with the patched env
        import importlib
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", None)
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", None)
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", None)
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid", "email", "profile"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig.from_env()
        assert cfg.enabled is False
        assert cfg.client_id == ""
        assert cfg.flow == "pkce"
        assert "email" in cfg.scopes

    def test_from_env_enabled(self, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "my-id")
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_SECRET", "my-secret")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://clerk.example.com")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "https://lenny.example.com/cb")
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid", "email"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig.from_env()
        assert cfg.enabled is True
        assert cfg.client_id == "my-id"
        assert cfg.is_configured()

    def test_is_configured_false_when_missing_fields(self):
        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig(
            enabled=True, client_id="", client_secret="",
            discovery_url="", redirect_uri="",
            scopes=["openid"], flow="pkce"
        )
        assert cfg.is_configured() is False

    def test_from_auth_env_falls_back_to_global_when_no_file(self, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "global-id")
        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig.from_auth_env("/nonexistent/auth.env")
        assert cfg.enabled is True
        assert cfg.client_id == "global-id"

    def test_from_auth_env_file_overrides_stale_global(self, monkeypatch, tmp_path):
        from lenny import configs as lenny_configs
        # Simulate a worker whose in-process globals are stale (disabled, old id).
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "stale-id")
        auth_env = tmp_path / "auth.env"
        auth_env.write_text(
            "LENNY_EXTERNAL_AUTH_ENABLED=true\n"
            "OAUTH_CLIENT_ID=fresh-id\n"
            "OAUTH_DISCOVERY_URL=https://p.example.com\n"
            "OAUTH_REDIRECT_URI=https://l.example.com/cb\n"
        )
        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig.from_auth_env(str(auth_env))
        assert cfg.enabled is True          # file wins over stale global
        assert cfg.client_id == "fresh-id"
        assert cfg.is_configured()

    def test_unknown_flow_falls_back_to_pkce(self, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "x")
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_SECRET", "y")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://p.example.com")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "https://l.example.com/cb")
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "UNSUPPORTED_FLOW")

        from lenny.core.external_auth import OAuthConfig
        cfg = OAuthConfig.from_env()
        assert cfg.flow == "pkce"


# ─────────────────────────────────────────────────────────────────────────────
# PKCEHelper
# ─────────────────────────────────────────────────────────────────────────────

class TestPKCEHelper:
    def test_verifier_is_base64url(self):
        from lenny.core.external_auth import PKCEHelper
        v = PKCEHelper.generate_verifier()
        # Must be decodable as base64url without padding
        decoded = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        assert len(decoded) == 32

    def test_verifier_no_padding_chars(self):
        from lenny.core.external_auth import PKCEHelper
        v = PKCEHelper.generate_verifier()
        assert "=" not in v
        assert "+" not in v  # urlsafe base64 uses - and _ instead

    def test_challenge_is_s256_of_verifier(self):
        from lenny.core.external_auth import PKCEHelper
        verifier = PKCEHelper.generate_verifier()
        challenge = PKCEHelper.generate_challenge(verifier)
        expected_digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_verifiers_are_unique(self):
        from lenny.core.external_auth import PKCEHelper
        v1 = PKCEHelper.generate_verifier()
        v2 = PKCEHelper.generate_verifier()
        assert v1 != v2

    def test_challenge_no_padding(self):
        from lenny.core.external_auth import PKCEHelper
        verifier = PKCEHelper.generate_verifier()
        challenge = PKCEHelper.generate_challenge(verifier)
        assert "=" not in challenge


# ─────────────────────────────────────────────────────────────────────────────
# OIDCProvider
# ─────────────────────────────────────────────────────────────────────────────

class TestOIDCProvider:
    def test_authorization_url_pkce_includes_challenge(self):
        from lenny.core.external_auth import OIDCProvider, PKCEHelper
        cfg = _make_config(flow="pkce")
        provider = OIDCProvider(cfg)
        verifier = PKCEHelper.generate_verifier()

        url = provider.authorization_url(
            "https://provider.example.com/authorize",
            state="mystate",
            nonce="mynonce",
            code_verifier=verifier,
        )
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "state=mystate" in url
        assert "nonce=mynonce" in url
        assert "response_type=code" in url

    def test_authorization_url_always_uses_code_response_type(self):
        """PKCE-only: response_type is always 'code', never 'token'."""
        from lenny.core.external_auth import OIDCProvider, PKCEHelper
        cfg = _make_config(flow="pkce")
        provider = OIDCProvider(cfg)
        verifier = PKCEHelper.generate_verifier()

        url = provider.authorization_url(
            "https://provider.example.com/authorize",
            state="s",
            nonce="n",
            code_verifier=verifier,
        )
        assert "response_type=code" in url
        assert "response_type=token" not in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url

    def test_extract_email_happy_path(self):
        from lenny.core.external_auth import OIDCProvider
        email = OIDCProvider.extract_email({"email": "User@Example.com", "email_verified": True})
        assert email == "user@example.com"

    def test_extract_email_strips_and_lowercases(self):
        from lenny.core.external_auth import OIDCProvider
        email = OIDCProvider.extract_email({"email": "  Alice@Example.COM  "})
        assert email == "alice@example.com"

    def test_extract_email_raises_if_missing(self):
        from lenny.core.external_auth import OIDCProvider, OIDCTokenError
        with pytest.raises(OIDCTokenError, match="email claim"):
            OIDCProvider.extract_email({})

    def test_extract_email_raises_if_explicitly_unverified(self):
        from lenny.core.external_auth import OIDCProvider, OIDCTokenError
        with pytest.raises(OIDCTokenError, match="not verified"):
            OIDCProvider.extract_email({"email": "a@b.com", "email_verified": False})

    def test_extract_email_passes_when_verified_absent(self):
        """email_verified absent (not False) should still pass."""
        from lenny.core.external_auth import OIDCProvider
        email = OIDCProvider.extract_email({"email": "a@b.com"})
        assert email == "a@b.com"

    def test_discover_caches_result(self):
        from lenny.core.external_auth import OIDCProvider, _DISCOVERY_CACHE, _DISCOVERY_CACHE_TS
        cfg = _make_config(discovery_url="https://cache.example.com")
        provider = OIDCProvider(cfg)

        _DISCOVERY_CACHE.clear()
        _DISCOVERY_CACHE_TS.clear()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = _FAKE_DISCOVERY.copy()

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value = mock_client

                doc1 = await provider.discover()
                doc2 = await provider.discover()  # should hit cache

                # get() called only once
                assert mock_client.get.call_count == 1
                assert doc1 == doc2

        asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# ExternalAuthService
# ─────────────────────────────────────────────────────────────────────────────

class TestExternalAuthService:
    def test_is_enabled_false(self):
        from lenny.core.external_auth import ExternalAuthService
        svc = ExternalAuthService(_make_config(enabled=False))
        assert svc.is_enabled() is False

    def test_is_enabled_true(self):
        from lenny.core.external_auth import ExternalAuthService
        svc = ExternalAuthService(_make_config(enabled=True))
        assert svc.is_enabled() is True

    def test_assert_enabled_raises_when_disabled(self):
        from lenny.core.external_auth import ExternalAuthService
        svc = ExternalAuthService(_make_config(enabled=False))
        with pytest.raises(RuntimeError, match="not enabled"):
            svc.assert_enabled()

    def test_assert_enabled_raises_when_not_configured(self):
        from lenny.core.external_auth import ExternalAuthService
        svc = ExternalAuthService(_make_config(enabled=True, client_id="", discovery_url=""))
        with pytest.raises(RuntimeError, match="not fully configured"):
            svc.assert_enabled()

    def test_initiate_flow_returns_url_state_nonce_verifier(self):
        from lenny.core.external_auth import ExternalAuthService

        async def _run():
            svc = ExternalAuthService(_make_config())
            with patch.object(svc._provider, "discover", AsyncMock(return_value=_FAKE_DISCOVERY)):
                auth_url, state, nonce, code_verifier = await svc.initiate_flow()

            assert "provider.example.com/authorize" in auth_url
            assert len(state) == 64   # 32 bytes hex
            assert len(nonce) == 64
            assert code_verifier is not None  # PKCE
            assert "code_challenge" in auth_url

        asyncio.run(_run())

    def test_complete_flow_returns_email(self):
        from lenny.core.external_auth import ExternalAuthService

        async def _run():
            svc = ExternalAuthService(_make_config())
            fake_claims = {
                "email": "patron@example.com",
                "email_verified": True,
                "iss": "https://provider.example.com",
                "aud": "test-client",
                "nonce": "testnonce",
            }
            with (
                patch.object(svc._provider, "discover", AsyncMock(return_value=_FAKE_DISCOVERY)),
                patch.object(
                    svc._provider, "exchange_code",
                    AsyncMock(return_value={"id_token": "fake.id.token"}),
                ),
                patch.object(
                    svc._provider, "validate_id_token",
                    AsyncMock(return_value=fake_claims),
                ),
            ):
                email = await svc.complete_flow(
                    code="authcode", nonce="testnonce"
                )
            assert email == "patron@example.com"

        asyncio.run(_run())

    def test_complete_flow_raises_on_missing_id_token(self):
        from lenny.core.external_auth import ExternalAuthService, OIDCTokenError

        async def _run():
            svc = ExternalAuthService(_make_config())
            with (
                patch.object(svc._provider, "discover", AsyncMock(return_value=_FAKE_DISCOVERY)),
                patch.object(
                    svc._provider, "exchange_code",
                    AsyncMock(return_value={"access_token": "only-access"}),
                ),
            ):
                with pytest.raises(OIDCTokenError, match="id_token"):
                    await svc.complete_flow(code="c", nonce="n")

        asyncio.run(_run())

    def test_toggle_writes_auth_env(self):
        from lenny.core.external_auth import ExternalAuthService

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("ADMIN_USERNAME=admin\nLENNY_EXTERNAL_AUTH_ENABLED=false\n")
            tmp_path = f.name

        try:
            ExternalAuthService.toggle(True, auth_env_path=tmp_path)
            with open(tmp_path) as fh:
                content = fh.read()
            assert "LENNY_EXTERNAL_AUTH_ENABLED=true" in content
            assert "ADMIN_USERNAME=admin" in content  # unrelated lines preserved
        finally:
            os.unlink(tmp_path)

    def test_toggle_false_writes_false(self):
        from lenny.core.external_auth import ExternalAuthService

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("LENNY_EXTERNAL_AUTH_ENABLED=true\n")
            tmp_path = f.name

        try:
            ExternalAuthService.toggle(False, auth_env_path=tmp_path)
            with open(tmp_path) as fh:
                assert "LENNY_EXTERNAL_AUTH_ENABLED=false" in fh.read()
        finally:
            os.unlink(tmp_path)

    def test_save_config_persists_multiple_keys(self):
        from lenny.core.external_auth import ExternalAuthService

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("OAUTH_CLIENT_ID=\nOAUTH_FLOW=pkce\n")
            tmp_path = f.name

        try:
            ExternalAuthService.save_config(
                {"OAUTH_CLIENT_ID": "new-id", "OAUTH_FLOW": "pkce"},
                auth_env_path=tmp_path,
            )
            with open(tmp_path) as fh:
                content = fh.read()
            assert "OAUTH_CLIENT_ID=new-id" in content
            assert "OAUTH_FLOW=pkce" in content
        finally:
            os.unlink(tmp_path)

    def test_apply_in_process_updates_configs(self, monkeypatch):
        from lenny import configs as lenny_configs
        from lenny.core.external_auth import ExternalAuthService

        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", None)
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        ExternalAuthService.apply_in_process({
            "LENNY_EXTERNAL_AUTH_ENABLED": "true",
            "OAUTH_CLIENT_ID": "live-id",
            "OAUTH_FLOW": "pkce",
        })

        assert lenny_configs.EXTERNAL_AUTH_ENABLED is True
        assert lenny_configs.OAUTH_CLIENT_ID == "live-id"
        assert lenny_configs.OAUTH_FLOW == "pkce"


# ─────────────────────────────────────────────────────────────────────────────
# Shared FastAPI test client (used by the external start/callback route tests)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def app_client():
    """FastAPI test client with TESTING env var set."""
    os.environ.setdefault("TESTING", "true")
    os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")
    from lenny.app import app
    return TestClient(app, raise_server_exceptions=True)


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /oauth/external/start
# ─────────────────────────────────────────────────────────────────────────────

class TestOAuthExternalStartRoute:
    def test_start_returns_503_when_disabled(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        resp = app_client.get("/v1/api/oauth/external/start", follow_redirects=False)
        assert resp.status_code == 503
        assert resp.json()["error"] == "external_auth_disabled"

    def test_start_returns_503_when_not_configured(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "")
        resp = app_client.get("/v1/api/oauth/external/start", follow_redirects=False)
        assert resp.status_code == 503
        assert resp.json()["error"] == "not_configured"

    def test_start_redirects_to_provider(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "cid")
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_SECRET", "csec")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://provider.example.com")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "https://lenny.example.com/cb")
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid", "email"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        from lenny.core.external_auth import OIDCProvider
        with patch.object(
            OIDCProvider, "discover", AsyncMock(return_value=_FAKE_DISCOVERY)
        ):
            resp = app_client.get("/v1/api/oauth/external/start", follow_redirects=False)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "provider.example.com/authorize" in location
        assert "code_challenge" in location
        # State cookie must be set
        assert "_oidc_state" in resp.cookies


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET /oauth/external/callback
# ─────────────────────────────────────────────────────────────────────────────

class TestOAuthExternalCallbackRoute:
    def _make_state_cookie(self, state: str, nonce: str = "testnonce", cv: str = "verifier") -> str:
        """Produce a valid signed state cookie for testing."""
        os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")
        from lenny import configs as lenny_configs
        from itsdangerous import URLSafeTimedSerializer
        s = URLSafeTimedSerializer(lenny_configs.SEED, salt="oidc-state")
        return s.dumps({"s": state, "n": nonce, "cv": cv})

    def test_callback_missing_code_returns_400(self, app_client):
        resp = app_client.get("/v1/api/oauth/external/callback?state=abc")
        assert resp.status_code == 400

    def test_callback_provider_error_returns_401(self, app_client):
        resp = app_client.get(
            "/v1/api/oauth/external/callback?error=access_denied&error_description=Denied"
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "provider_error"

    def test_callback_missing_state_cookie_returns_403(self, app_client):
        resp = app_client.get("/v1/api/oauth/external/callback?code=xyz&state=abc")
        assert resp.status_code == 403
        assert "state" in resp.json()["error"]

    def test_callback_state_mismatch_returns_403(self, app_client):
        cookie_val = self._make_state_cookie(state="correct_state")
        resp = app_client.get(
            "/v1/api/oauth/external/callback?code=xyz&state=WRONG_STATE",
            cookies={"_oidc_state": cookie_val},
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "state_mismatch"

    def test_callback_success_issues_session_cookie(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "cid")
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_SECRET", "csec")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://provider.example.com")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "https://lenny.example.com/cb")
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid", "email"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        the_state = "validstate123"
        cookie_val = self._make_state_cookie(state=the_state, nonce="n1", cv="verif")

        from lenny.core.external_auth import ExternalAuthService
        with patch.object(
            ExternalAuthService,
            "complete_flow",
            AsyncMock(return_value="patron@example.com"),
        ):
            resp = app_client.get(
                f"/v1/api/oauth/external/callback?code=authcode&state={the_state}",
                cookies={"_oidc_state": cookie_val},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "session" in resp.cookies
        # Temp cookie must be cleared
        oidc_cookie = resp.cookies.get("_oidc_state")
        # Either deleted (max_age=0) or absent
        assert oidc_cookie is None or oidc_cookie == ""

    def test_callback_open_redirect_blocked(self, app_client, monkeypatch):
        """redirect_to is taken from the signed state cookie, not the callback
        query string — an attacker-controlled query param is silently ignored."""
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "cid")
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_SECRET", "csec")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://provider.example.com")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "https://lenny.example.com/cb")
        monkeypatch.setattr(lenny_configs, "OAUTH_SCOPES", ["openid", "email"])
        monkeypatch.setattr(lenny_configs, "OAUTH_FLOW", "pkce")

        the_state = "validstate456"
        # State cookie has no "r" key → callback falls back to /v1/api/opds
        cookie_val = self._make_state_cookie(state=the_state)

        from lenny.core.external_auth import ExternalAuthService
        with patch.object(
            ExternalAuthService,
            "complete_flow",
            AsyncMock(return_value="user@example.com"),
        ):
            resp = app_client.get(
                f"/v1/api/oauth/external/callback?code=c&state={the_state}"
                "&redirect_to=https://evil.example.com/steal",
                cookies={"_oidc_state": cookie_val},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        location = resp.headers.get("location", "")
        assert "evil.example.com" not in location
        assert location == "/v1/api/opds"


# ─────────────────────────────────────────────────────────────────────────────
# Route: GET/POST /oauth/authorize — OTP fallback redirect
# ─────────────────────────────────────────────────────────────────────────────

class TestOAuthAuthorizeRedirect:
    """When lending is off but external auth is on, /oauth/authorize redirects
    to /oauth/external/start instead of returning a 503."""

    def test_redirects_to_external_start_when_lending_off_and_external_on(
        self, app_client, monkeypatch
    ):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LENDING_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        # Provide the minimum fields required for is_configured() to return True
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "test-client")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://provider/.well-known/openid-configuration")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "http://localhost/v1/api/oauth/external/callback")
        resp = app_client.get("/v1/api/oauth/authorize", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/v1/api/oauth/external/start"

    def test_opds_params_forwarded_to_external_start(
        self, app_client, monkeypatch
    ):
        """OPDS redirect_uri and state must be forwarded so the callback can
        complete the implicit flow and return a token to the OPDS client."""
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LENDING_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        # Provide the minimum fields required for is_configured() to return True
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "test-client")
        monkeypatch.setattr(lenny_configs, "OAUTH_DISCOVERY_URL", "https://provider/.well-known/openid-configuration")
        monkeypatch.setattr(lenny_configs, "OAUTH_REDIRECT_URI", "http://localhost/v1/api/oauth/external/callback")
        resp = app_client.get(
            "/v1/api/oauth/authorize"
            "?redirect_uri=opds://authorize/&state=opdsstate123&client_id=opds",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "opds_redirect_uri=opds" in location
        assert "opds_state=opdsstate123" in location

    def test_returns_503_when_lending_off_and_external_off(
        self, app_client, monkeypatch
    ):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LENDING_ENABLED", False)
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        resp = app_client.get("/v1/api/oauth/authorize", follow_redirects=False)
        assert resp.status_code == 503


class TestAuthorizeRejectsUnusableRedirectUri:
    """Regression: a rejected redirect_uri must fail loudly, not silently.

    Reported 2026-09-02 — patrons signing in to Lenny from a separate-origin
    browser OPDS client (reader.archive.org) authenticated successfully and were
    never returned to the app. `_safe_opds_redirect` returned "" for the client's
    https URI (its host was not in the empty-by-default LENNY_OPDS_ALLOWED_HOSTS)
    and the caller substituted "opds://authorize/", so the response was
    indistinguishable from one where no redirect_uri had been sent at all: HTTP
    200, a normal OTP form, and a dead-end success page after login.
    """

    OPDS_CLIENT = "https://reader.example.org"

    @pytest.fixture(autouse=True)
    def _lending_on_external_off(self, monkeypatch):
        """Production's shape: OTP lending live, external OIDC off."""
        from lenny import configs as lenny_configs
        from lenny.core.external_auth import OAuthConfig
        monkeypatch.setattr(lenny_configs, "LENDING_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", False)
        # patron_auth_mode() == "ol" needs lending mode "ol" plus both S3 keys.
        monkeypatch.setattr(lenny_configs, "read_lending_mode", lambda: "ol")
        monkeypatch.setattr(lenny_configs, "OL_S3_ACCESS_KEY", "test-access")
        monkeypatch.setattr(lenny_configs, "OL_S3_SECRET_KEY", "test-secret")
        # ...and external must read as off, which comes from auth.env, not configs.
        monkeypatch.setattr(
            OAuthConfig, "from_auth_env", classmethod(lambda cls, path=None: _make_config(enabled=False)),
        )

    def test_https_redirect_uri_not_in_allowlist_is_a_400(self, app_client, monkeypatch):
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "")
        resp = app_client.get(
            f"/v1/api/oauth/authorize?redirect_uri={self.OPDS_CLIENT}&state=abc123",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_redirect_uri"
        # The caller is told which URI was refused and which setting governs it.
        assert body["redirect_uri"] == self.OPDS_CLIENT
        assert "LENNY_OPDS_ALLOWED_HOSTS" in body["message"]

    def test_allowlisted_https_redirect_uri_survives_into_the_form(
        self, app_client, monkeypatch
    ):
        """The fix must not merely error — the allowlisted case has to work."""
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "reader.example.org")
        resp = app_client.get(
            f"/v1/api/oauth/authorize?redirect_uri={self.OPDS_CLIENT}&state=abc123",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # The OTP form must carry the client's URI forward, not "opds://authorize/",
        # or the POST that follows loses it and strands the patron anyway.
        assert f'name="redirect_uri" value="{self.OPDS_CLIENT}"' in resp.text
        assert 'value="opds://authorize/"' not in resp.text

    def test_omitted_redirect_uri_still_defaults_quietly(self, app_client, monkeypatch):
        """Sending none is legitimate — only a *rejected* one is an error."""
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "")
        resp = app_client.get("/v1/api/oauth/authorize?state=abc123", follow_redirects=False)
        assert resp.status_code == 200
        assert 'name="redirect_uri" value="opds://authorize/"' in resp.text

    def test_open_redirect_is_still_refused(self, app_client, monkeypatch):
        """The allowlist is an open-redirect defence; erroring must not relax it."""
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "reader.example.org")
        resp = app_client.get(
            "/v1/api/oauth/authorize?redirect_uri=https://evil.example.com/steal",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"

    def test_rejection_is_logged_with_the_offending_host(self, app_client, monkeypatch, caplog):
        """An operator reading `make log` must be able to see why login broke."""
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "")
        with caplog.at_level("WARNING", logger="lenny.routes.oauth"):
            app_client.get(
                f"/v1/api/oauth/authorize?redirect_uri={self.OPDS_CLIENT}",
                follow_redirects=False,
            )
        assert "reader.example.org" in caplog.text
        assert "LENNY_OPDS_ALLOWED_HOSTS" in caplog.text


class TestExternalStartRejectsUnusableRedirectUri:
    """Same silent-drop existed on the external OIDC entry point."""

    def test_bad_opds_redirect_uri_is_a_400(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "EXTERNAL_AUTH_ENABLED", True)
        monkeypatch.setattr(lenny_configs, "OAUTH_CLIENT_ID", "test-client")
        monkeypatch.setattr(
            lenny_configs, "OAUTH_DISCOVERY_URL",
            "https://provider/.well-known/openid-configuration",
        )
        monkeypatch.setattr(
            lenny_configs, "OAUTH_REDIRECT_URI",
            "http://localhost/v1/api/oauth/external/callback",
        )
        monkeypatch.setenv("LENNY_OPDS_ALLOWED_HOSTS", "")
        resp = app_client.get(
            "/v1/api/oauth/external/start?opds_redirect_uri=https://reader.example.org",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"


class TestSafeOpdsRedirect:
    """Unit tests for the _safe_opds_redirect open-redirect guard."""

    def _redirect(self, url: str, allowed_hosts: str = "") -> str:
        import os
        from unittest.mock import patch
        from lenny.routes.oauth import _safe_opds_redirect
        with patch.dict(os.environ, {"LENNY_OPDS_ALLOWED_HOSTS": allowed_hosts}):
            return _safe_opds_redirect(url)

    def test_opds_scheme_always_allowed(self):
        assert self._redirect("opds://authorize/") == "opds://authorize/"

    def test_relative_v1_api_path_allowed(self):
        assert self._redirect("/v1/api/opds") == "/v1/api/opds"

    def test_relative_path_outside_v1_blocked(self):
        assert self._redirect("/admin") == ""

    def test_https_blocked_when_no_allowlist(self):
        assert self._redirect("https://evil.com/steal") == ""

    def test_https_blocked_when_host_not_in_allowlist(self):
        assert self._redirect("https://evil.com/steal", allowed_hosts="good.client.com") == ""

    def test_https_allowed_when_host_in_allowlist(self):
        url = "https://good.client.com/opds/auth"
        assert self._redirect(url, allowed_hosts="good.client.com") == url

    def test_https_allowed_with_multiple_hosts_in_allowlist(self):
        url = "https://second.client.com/cb"
        assert self._redirect(url, allowed_hosts="first.com, second.client.com, third.com") == url

    def test_http_always_blocked(self):
        assert self._redirect("http://example.com/opds") == ""

    def test_empty_string_returns_empty(self):
        assert self._redirect("") == ""

    def test_protocol_relative_blocked(self):
        assert self._redirect("//evil.com/steal") == ""

    def test_https_allowed_with_explicit_port_in_allowlist(self):
        url = "https://good.com:8443/opds/auth"
        assert self._redirect(url, allowed_hosts="good.com:8443") == url

    def test_https_blocked_when_port_mismatch(self):
        # allowlist has no port but URL has one — netloc won't match
        assert self._redirect("https://good.com:8443/opds", allowed_hosts="good.com") == ""

    def test_uppercase_scheme_blocked(self):
        # urlparse normalises scheme to lowercase; HTTPS:// has no scheme match path
        assert self._redirect("HTTPS://evil.com/steal", allowed_hosts="evil.com") == ""
