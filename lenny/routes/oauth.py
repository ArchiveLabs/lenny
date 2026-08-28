#!/usr/bin/env python
"""
OAuth / OIDC routes for Lenny.

Contains:
  - Existing OPDS-standard OAuth endpoints (moved from api.py):
      GET/POST /oauth/implicit   — OPDS Authentication Document
      GET/POST /oauth/authorize  — OTP-based authorization

  - New external OIDC provider endpoints:
      GET /oauth/external/start     — initiate PKCE/auth-code flow
      GET /oauth/external/callback  — provider callback; issues Lenny session cookie

Nginx rate-limiting:
  - /oauth/external/* hits the "oauth" zone (10 req/min, burst 5) via the
    regex location block added in lenny.conf — tighter than the general API zone.
  - /oauth/authorize already has its own nginx location with the same zone.
"""

import json
import logging
import os
from typing import Optional
from urllib.parse import quote, urlencode

from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from lenny import configs
from lenny.core import auth
from lenny.core.api import LennyAPI
from lenny.core.exceptions import (
    InvalidOLCredentialsError,
    LendingNotConfiguredError,
    OTPGenerationError,
    RateLimitError,
)
from lenny.core.external_auth import (
    ExternalAuthService,
    OAuthConfig,
    OIDCDiscoveryError,
    OIDCTokenError,
)
from lenny.core.patron_auth import AuthModeManager as _AuthModeManager
from lenny.core.patron_auth import validate_patron_ia_s3
from pyopds2_lenny import LennyDataProvider

logger = logging.getLogger(__name__)

router = APIRouter()

# TTL for the temporary OIDC state cookie (10 minutes — provider must redirect back within this window)
_OIDC_STATE_TTL = 600


def _get_oidc_state_serializer() -> URLSafeTimedSerializer:
    """Signed serializer for the transient PKCE state cookie."""
    return URLSafeTimedSerializer(configs.SEED, salt="oidc-state")


# ─────────────────────────────────────────────────────────────────────────────
# Existing OPDS auth endpoints (moved verbatim from api.py)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_session(request: Request, session: Optional[str] = None) -> Optional[str]:
    if session:
        return session
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _get_authenticated_email(
    request: Optional[Request] = None,
    session: Optional[str] = None,
) -> Optional[str]:
    if request is not None and not session:
        session = _extract_session(request, session)
    if not session:
        return None
    client_ip: Optional[str] = None
    if request is not None and getattr(request, "client", None) is not None:
        client_ip = request.client.host
    email_data = auth.verify_session_cookie(session, client_ip=client_ip)
    if not email_data:
        return None
    return email_data.get("email") if isinstance(email_data, dict) else None


@router.get("/oauth/implicit")
@router.post("/oauth/implicit")
async def oauth_implicit(request: Request) -> Response:
    """Returns the OPDS Authentication Document describing the available flows."""
    return Response(
        content=json.dumps(LennyDataProvider.get_authentication_document()),
        media_type="application/opds-authentication+json",
    )


