#!/usr/bin/env python
"""
External OAuth / OIDC authentication support.

Provides generic OpenID Connect (OIDC) integration so operators can plug in
any standards-compliant provider (Clerk, Auth0, Okta, Keycloak, Google, …)
as an alternative patron auth path while leaving the existing OTP flow
completely untouched.

Only PKCE (Authorization Code + PKCE / S256) is supported. Implicit flow is
not supported: the provider redirect returns a token in the URL fragment which
is never sent to the server, making it incompatible with Lenny's server-side
callback architecture. Plain Authorization Code (no PKCE) is also removed —
PKCE is mandatory per OAuth 2.1 and all current providers support it.

Flow overview:
  1. ExternalAuthService.initiate_flow()
       → generates state, nonce, code_verifier
       → returns (auth_url, state, nonce, code_verifier)
  2. Browser redirects to provider; user authenticates
  3. Provider redirects to /oauth/external/callback?code=…&state=…
  4. ExternalAuthService.complete_flow(code, state, nonce, code_verifier)
       → validates state, exchanges code for tokens, validates ID token
       → returns verified email string
  5. Route issues a standard Lenny session cookie for that email
       → all downstream borrow/shelf/profile code unchanged
"""

import hashlib
import logging
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Literal, Optional

import httpx

logger = logging.getLogger(__name__)

# ── In-process OIDC discovery cache (TTL: 30 min) ────────────────────────────
_DISCOVERY_CACHE: dict[str, dict] = {}
_DISCOVERY_CACHE_TS: dict[str, float] = {}
_DISCOVERY_TTL = 1800  # seconds


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file into a dict; ``{}`` if unreadable.

    Used so config reads can come straight from auth.env (the cross-worker source
    of truth) rather than a per-worker cached global. Keys present-but-empty map
    to ``""``; missing keys are simply absent from the dict.
    """
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                out[key.strip()] = value
    except OSError:
        return {}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# OAuthConfig
# ─────────────────────────────────────────────────────────────────────────────

# `prompt` values Lenny will honour and pass on to the provider. A fixed
# allow-list: whatever else a caller sends is dropped, never forwarded.
PROMPTS = ("login", "select_account")


def valid_prompt(value: Optional[str]) -> Optional[str]:
    """The first allowed `prompt` token in a space-separated value, or None."""
    for token in (value or "").split():
        if token in PROMPTS:
            return token
    return None


@dataclass
class OAuthConfig:
    """All configuration needed to drive an OIDC provider.

    Load from environment via ``OAuthConfig.from_env()``.  The dataclass is
    intentionally value-only — no side effects in __init__.
    """

    enabled: bool
    client_id: str
    client_secret: str
    discovery_url: str
    redirect_uri: str
    scopes: list[str]
    flow: Literal["pkce"]

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "OAuthConfig":
        from lenny import configs  # deferred to avoid circular import at module load
        flow = configs.OAUTH_FLOW or "pkce"
        if flow != "pkce":
            logger.warning(
                "OAUTH_FLOW=%r is not supported. Only 'pkce' is allowed. "
                "Implicit and plain Authorization Code flows have been removed. "
                "Defaulting to 'pkce'.",
                flow,
            )
            flow = "pkce"
        return cls(
            enabled=configs.EXTERNAL_AUTH_ENABLED,
            client_id=configs.OAUTH_CLIENT_ID or "",
            client_secret=configs.OAUTH_CLIENT_SECRET or "",
            discovery_url=configs.OAUTH_DISCOVERY_URL or "",
            redirect_uri=configs.OAUTH_REDIRECT_URI or "",
            scopes=list(configs.OAUTH_SCOPES) if configs.OAUTH_SCOPES else ["openid", "email", "profile"],
            flow=flow,
        )

    @classmethod
    def from_auth_env(cls, auth_env_path: str = "/app/auth.env") -> "OAuthConfig":
        """Like :meth:`from_env`, but reads ``auth.env`` directly.

        ``save_config`` writes the file while ``apply_in_process`` only updates the
        handling worker's globals, so :meth:`from_env` can be stale on other workers.
        Reading the file gives every worker the same view. Any key absent from the
        file falls back to the in-process config (and ultimately the boot default).
        """
        from lenny import configs  # deferred to avoid circular import at module load
        raw = _read_env_file(auth_env_path)

        enabled_raw = raw.get("LENNY_EXTERNAL_AUTH_ENABLED")
        enabled = (
            enabled_raw.lower() == "true"
            if enabled_raw is not None
            else configs.EXTERNAL_AUTH_ENABLED
        )

        scopes_raw = raw.get("OAUTH_SCOPES")
        if scopes_raw is not None:
            scopes = scopes_raw.split() or ["openid", "email", "profile"]
        else:
            scopes = list(configs.OAUTH_SCOPES) if configs.OAUTH_SCOPES else ["openid", "email", "profile"]

        flow = raw.get("OAUTH_FLOW", configs.OAUTH_FLOW or "pkce") or "pkce"
        if flow != "pkce":
            flow = "pkce"

        return cls(
            enabled=enabled,
            client_id=raw.get("OAUTH_CLIENT_ID", configs.OAUTH_CLIENT_ID or ""),
            client_secret=raw.get("OAUTH_CLIENT_SECRET", configs.OAUTH_CLIENT_SECRET or ""),
            discovery_url=raw.get("OAUTH_DISCOVERY_URL", configs.OAUTH_DISCOVERY_URL or ""),
            redirect_uri=raw.get("OAUTH_REDIRECT_URI", configs.OAUTH_REDIRECT_URI or ""),
            scopes=scopes,
            flow=flow,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when minimum fields are present to attempt a flow."""
        return bool(self.client_id and self.discovery_url and self.redirect_uri)


