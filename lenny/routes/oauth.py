
import logging
from typing import Optional
from urllib.parse import urlencode
from fastapi import APIRouter, Request, Form, HTTPException, Response
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

from lenny.core.limiter import limiter
from lenny.core import auth
from lenny.core.oauth import OAuthService
from lenny.routes.api import extract_session, get_authenticated_email

logger = logging.getLogger(__name__)
router = APIRouter()

from pyopds2_lenny import LennyDataProvider


def _get_client_ip(request: Request) -> str:
    """Extract client IP from proxy headers, falling back to direct connection."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def _build_oauth_context(request: Request, **oauth_params) -> dict:
    """Build the template context dict with all OAuth params preserved."""
    return {
        "request": request,
        "redirect_uri": oauth_params["redirect_uri"],
        "client_id": oauth_params["client_id"],
        "state": oauth_params["state"],
        "code_challenge": oauth_params["code_challenge"],
        "code_challenge_method": oauth_params["code_challenge_method"],
        "scope": oauth_params["scope"],
        "post_url": oauth_params.get("post_url", f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path),
    }


def _complete_authorization(
    request: Request,
    email: str,
    session_cookie: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
) -> Response:
    """
    Generate auth code, build the redirect/success response, and set cookies.
    This is the single place that handles code creation + redirect for both
    the already-authenticated and the freshly-OTP-authenticated paths.
    """
    code = OAuthService.create_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        email=email,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope,
    )

    if redirect_uri.startswith("opds://"):
        resp = request.app.templates.TemplateResponse(
            "oauth_success.html",
            {"request": request, "email": email, "code": code, "state": state},
        )
    else:
        params = urlencode({"code": code, "state": state})
        separator = "&" if "?" in redirect_uri else "?"
        resp = RedirectResponse(
            url=f"{redirect_uri}{separator}{params}", status_code=302
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



@router.get("/implicit")
async def oauth_implicit(request: Request):
    """Returns the OPDS Authentication Document (JSON)."""
    doc = LennyDataProvider.get_authentication_document()
    return JSONResponse(content=doc, media_type="application/opds-authentication+json")


@router.api_route("/authorize", methods=["GET", "POST"])
async def oauth_authorize(
    request: Request,
    response: Response,
    redirect_uri: Optional[str] = None,
    client_id: Optional[str] = None,
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: str = 'S256',
    scope: str = "openid"
):
    """OAuth 2.0 Authorize Endpoint with PKCE.
    
    Requires client_id, redirect_uri, state, and code_challenge.
    Returns a friendly error page listing missing params if any are absent.
    The OPDS Authentication Document is served at /implicit.
    """
    # 0. Check required params — show friendly error if missing
    missing = []
    if not client_id:
        missing.append("client_id")
    if not redirect_uri:
        missing.append("redirect_uri")
    if not state:
        missing.append("state")
    if not code_challenge:
        missing.append("code_challenge")
    
    if missing:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "error_description": f"Missing required parameters: {', '.join(missing)}",
                "missing_parameters": missing,
            }
        )

    # 1. Validate Client & Redirect URI
    if not OAuthService.validate_client(client_id, redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid client_id or redirect_uri")

    # 2. Enforce PKCE (S256) + reject fragments (RFC 6749 §3.1.2)
    if code_challenge_method != 'S256':
        raise HTTPException(status_code=400, detail="PKCE required. code_challenge_method must be S256")
    if '#' in redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri must not contain a fragment")

    # Shared OAuth params passed to helpers
    oauth_params = dict(
        client_id=client_id, redirect_uri=redirect_uri, state=state,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        scope=scope,
    )

    # 3. Already authenticated → complete immediately
    # Note: `state` is passed through but NOT validated server-side.
    # Per OAuth 2.0 spec (RFC 6749 §10.12), the CLIENT validates state.
    session = extract_session(request, request.cookies.get("session"))
    email = get_authenticated_email(request, session)

    if email:
        session_cookie = auth.create_session_cookie(email, ip=_get_client_ip(request))
        return _complete_authorization(request, email, session_cookie, **oauth_params)

    # 4. Not authenticated → handle OTP flow
    if request.method == "POST":
        form = await request.form()
        post_email = form.get("email")
        post_otp = form.get("otp")
        client_ip = _get_client_ip(request)

        context = _build_oauth_context(
            request,
            post_url=f"{request.url.path}?{urlencode(request.query_params)}",
            **oauth_params,
        )

        if post_email and post_otp:
            # OTP redemption
            session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
            if not session_cookie:
                context["error"] = "Authentication failed. Invalid OTP."
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)

            return _complete_authorization(request, post_email, session_cookie, **oauth_params)

        elif post_email:
            # OTP issuance
            try:
                auth.OTP.issue(post_email, client_ip)
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)
            except Exception:
                logger.exception("Failed to issue OTP during OAuth flow")
                context["error"] = "Failed to issue OTP. Please try again later."
                return request.app.templates.TemplateResponse("otp_issue.html", context)

    # GET → show login form
    context = _build_oauth_context(request, **oauth_params)
    return request.app.templates.TemplateResponse("otp_issue.html", context)


@router.post("/token")
@limiter.limit("5/minute")
async def oauth_token(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: str = Form(...),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    """OAuth 2.0 Token Endpoint. Supports authorization_code and refresh_token grants."""
    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "code, redirect_uri, and code_verifier are required"}
            )
        try:
            return OAuthService.exchange_code(
                client_id=client_id,
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
        except ValueError as e:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": str(e)}
            )

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "refresh_token is required"}
            )
        try:
            return OAuthService.refresh_access_token(
                client_id=client_id,
                refresh_token=refresh_token,
            )
        except ValueError as e:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": str(e)}
            )

    else:
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_grant_type"}
        )