@router.api_route("/oauth/authorize", methods=["GET", "POST"])
async def oauth_authorize(
    request: Request,
    response: Response,
    redirect_uri: Optional[str] = None,
    client_id: Optional[str] = None,
    state: Optional[str] = None,
) -> Response:
    """
    Handles OTP-based authorization (OPDS Implicit flow).

    GET  → renders OTP-issue form (if not logged in) or redirects with token
    POST → processes email / OTP submission

    If lending (OTP) is disabled but external OAuth is configured, redirects
    the browser to /oauth/external/start so OPDS clients land on the right flow.
    """
    # If external OIDC is fully configured (not just the flag), route there.
    # This handles OPDS clients that hit /oauth/authorize directly from the auth doc.
    # Browser users are routed before reaching here via borrow_item's direct dispatch.
    from lenny.routes.api import _external_auth_ready
    if _external_auth_ready():
        params: dict = {}
        if redirect_uri:
            params["opds_redirect_uri"] = redirect_uri
        if state:
            params["opds_state"] = state
        qs = ("?" + urlencode(params)) if params else ""
        return RedirectResponse(url=f"/v1/api/oauth/external/start{qs}", status_code=302)
    _require_lending()
    session = request.cookies.get("session")
    email = _get_authenticated_email(request, session)

    if email:
        body = await LennyAPI.parse_request_body(request)
        requested_uri = redirect_uri or body.get("redirect_uri") or ""
        safe_uri = _safe_opds_redirect(requested_uri or "opds://authorize/")
        if requested_uri and not safe_uri:
            return _invalid_redirect_uri_response(requested_uri)
        redirect_uri = safe_uri or "/v1/api/opds"
        state = state or body.get("state")
        fragment = LennyAPI.build_oauth_fragment(session, state)
        return RedirectResponse(url=f"{redirect_uri}#{urlencode(fragment)}", status_code=303)

    client_ip = request.client.host if request.client else "unknown"
    body = await LennyAPI.parse_request_body(request)
    req_params = dict(request.query_params)

    post_email = body.get("email")
    post_otp = body.get("otp")

    # Tell "no redirect_uri supplied" apart from "one was supplied and rejected".
    # Collapsing the two silently strands the client: it gets a normal-looking OTP
    # form and, on success, a dead-end page instead of a redirect home.
    requested_redirect_uri = body.get("redirect_uri") or req_params.get("redirect_uri") or ""
    current_redirect_uri = _safe_opds_redirect(requested_redirect_uri or "opds://authorize/")
    if requested_redirect_uri and not current_redirect_uri:
        return _invalid_redirect_uri_response(requested_redirect_uri)
    current_redirect_uri = current_redirect_uri or "opds://authorize/"
    current_state = body.get("state") or req_params.get("state")
    current_client_id = body.get("client_id") or req_params.get("client_id")

    _params: dict = {}
    if current_redirect_uri != "opds://authorize/":
        _params["redirect_uri"] = current_redirect_uri
    if current_state:
        _params["state"] = current_state
    post_url = "/v1/api/oauth/authorize"
    if _params:
        post_url += "?" + urlencode(_params)

    context = {
        "request": request,
        "redirect_uri": current_redirect_uri,
        "state": current_state,
        "client_id": current_client_id,
        "post_url": post_url,
        "next": current_redirect_uri,
        "book_id": "oauth",
        "action": "oauth",
    }

    if request.method == "POST" and post_email and post_otp:
        try:
            session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
        except LendingNotConfiguredError as e:
            context["error"] = str(e)
            return request.app.templates.TemplateResponse("otp_issue.html", context)
        except (OTPGenerationError, RateLimitError) as e:
            # Not a wrong password — Open Library refused for its own reason.
            context["error"] = str(e)
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_redeem.html", context)
        if not session_cookie:
            context["error"] = "Authentication failed. Invalid OTP."
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_redeem.html", context)

        fragment = LennyAPI.build_oauth_fragment(session_cookie, current_state)

        if current_redirect_uri.startswith("opds://"):
            success_context = {
                "request": request,
                "email": post_email,
                "auth_doc_id": LennyAPI.make_url("/v1/api/oauth/implicit"),
                "access_token": session_cookie,
                "expires_in": auth.COOKIE_TTL,
                "state": current_state,
            }
            resp = request.app.templates.TemplateResponse("oauth_success.html", success_context)
        else:
            resp = RedirectResponse(
                url=f"{current_redirect_uri}#{urlencode(fragment)}",
                status_code=303,
            )

        resp.set_cookie(
            key="session",
            value=session_cookie,
            max_age=auth.COOKIE_TTL,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
        return resp

    if request.method == "POST" and post_email:
        # Advance to the "enter your code" screen only once Open Library confirms
        # it issued one — it reports failure as HTTP 200 with a JSON error body.
        try:
            auth.OTP.issue(post_email, client_ip)
        except LendingNotConfiguredError as e:
            context["error"] = str(e)
            return request.app.templates.TemplateResponse("otp_issue.html", context)
        except OTPGenerationError as e:
            context["error"] = str(e)
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_issue.html", context)
        except Exception:
            logger.exception("Unexpected error issuing OTP")
            context["error"] = "Failed to issue OTP. Please try again."
            return request.app.templates.TemplateResponse("otp_issue.html", context)
        context["email"] = post_email
        return request.app.templates.TemplateResponse("otp_redeem.html", context)

    return request.app.templates.TemplateResponse("otp_issue.html", context)


# ─────────────────────────────────────────────────────────────────────────────
# External OIDC provider endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/oauth/external/start")
async def oauth_external_start(
    request: Request,
    redirect_to: Optional[str] = None,
    opds_redirect_uri: Optional[str] = None,
    opds_state: Optional[str] = None,
) -> Response:
    """Initiate the external OIDC flow.

    Generates state, nonce, and (for PKCE) a code verifier; stores them in a
    short-lived signed cookie; then redirects the browser to the provider's
    authorization endpoint.

    The optional ``redirect_to`` query parameter controls where the browser
    lands after a successful login.  It must be a safe relative path; invalid
    values are silently replaced with ``/admin``.

    Returns 503 when external auth is not configured.
    """
    cfg = OAuthConfig.from_env()
    svc = ExternalAuthService(cfg)

    if not svc.is_enabled():
        return JSONResponse(
            status_code=503,
            content={"error": "external_auth_disabled", "message": "External auth is not enabled."},
        )
    if not cfg.is_configured():
        return JSONResponse(
            status_code=503,
            content={"error": "not_configured", "message": "External auth credentials are not configured."},
        )

    # Validate opds_redirect_uri BEFORE contacting the provider — no point paying
    # for a discovery round-trip on a request we are going to refuse anyway.
    # Allow: opds:// (native OPDS client), allowlisted https:// (browser-based OPDS
    # client), and relative /v1/api/ paths (in-Lenny browser flow).
    # Reject: everything else — would be an open redirect leaking the token.
    # Reject loudly: a silent drop here resurfaces much later as a login that
    # simply never comes back, with nothing in the logs to explain it.
    requested_opds_uri = opds_redirect_uri or ""
    safe_opds_uri = _safe_opds_redirect(requested_opds_uri)
    if requested_opds_uri and not safe_opds_uri:
        return _invalid_redirect_uri_response(requested_opds_uri)

    try:
        auth_url, state, nonce, code_verifier = await svc.initiate_flow()
    except (OIDCDiscoveryError, RuntimeError) as exc:
        logger.error("OIDC initiate_flow failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "provider_error", "message": "Could not reach the authentication provider."},
        )

    # Validate and store the post-login redirect target in the state cookie.
    # _safe_redirect rejects absolute URLs and open-redirect attempts.
    # Default to the OPDS feed — that's Lenny's user-facing entry point.
    # Callers (e.g. admin UI) can override by passing ?redirect_to=/admin.
    safe_redirect_to = _safe_redirect(redirect_to or "") or "/v1/api/opds"

    # Store state + nonce + code_verifier + redirect targets in a signed, short-lived cookie.
    # The callback reads this back to validate the round-trip and redirect correctly.
    serializer = _get_oidc_state_serializer()
    state_payload = {
        "s": state,
        "n": nonce,
        "cv": code_verifier,
        "r": safe_redirect_to,
        "opds_uri": safe_opds_uri,
        "opds_state": opds_state or "",
    }
    state_cookie_value = serializer.dumps(state_payload)

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key="_oidc_state",
        value=state_cookie_value,
        max_age=_OIDC_STATE_TTL,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/v1/api/oauth/external",
    )
    return response