# ─────────────────────────────────────────────────────────────────────────────
# PKCEHelper
# ─────────────────────────────────────────────────────────────────────────────

class PKCEHelper:
    """RFC 7636 PKCE utilities — pure static, no state."""

    @staticmethod
    def generate_verifier() -> str:
        """Return a 43-octet base64url-encoded random code verifier (RFC 7636 §4.1)."""
        raw = secrets.token_bytes(32)
        # base64url, no padding — exactly as the spec requires
        import base64
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def generate_challenge(verifier: str) -> str:
        """Return the S256 code challenge for *verifier* (RFC 7636 §4.2)."""
        import base64
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# OIDCProvider
# ─────────────────────────────────────────────────────────────────────────────

class OIDCDiscoveryError(Exception):
    """Raised when the provider's discovery document cannot be fetched/parsed."""


class OIDCTokenError(Exception):
    """Raised when token exchange or ID-token validation fails."""


class OIDCProvider:
    """
    Generic OpenID Connect provider.

    Uses ``authlib`` for OAuth 2.0 client operations and httpx for async
    HTTP.  The discovery document is cached in-process for ``_DISCOVERY_TTL``
    seconds to avoid hitting the provider on every request.
    """

    def __init__(self, config: OAuthConfig) -> None:
        self._cfg = config

    # ── discovery ─────────────────────────────────────────────────────────────

    async def discover(self) -> dict:
        """Fetch (and cache) the OIDC well-known configuration document."""
        import time

        url = self._cfg.discovery_url
        now = time.monotonic()

        cached_ts = _DISCOVERY_CACHE_TS.get(url, 0)
        if url in _DISCOVERY_CACHE and (now - cached_ts) < _DISCOVERY_TTL:
            return _DISCOVERY_CACHE[url]

        # Normalise: append /.well-known/openid-configuration if not present
        if not url.endswith("/openid-configuration"):
            wk_url = url.rstrip("/") + "/.well-known/openid-configuration"
        else:
            wk_url = url

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(wk_url)
                resp.raise_for_status()
                doc = resp.json()
        except httpx.HTTPError as exc:
            # Invalidate stale cache so next caller retries
            _DISCOVERY_CACHE.pop(url, None)
            _DISCOVERY_CACHE_TS.pop(url, None)
            raise OIDCDiscoveryError(
                f"Could not fetch OIDC discovery document from {wk_url}: {exc}"
            ) from exc
        except ValueError as exc:
            # Provider returned non-JSON (e.g. HTML maintenance page with 200 status)
            _DISCOVERY_CACHE.pop(url, None)
            _DISCOVERY_CACHE_TS.pop(url, None)
            raise OIDCDiscoveryError(
                f"OIDC discovery document from {wk_url} is not valid JSON: {exc}"
            ) from exc

        _DISCOVERY_CACHE[url] = doc
        _DISCOVERY_CACHE_TS[url] = now
        return doc

    # ── authorization URL ─────────────────────────────────────────────────────

    def authorization_url(
        self,
        authorization_endpoint: str,
        state: str,
        nonce: str,
        code_verifier: str,
        prompt: Optional[str] = None,
    ) -> str:
        """Build the provider's authorization URL.

        If *code_verifier* is supplied (PKCE) the S256 challenge is appended.
        *nonce* is always included for replay-attack protection.
        *prompt* (``login`` or ``select_account``) asks the provider to show its
        sign-in or account chooser even if it already has a session.
        """
        from urllib.parse import urlencode

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._cfg.client_id,
            "redirect_uri": self._cfg.redirect_uri,
            "scope": " ".join(self._cfg.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": PKCEHelper.generate_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        if prompt:
            params["prompt"] = prompt

        sep = "&" if "?" in authorization_endpoint else "?"
        return f"{authorization_endpoint}{sep}{urlencode(params)}"

    # ── token exchange ────────────────────────────────────────────────────────

    async def exchange_code(
        self,
        token_endpoint: str,
        code: str,
        code_verifier: Optional[str] = None,
    ) -> dict:
        """Exchange *code* for tokens at *token_endpoint*.

        Returns the raw token response dict (contains ``id_token``,
        ``access_token``, etc.).
        Raises ``OIDCTokenError`` on any failure.
        """
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        async with AsyncOAuth2Client(
            client_id=self._cfg.client_id,
            client_secret=self._cfg.client_secret,
            redirect_uri=self._cfg.redirect_uri,
        ) as client:
            try:
                extra: dict = {}
                if code_verifier is not None:
                    extra["code_verifier"] = code_verifier

                token = await client.fetch_token(
                    token_endpoint,
                    grant_type="authorization_code",
                    code=code,
                    **extra,
                )
            except Exception as exc:
                raise OIDCTokenError(f"Token exchange failed: {exc}") from exc

        return dict(token)

    # ── ID-token validation ───────────────────────────────────────────────────

    async def validate_id_token(
        self,
        id_token: str,
        jwks_uri: str,
        issuer: str,
        nonce: str,
    ) -> dict:
        """Validate *id_token* and return its claims.

        Checks: signature (JWK), expiry, ``iss``, ``aud``, ``nonce``.
        Raises ``OIDCTokenError`` on any validation failure.
        """
        from authlib.jose import JsonWebKey, jwt as jose_jwt
        from authlib.jose.errors import JoseError

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(jwks_uri)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OIDCTokenError(f"Could not fetch JWKS from {jwks_uri}: {exc}") from exc
        try:
            jwks = JsonWebKey.import_key_set(resp.json())
        except Exception as exc:
            # Covers json.JSONDecodeError (non-JSON response) and authlib parse errors
            raise OIDCTokenError(f"Could not parse JWKS from {jwks_uri}: {exc}") from exc

        try:
            claims = jose_jwt.decode(
                id_token,
                jwks,
                claims_options={
                    "iss": {"essential": True, "value": issuer},
                    "aud": {"essential": True, "value": self._cfg.client_id},
                    "exp": {"essential": True},
                },
            )
            claims.validate()
        except JoseError as exc:
            raise OIDCTokenError(f"ID token validation failed: {exc}") from exc

        # Validate nonce separately (not in authlib's claims_options by default)
        token_nonce = claims.get("nonce")
        if not token_nonce:
            raise OIDCTokenError("ID token missing nonce claim")
        if not secrets.compare_digest(token_nonce, nonce):
            raise OIDCTokenError("ID token nonce mismatch — possible replay attack")

        return dict(claims)

    # ── email extraction ──────────────────────────────────────────────────────

    @staticmethod
    def extract_email(claims: dict) -> str:
        """Return the ``email`` claim from validated *claims*.

        Raises ``OIDCTokenError`` if the claim is absent or the provider
        explicitly reports the email as unverified.
        """
        email = claims.get("email")
        if not email or not isinstance(email, str):
            raise OIDCTokenError(
                "ID token does not contain an email claim. "
                "Ensure 'email' is in OAUTH_SCOPES and the provider returns it."
            )
        # Reject explicitly-unverified emails (True / absent both pass)
        if claims.get("email_verified") is False:
            raise OIDCTokenError(
                "Email address is not verified by the provider."
            )
        return email.strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# ExternalAuthService
# ─────────────────────────────────────────────────────────────────────────────

class ExternalAuthService:
    """
    Top-level facade for the external OAuth / OIDC flow.

    Usage::

        svc = ExternalAuthService(OAuthConfig.from_env())

        # 1. Start flow
        auth_url, state, nonce, cv = await svc.initiate_flow()

        # 2. After provider redirects back…
        email = await svc.complete_flow(code, state, nonce, code_verifier=cv)

        # 3. Issue Lenny session cookie for email (same as OTP path)
        from lenny.core.auth import create_session_cookie
        cookie = create_session_cookie(email)
    """

    def __init__(self, config: OAuthConfig) -> None:
        self._cfg = config
        self._provider = OIDCProvider(config)

    # ── guard ─────────────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self._cfg.enabled

    def assert_enabled(self) -> None:
        if not self._cfg.enabled:
            raise RuntimeError("External auth is not enabled on this instance.")
        if not self._cfg.is_configured():
            raise RuntimeError(
                "External auth is enabled but not fully configured. "
                "Set OAUTH_CLIENT_ID, OAUTH_DISCOVERY_URL, and OAUTH_REDIRECT_URI."
            )

    # ── flow: initiate ────────────────────────────────────────────────────────

    async def initiate_flow(
        self,
        prompt: Optional[str] = None,
    ) -> tuple[str, str, str, str]:
        """Return ``(auth_url, state, nonce, code_verifier)`` to start the PKCE flow."""
        self.assert_enabled()

        state = secrets.token_hex(32)
        nonce = secrets.token_hex(32)
        code_verifier = PKCEHelper.generate_verifier()

        doc = await self._provider.discover()
        auth_endpoint = doc.get("authorization_endpoint")
        if not auth_endpoint:
            raise OIDCDiscoveryError("Discovery document missing 'authorization_endpoint'")

        auth_url = self._provider.authorization_url(
            auth_endpoint, state, nonce, code_verifier=code_verifier, prompt=prompt
        )
        return auth_url, state, nonce, code_verifier

    # ── flow: complete ────────────────────────────────────────────────────────

    async def complete_flow(
        self,
        code: str,
        nonce: str,
        code_verifier: Optional[str] = None,
    ) -> str:
        """Exchange *code* for an ID token, validate it, and return the patron email.

        Callers are responsible for validating the ``state`` parameter against
        their stored value *before* calling this method (CSRF guard).

        Raises ``OIDCTokenError`` or ``OIDCDiscoveryError`` on any failure —
        callers should return a generic 401 to avoid information leakage.
        """
        self.assert_enabled()

        doc = await self._provider.discover()
        token_endpoint = doc.get("token_endpoint")
        if not token_endpoint:
            raise OIDCDiscoveryError("Discovery document missing 'token_endpoint'")

        jwks_uri = doc.get("jwks_uri")
        issuer = doc.get("issuer")
        if not jwks_uri or not issuer:
            raise OIDCDiscoveryError(
                "Discovery document missing 'jwks_uri' or 'issuer'"
            )

        # Exchange authorization code for tokens
        token_response = await self._provider.exchange_code(
            token_endpoint, code, code_verifier=code_verifier
        )

        id_token = token_response.get("id_token")
        if not id_token:
            raise OIDCTokenError("Token response does not contain an id_token")

        # Validate signature, expiry, iss, aud, nonce
        claims = await self._provider.validate_id_token(
            id_token, jwks_uri, issuer, nonce
        )

        return OIDCProvider.extract_email(claims)

    # ── config persistence ────────────────────────────────────────────────────

    @staticmethod
    def toggle(enabled: bool, auth_env_path: str = "/app/auth.env") -> None:
        """Atomically rewrite ``LENNY_EXTERNAL_AUTH_ENABLED`` in *auth_env_path*.

        Uses the same tmp-file + os.replace() pattern as
        ``lenny.core.ol_bootstrap.update_env_file`` — never leaves a
        half-written file.
        """
        from lenny.core.ol_bootstrap import update_env_file
        update_env_file(
            auth_env_path,
            {"LENNY_EXTERNAL_AUTH_ENABLED": str(enabled).lower()},
        )

    @staticmethod
    def save_config(updates: dict[str, str], auth_env_path: str = "/app/auth.env") -> None:
        """Persist arbitrary *updates* to *auth_env_path*.

        Keys must be valid auth.env variable names.  Values are written raw.
        """
        from lenny.core.ol_bootstrap import update_env_file
        update_env_file(auth_env_path, updates)

    @staticmethod
    def apply_in_process(updates: dict[str, str]) -> None:
        """Reflect *updates* into the running ``lenny.configs`` module so the
        current worker picks up new config without a container restart."""
        from lenny import configs

        mapping = {
            "LENNY_EXTERNAL_AUTH_ENABLED": (
                "EXTERNAL_AUTH_ENABLED",
                lambda v: v.lower() == "true",
            ),
            "OAUTH_CLIENT_ID":    ("OAUTH_CLIENT_ID",    lambda v: v or None),
            "OAUTH_CLIENT_SECRET": ("OAUTH_CLIENT_SECRET", lambda v: v or None),
            "OAUTH_DISCOVERY_URL": ("OAUTH_DISCOVERY_URL", lambda v: v or None),
            "OAUTH_REDIRECT_URI": ("OAUTH_REDIRECT_URI", lambda v: v or None),
            "OAUTH_SCOPES":       ("OAUTH_SCOPES",       lambda v: v.split() or ["openid", "email", "profile"]),
            "OAUTH_FLOW":         ("OAUTH_FLOW",         lambda v: v),
        }
        for env_key, value in updates.items():
            if env_key in mapping:
                attr, coerce = mapping[env_key]
                setattr(configs, attr, coerce(value))
        # Invalidate discovery cache so next request re-fetches
        _DISCOVERY_CACHE.clear()
        _DISCOVERY_CACHE_TS.clear()