@router.get("/oauth/external/callback")
async def oauth_external_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    _oidc_state: Optional[str] = Cookie(None),
) -> Response:
    """Handle the provider's redirect back to Lenny.

    Validates the CSRF state, exchanges the code for an ID token, verifies the
    email, then issues a standard Lenny session cookie.  The temporary OIDC
    state cookie is cleared regardless of outcome.
    """
    # Always clear the temp cookie — even on error paths
    def _clear_state_cookie(resp: Response) -> Response:
        resp.delete_cookie(
            key="_oidc_state",
            path="/v1/api/oauth/external",
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return resp

    # Provider returned an error
    if error:
        logger.warning("OIDC provider returned error=%r: %s", error, error_description)
        resp = JSONResponse(
            status_code=401,
            content={"error": "provider_error", "message": "Authentication was denied or cancelled."},
        )
        return _clear_state_cookie(resp)

    if not code or not state:
        resp = JSONResponse(
            status_code=400,
            content={"error": "missing_params", "message": "Missing code or state parameter."},
        )
        return _clear_state_cookie(resp)

    # Read and validate the OIDC state cookie
    if not _oidc_state:
        return JSONResponse(
            status_code=403,
            content={"error": "missing_state", "message": "OIDC state cookie missing. Start the flow again."},
        )

    try:
        serializer = _get_oidc_state_serializer()
        payload = serializer.loads(_oidc_state, max_age=_OIDC_STATE_TTL)
    except BadSignature:
        resp = JSONResponse(
            status_code=403,
            content={"error": "invalid_state", "message": "State cookie is invalid or expired."},
        )
        return _clear_state_cookie(resp)

    stored_state: str = payload.get("s", "")
    nonce: str = payload.get("n", "")
    code_verifier: Optional[str] = payload.get("cv")
    post_login_redirect: str = payload.get("r") or "/v1/api/opds"
    opds_uri: str = payload.get("opds_uri") or ""
    opds_state_val: str = payload.get("opds_state") or ""

    # Constant-time state comparison to prevent timing attacks
    import hmac as _hmac
    if not stored_state or not _hmac.compare_digest(stored_state, state):
        resp = JSONResponse(
            status_code=403,
            content={"error": "state_mismatch", "message": "State mismatch. Possible CSRF attack."},
        )
        return _clear_state_cookie(resp)

    # Exchange code and validate ID token
    cfg = OAuthConfig.from_env()
    svc = ExternalAuthService(cfg)
    try:
        email = await svc.complete_flow(code, nonce, code_verifier=code_verifier)
    except (OIDCTokenError, OIDCDiscoveryError, RuntimeError) as exc:
        logger.warning("OIDC complete_flow failed: %s", exc)
        resp = JSONResponse(
            status_code=401,
            content={"error": "auth_failed", "message": "Authentication failed. Please try again."},
        )
        return _clear_state_cookie(resp)

    # Issue a standard Lenny session cookie — identical to the OTP path
    client_ip = request.client.host if request.client else "unknown"
    session_cookie = auth.create_session_cookie(email, client_ip)

    def _set_session(resp: Response) -> Response:
        resp.set_cookie(
            key="session",
            value=session_cookie,
            max_age=auth.COOKIE_TTL,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
        return resp

    # If the flow was initiated from /oauth/authorize, complete the appropriate
    # OPDS flow based on what the caller originally passed as redirect_uri:
    #
    #   opds://   → native OPDS client (Thorium desktop, etc.)
    #               Show the success HTML page so the app can read the token
    #               from the page and use it as a Bearer token.
    #
    #   /path     → browser flow (book catalog or nav)
    #               Session cookie is already set — just redirect.
    #               Cases:
    #                 /v1/api/items/{id}/borrow?beta=true  → borrow endpoint → reader
    #                 /v1/api/opds                         → OPDS feed
    #               Do NOT append access_token to the URL; it would be logged
    #               by nginx and is unnecessary since the cookie handles auth.
    if opds_uri:
        if opds_uri.startswith("opds://"):
            success_context = {
                "request": request,
                "email": email,
                "auth_doc_id": LennyAPI.make_url("/v1/api/oauth/implicit"),
                "access_token": session_cookie,
                "expires_in": auth.COOKIE_TTL,
                "state": opds_state_val,
            }
            resp = request.app.templates.TemplateResponse("oauth_success.html", success_context)
            return _clear_state_cookie(_set_session(resp))
        if opds_uri.startswith("https://"):
            # Absolute HTTPS redirect_uri (browser-based OPDS client, e.g. bookreader).
            # Append the token as query params — same pattern as the OTP flow.
            fragment = LennyAPI.build_oauth_fragment(session_cookie, opds_state_val or None)
            final_resp = RedirectResponse(url=f"{opds_uri}#{urlencode(fragment)}", status_code=303)
            return _clear_state_cookie(_set_session(final_resp))
        # Relative /v1/api/ path — in-Lenny browser flow; session cookie is sufficient
        final_resp = RedirectResponse(url=opds_uri, status_code=303)
        return _clear_state_cookie(_set_session(final_resp))

    final_resp = RedirectResponse(url=post_login_redirect, status_code=303)
    return _clear_state_cookie(_set_session(final_resp))


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _invalid_redirect_uri_response(url: str) -> JSONResponse:
    """400 for a redirect_uri the caller supplied that Lenny will not honour.

    The alternative — quietly substituting ``opds://authorize/`` — is what made
    this class of failure invisible: the client saw HTTP 200 and a normal login
    form, the patron authenticated successfully, and then simply never arrived
    back at the app that sent them.
    """
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_redirect_uri",
            "message": (
                "This redirect_uri is not accepted. Use an opds:// URI, a relative "
                "/v1/api/ path, or an https:// URL whose host the operator has added "
                "to LENNY_OPDS_ALLOWED_HOSTS."
            ),
            "redirect_uri": url,
        },
    )


def _safe_redirect(url: str) -> str:
    """Return *url* only if it is a safe relative path; fall back to '/v1/api/opds'.

    Used for browser-side redirects. Rejects absolute URLs (open-redirect risk),
    protocol-relative ``//host`` forms, and backslash variants. The default
    (``/v1/api/opds``) is Lenny's user-facing root — admin UI callers can
    override it by passing an explicit ``redirect_to`` query parameter.
    """
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or url.startswith("//") or "\\" in url:
        return "/v1/api/opds"
    return url or "/v1/api/opds"


def _safe_opds_redirect(url: str) -> str:
    """Validate an OPDS redirect_uri from the caller.

    Allows:
      - ``opds://`` scheme  — native OPDS clients (Thorium, etc.)
      - ``https://`` URLs   — browser-based OPDS clients (bookreader, etc.)
                              The OPDS implicit flow requires absolute redirect_uri;
                              parity with the OTP path which also accepts these.
      - Relative paths under ``/v1/api/`` — in-Lenny browser flows

    Rejects: protocol-relative ``//``, backslash variants, plain ``http://``,
    and relative paths outside ``/v1/api/`` to prevent redirects to ``/admin``.
    Returns an empty string when the value is invalid.

    Every rejection is logged. The allowlist defaults to empty, which blocks
    *every* ``https://`` client, so "my OPDS reader never gets redirected back"
    is a configuration error far more often than a code one — the operator needs
    a log line naming the host they have to add.
    """
    if not url:
        return ""
    if url.startswith("opds://"):
        return url
    if url.startswith("https://"):
        allowed_raw = os.environ.get("LENNY_OPDS_ALLOWED_HOSTS", "")
        allowed = {h.strip() for h in allowed_raw.split(",") if h.strip()}
        parsed_https = urlparse(url)
        if allowed and parsed_https.netloc in allowed:
            return url
        logger.warning(
            "Rejected https redirect_uri: host %r is not in LENNY_OPDS_ALLOWED_HOSTS "
            "(%s). Add that host to the setting to let this OPDS client complete login.",
            parsed_https.netloc,
            "unset — all https:// redirect URIs are blocked" if not allowed
            else f"{len(allowed)} host(s) allowed",
        )
        return ""
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or url.startswith("//") or "\\" in url:
        logger.warning(
            "Rejected redirect_uri %r: only opds://, allowlisted https://, and "
            "relative /v1/api/ paths are permitted.", url,
        )
        return ""
    normalized = os.path.normpath(url)
    if not normalized.startswith("/v1/api/"):
        logger.warning(
            "Rejected relative redirect_uri %r: must resolve to a path under /v1/api/.", url,
        )
        return ""
    return normalized


def _require_lending() -> None:
    _AuthModeManager().require_patron_login_available()


@router.post("/oauth/ia-s3")
async def ia_s3_login(request: Request) -> Response:
    """Exchange IA S3 credentials for a Lenny session cookie.

    Patron sends:  Authorization: LOW <access>:<secret>
    Returns:       200 + session cookie on success
                   401 on invalid/rejected credentials
                   503 when IA service unavailable
    """
    if not _AuthModeManager().is_ia_s3_enabled():
        raise HTTPException(
            status_code=404,
            detail={"error": "ia_auth_disabled",
                    "message": "IA S3 auth is not enabled on this instance."},
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.upper().startswith("LOW "):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_auth_header",
                    "message": "Expected Authorization: LOW <access>:<secret>"},
        )

    remainder = auth_header[4:]  # strip scheme prefix (case-insensitive "LOW ")
    if ":" not in remainder:
        raise HTTPException(
            status_code=400,
            detail={"error": "malformed_auth_header",
                    "message": "Authorization header must be LOW <access>:<secret>"},
        )

    access, secret = remainder.split(":", 1)
    if not access or not secret:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_credentials",
                    "message": "Access key or secret is empty"},
        )

    try:
        email = await validate_patron_ia_s3(access, secret)
    except InvalidOLCredentialsError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_credentials", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.error("IA S3 patron auth error: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "ia_unavailable",
                    "message": "IA auth service temporarily unavailable"},
        ) from exc

    client_ip = request.client.host if request.client else None
    session_val = auth.create_session_cookie(email, client_ip)

    resp = Response(status_code=200)
    resp.set_cookie(
        key="session",
        value=session_val,
        max_age=auth.COOKIE_TTL,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    return resp
