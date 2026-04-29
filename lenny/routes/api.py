#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import json
import httpx
from functools import wraps
from typing import Optional, Generator, List
from urllib.parse import urlencode
from fastapi import (
    APIRouter,
    Request,
    UploadFile,
    File,
    Form,
    HTTPException,
    status,
    Body,
    Cookie
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    JSONResponse,
)
from lenny.core import auth
from lenny.core.api import LennyAPI
from lenny.core import ol_bootstrap
from lenny.core.cache import Cache
from lenny.core.openlibrary import ol_auth_status
from lenny import configs
from pyopds2_lenny import LennyDataProvider, build_post_borrow_publication, LennyDataRecord
from lenny.core.exceptions import (
    INVALID_ITEM,
    InvalidFileError,
    ItemExistsError,
    ItemNotFoundError,
    LoanNotRequiredError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
    BookUnavailableError,
)
from lenny.schemas.ol import OLLoginRequest
from lenny.core.readium import ReadiumAPI
from lenny.core.models import Item
from urllib.parse import quote
COOKIES_MAX_AGE = 604800  # 1 week

def extract_session(request: Request, session: Optional[str] = None) -> Optional[str]:
    """Extract session from cookie or Bearer token."""
    if session:
        return session
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0], parts[1].strip()
    if scheme != "Bearer" or not token:
        return None
    return token


def get_authenticated_email(
    request: Optional[Request] = None,
    session: Optional[str] = None
) -> Optional[str]:
    """Verify session (optionally IP-bound) and extract email. Returns None if unauthenticated."""
    if request is not None and not session:
        session = extract_session(request, session)
    if not session:
        return None
    client_ip: Optional[str] = None
    if request is not None and getattr(request, "client", None) is not None:
        client_ip = request.client.host
    email_data = auth.verify_session_cookie(session, client_ip=client_ip)
    if not email_data:
        return None
    return email_data.get("email") if isinstance(email_data, dict) else email_data


def is_direct_auth_mode(auth_mode: Optional[str] = None, beta: bool = False) -> bool:
    """Determine if direct auth mode (OTP) is enabled vs OAuth."""
    return (auth_mode == "direct") or beta or configs.AUTH_MODE_DIRECT


# All IP-based rate limiting is handled by nginx (limit_req zones).
# OTP email-based rate limiting remains in auth.py via Cache.is_throttled.
router = APIRouter()

def requires_item_auth(do_function=None):
    """
    Decorator checks item existence and gets email of
    authenticated patron and passes them to the wrapped function
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(
                request: Request, book_id: str, format: str = "epub",
                session: Optional[str] = Cookie(None),
                email=None, item=None, *args, **kwargs):
            session = extract_session(request, session)

            if item := Item.exists(book_id):
                result = LennyAPI.auth_check(item, session=session, request=request)
                email = result.get('email', '')
                if 'error' in result:
                    return JSONResponse(
                        status_code=401, 
                        content=LennyDataProvider.get_authentication_document(),
                        media_type="application/opds-authentication+json"
                    )
 
                return await func(
                    request=request, book_id=book_id, format=format, session=session,
                    email=email, item=item, *args, **kwargs
                )            
            return JSONResponse(status_code=401, content={"detail": "Invalid item"})    
        return wrapper
    return decorator

@router.get('/', status_code=status.HTTP_200_OK)
async def home(request: Request):
    kwargs = {"request": request}
    return request.app.templates.TemplateResponse("index.html", kwargs)

@router.get('/health', status_code=status.HTTP_200_OK)
async def health():
    return {"status": "ok"}

@router.get("/items")
async def get_items(fields: Optional[str]=None, offset: Optional[int]=None, limit: Optional[int]=None, encrypted: Optional[bool]=None):
    fields = fields.split(",") if fields else None
    return LennyAPI.get_enriched_items(
        fields=fields, offset=offset, limit=limit, encrypted=encrypted
    )

@router.get("/opds")
async def get_opds_catalog(request: Request, offset: Optional[int]=None, limit: Optional[int]=None, beta: bool = False, auth_mode: Optional[str] = None, session: Optional[str] = Cookie(None)):
    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    
    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(offset=offset, limit=limit, auth_mode_direct=is_direct_auth_mode(auth_mode, beta), email=email)
        ),
        media_type="application/opds+json"
    )

@router.get("/opds/search")
async def opds_search(request: Request, query: Optional[str] = "", auth_mode: Optional[str] = None, beta: bool = False):
    """
    OPDS 2.0 search endpoint. Public — no authentication required.
    """
    return Response(
        content=json.dumps(
            LennyAPI.search_feed(
                query=query,
                auth_mode_direct=is_direct_auth_mode(auth_mode, beta),
            )
        ),
        media_type="application/opds+json"
    )

@router.api_route("/opds/{book_id}",  methods=["GET", "POST"])
async def get_opds_item(request: Request, book_id: int, session: Optional[str] = Cookie(None), beta: bool = False, auth_mode: Optional[str] = None):
    """
    Returns OPDS publication info. If authenticated, also processes borrow
    link generation (showing read/return options).
    """
    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    
    item = Item.exists(book_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(olid=book_id, auth_mode_direct=is_direct_auth_mode(auth_mode, beta), email=email)
        ),
        media_type="application/opds-publication+json"
    )


@router.get("/items/{book_id}/read")
@requires_item_auth()
async def redirect_reader(request: Request, book_id: str, format: str = "epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    encoded_manifest_uri = quote(manifest_uri, safe='')
    reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
    return RedirectResponse(url=reader_url, status_code=307)

@router.get("/items/{book_id}/readium/manifest.json")
@requires_item_auth()
async def get_manifest(request: Request, book_id: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    return ReadiumAPI.get_manifest(book_id, format)

@router.get("/items/{book_id}/readium/{readium_path:path}")
@requires_item_auth()
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
    with httpx.Client() as client:
        r = client.get(readium_url, params=dict(request.query_params))
        if readium_url.endswith('.json'):
            return r.json()
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        return Response(content=r.content, media_type=content_type)


@router.api_route('/items/{book_id}/borrow', methods=["GET", "POST"])
async def borrow_item(request: Request, response: Response, book_id: int, format: str=".epub", session: Optional[str] = Cookie(None), beta: bool = False, auth_mode: Optional[str] = None):
    """
    Unified Borrow Endpoint.
    
    Decides between standard OPDS 401 response (OAuth mode) or interactive OTP flow (Direct mode)
    based on configuration and authentication state.
    """
    _require_lending()
    is_direct_mode = is_direct_auth_mode(auth_mode, beta)

    if not (item := Item.exists(book_id)):
         raise HTTPException(status_code=404, detail="Item not found")

    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    
    if email:
        try:
            loan = item.borrow(email)
        except LoanNotRequiredError:
            pass
        except BookUnavailableError:
             raise HTTPException(status_code=409, detail="No copies available for borrowing")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        
        if is_direct_mode:
            return RedirectResponse(
                url=f"/v1/api/items/{book_id}/read", 
                status_code=303
            )

        return Response(
            content=json.dumps(build_post_borrow_publication(book_id, auth_mode_direct=is_direct_mode)),
            media_type="application/opds-publication+json"
        )
    
    if not is_direct_mode:
          return JSONResponse(
                status_code=401, 
                content=LennyDataProvider.get_authentication_document(),
                media_type="application/opds-authentication+json"
          )
    
    client_ip = request.client.host
    body = await LennyAPI.parse_request_body(request)
    req_params = dict(request.query_params)
    
    post_email = body.get("email")
    post_otp = body.get("otp")
    post_url = f"/v1/api/items/{book_id}/borrow"
    if beta:
        post_url += "?beta=true"
    
    context = {
        "request": request,
        "redirect_uri": post_url,
        "state": "direct",
        "client_id": "direct",
        "post_url": post_url,
        "next": post_url,
        "book_id": book_id,
        "action": "borrow",
        "auth_mode": "direct"
    }

    if request.method == "POST":
        if post_email and post_otp:
            session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
            if not session_cookie:
                context["error"] = "Authentication failed. Invalid OTP."
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)
            
            response = RedirectResponse(url=post_url, status_code=302)
            response.set_cookie(
                key="session", value=session_cookie, max_age=auth.COOKIE_TTL,
                httponly=True, secure=True, samesite="Lax", path="/"
            )
            return response

        if post_email:
            try:
                auth.OTP.issue(post_email, client_ip)
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)
            except Exception as e:
                context["error"] = f"Failed to issue OTP: {str(e)}"
                return request.app.templates.TemplateResponse("otp_issue.html", context)
    
    return request.app.templates.TemplateResponse("otp_issue.html", context)

@router.api_route('/items/{book_id}/return', methods=['GET', 'POST'], status_code=status.HTTP_200_OK)
@requires_item_auth()
async def return_item(request: Request, book_id: int, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str='', beta: bool = False, auth_mode: Optional[str] = None):
    """
    Return a borrowed book.
    
    After successful return, returns OPDS publication with borrow link
    (book is now available to borrow again).
    """
    is_direct_mode = is_direct_auth_mode(auth_mode, beta)

    try:
        loan = item.unborrow(email)
        
        if is_direct_mode:
             redirect_url = f"/v1/api/opds/{book_id}"
             if beta or auth_mode == "direct":
                 redirect_url += "?auth_mode=direct"
             return RedirectResponse(url=redirect_url, status_code=303)

        return Response(
            content=json.dumps(LennyAPI.opds_feed(olid=book_id, auth_mode_direct=is_direct_mode)),
            media_type="application/opds-publication+json"
        )
    except LoanNotRequiredError:
        return Response(
            content=json.dumps({"error": "open_access", "message": "This book is open access and doesn't require return"}),
            media_type="application/json"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post('/upload', status_code=status.HTTP_200_OK)
async def upload(
    request: Request,
    openlibrary_edition: int = Form(
        ..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(
        False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(
        ..., description="The PDF or EPUB file to upload (max 50MB)")
):

    try:
        item = LennyAPI.add(
            openlibrary_edition=openlibrary_edition,
            files=[file],  # TODO expand to allow multiple
            uploader_ip=request.client.host,
            encrypt=encrypted,
        )
        return HTMLResponse(
            status_code=status.HTTP_200_OK,
            content="File uploaded successfully."
        )
    except UploaderNotAllowedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ItemExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseInsertError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except FileTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except S3UploadError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@router.get("/profile")
async def profile(request: Request, session: Optional[str] = Cookie(None)):
    """
    Returns the OPDS 2.0 User Profile.
    """
    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    
    if not email:
        return JSONResponse(
            status_code=401,
            content=LennyDataProvider.get_authentication_document(),
            media_type="application/opds-authentication+json"
        )
    
    name = email.split("@")[0]
    profile_data = LennyAPI.get_user_profile(email, name)

    return JSONResponse(
        profile_data, 
        media_type="application/json" if "text/html" in request.headers.get("accept", "") else "application/opds-profile+json"
    )


@router.get("/shelf")
async def get_shelf(request: Request, session: Optional[str] = Cookie(None), auth_mode: Optional[str] = None):
    """
    Returns the user's bookshelf as an OPDS 2.0 Feed.
    Contains all currently borrowed items with return/read links.
    """
    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    
    if not email:
        return JSONResponse(
            status_code=401,
            content=LennyDataProvider.get_authentication_document(),
            media_type="application/opds-authentication+json"
        )
    
    shelf_feed = LennyAPI.get_shelf_feed(email, auth_mode_direct=is_direct_auth_mode(auth_mode))
    
    return Response(
        content=json.dumps(shelf_feed),
        media_type="application/opds+json"
    )


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(response: Response, session: str = Cookie(None)):
    response.delete_cookie(
        key="session",
        path="/",
        secure=True,
        samesite="Lax"
    )
    return {"success": True, "message": "Logged out successfully"}

@router.get("/oauth/implicit")
async def oauth_implicit(request: Request):
    """
    Returns the OPDS Authentication Document (JSON) describing the implicit flow.
    """
    return Response(
        content=json.dumps(LennyDataProvider.get_authentication_document()),
        media_type="application/opds-authentication+json"
    )

@router.api_route("/oauth/authorize", methods=["GET", "POST"])
async def oauth_authorize(
    request: Request, 
    response: Response,
    redirect_uri: Optional[str] = None,
    client_id: Optional[str] = None,
    state: Optional[str] = None
):
    """
    Handles the authorization request.
    If logged in, redirects to redirect_uri with access_token in fragment.
    If not logged in, handles OTP flow directly.
    """
    _require_lending()
    session = request.cookies.get("session")
    email = get_authenticated_email(request, session)

    if email:
        body = await LennyAPI.parse_request_body(request)
        redirect_uri = redirect_uri or body.get("redirect_uri") or "opds://authorize/"
        state = state or body.get("state")
        
        fragment = LennyAPI.build_oauth_fragment(session, state)
        return RedirectResponse(url=f"{redirect_uri}?{urlencode(fragment)}", status_code=303)

    client_ip = request.client.host
    body = await LennyAPI.parse_request_body(request)
    req_params = dict(request.query_params)
    
    post_email = body.get("email")
    post_otp = body.get("otp")
    
    current_redirect_uri = body.get("redirect_uri") or req_params.get("redirect_uri") or "opds://authorize/"
    current_state = body.get("state") or req_params.get("state")
    current_client_id = body.get("client_id") or req_params.get("client_id")

    post_url = "/v1/api/oauth/authorize"
    if current_redirect_uri != "opds://authorize/":
        post_url += f"?redirect_uri={quote(current_redirect_uri, safe='')}"
    if current_state:
        post_url += f"&state={quote(current_state, safe='')}"
    
    context = {
        "request": request,
        "redirect_uri": current_redirect_uri,
        "state": current_state,
        "client_id": current_client_id,
        "post_url": post_url,
        "next": current_redirect_uri,
        "book_id": "oauth",
        "action": "oauth"
    }

    if request.method == "POST" and post_email and post_otp:
        session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
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
                "state": current_state
            }
            response = request.app.templates.TemplateResponse("oauth_success.html", success_context)
        else:
            response = RedirectResponse(
                url=f"{current_redirect_uri}?{urlencode(fragment)}",
                status_code=303
            )
        
        response.set_cookie(
            key="session",
            value=session_cookie,
            max_age=auth.COOKIE_TTL,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/"
        )
        return response

    if request.method == "POST" and post_email:
        try:
            auth.OTP.issue(post_email, client_ip)
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_redeem.html", context)
        except Exception:
            context["error"] = "Failed to issue OTP. Please try again."
            return request.app.templates.TemplateResponse("otp_issue.html", context)

    return request.app.templates.TemplateResponse("otp_issue.html", context)

@router.post("/admin/auth", status_code=status.HTTP_200_OK)
async def admin_auth(request: Request, body: dict = Body(...)):
    """
    Validates the admin key and internal secret, returns a signed token.
    Called server-side from lenny-app; never exposed through nginx.
    """
    internal_secret = request.headers.get("X-Admin-Internal-Secret", "")
    if not auth.verify_admin_internal_secret(internal_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    username = body.get("username", "")
    password = body.get("password", "")
    token = auth.authenticate_admin(username, password)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid admin key")

    return JSONResponse({"token": token})


@router.get("/admin/verify", status_code=status.HTTP_200_OK)
async def admin_verify(request: Request):
    """
    Verifies a signed admin token passed as a Bearer token.
    Called server-side from lenny-app middleware; never exposed through nginx.
    """
    internal_secret = request.headers.get("X-Admin-Internal-Secret", "")
    if not auth.verify_admin_internal_secret(internal_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not auth.verify_admin_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return JSONResponse({"valid": True})


# ─── Open Library / Internet Archive auth bootstrap ──────────────────────
# These routes let the admin UI log Lenny into archive.org and persist the
# returned IA S3 keys to .env. They mirror `docker/utils/ol_configure.sh` so
# an operator can log in either from the UI or from a shell.
#
# Every /admin/ol/* route requires BOTH X-Admin-Internal-Secret (server-side
# shared secret — proxied by lenny-app, never reachable through nginx) AND a
# valid admin Bearer token (proof the admin user is signed in). This matches
# the /admin/auth + /admin/verify pair already exposed on this router.

OL_ENV_PATH = "/app/.env"
OL_LOGIN_RATE_LIMIT = 5
OL_LOGIN_RATE_WINDOW = 300


def _require_lending() -> None:
    """Raise 503 if lending is disabled or OL credentials are not configured."""
    if not configs.LENDING_ENABLED:
        raise HTTPException(status_code=503, detail="Lending is not enabled on this instance.")
    if not (configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY):
        raise HTTPException(status_code=503, detail="Lending is not configured: Open Library credentials are missing. Run 'make ol-login'.")


def _require_admin(request: Request) -> None:
    """Enforce the internal-secret + admin-token pair used by every /admin/ol/* route."""
    internal_secret = request.headers.get("X-Admin-Internal-Secret", "")
    if not auth.verify_admin_internal_secret(internal_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not auth.verify_admin_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _apply_ol_env_in_process(
    access: Optional[str],
    secret: Optional[str],
    username: Optional[str],
    lending_enabled: Optional[bool] = None,
) -> None:
    """Update lenny.configs so the running worker uses new credentials
    without a container restart. `ol_auth_headers()` reads these at call-time."""
    configs.OL_S3_ACCESS_KEY = access or None
    configs.OL_S3_SECRET_KEY = secret or None
    configs.OL_USERNAME = username or None
    if lending_enabled is not None:
        configs.LENDING_ENABLED = lending_enabled


@router.get("/admin/ol/status", status_code=status.HTTP_200_OK)
async def admin_ol_status(request: Request):
    """Current Lenny ↔ OL auth state. Used by the admin UI to render the
    "Logged in as …" banner and decide whether to show the login form."""
    _require_admin(request)
    return JSONResponse(ol_auth_status())


@router.post("/admin/ol/login", status_code=status.HTTP_200_OK)
async def admin_ol_login(request: Request, body: OLLoginRequest = Body(...)):
    """Exchange IA email/password for S3 keys and persist them to .env.

    Rate-limited by (client IP, email) to 5 attempts / 5 minutes. Refuses
    to overwrite an existing login unless `replace=true` is sent — matches
    the shell `ol-login` re-login confirmation flow.
    """
    _require_admin(request)

    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{client_ip}:{body.email.lower()}"
    if Cache.is_throttled(
        "ol:login", throttle_key, OL_LOGIN_RATE_LIMIT, OL_LOGIN_RATE_WINDOW
    ):
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "message": "Too many attempts. Try again in a few minutes.",
            },
        )

    if configs.OL_S3_ACCESS_KEY and configs.OL_USERNAME and not body.replace:
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_logged_in",
                "message": (
                    f"Already logged in as {configs.OL_USERNAME}. "
                    "Send replace=true to overwrite these credentials."
                ),
                "username": configs.OL_USERNAME,
            },
        )

    try:
        access, secret, screenname = ol_bootstrap.acquire_keys(body.email, body.password)
    except ol_bootstrap.OLBootstrapError as err:
        mapping = {
            "INVALID_CREDENTIALS": (401, "invalid_credentials", "Email or password is incorrect."),
            "BAD_EMAIL":           (400, "bad_email",            "Email must be a valid address."),
            "BAD_PASSWORD":        (400, "bad_password",         "Password must not be empty."),
            "IA_UNREACHABLE":      (502, "ia_unreachable",       "Could not reach archive.org. Check network."),
            "NO_KEYS":             (500, "no_keys",              "archive.org did not return S3 keys for this account."),
            "MISSING_DEP":         (500, "missing_dep",          "Server is missing the 'internetarchive' package. Run 'make redeploy'."),
        }
        status_code, code, message = mapping.get(
            err.code, (500, "unknown", "Login failed. Please try again.")
        )
        return JSONResponse(status_code=status_code, content={"error": code, "message": message})

    try:
        ol_bootstrap.update_env_file(
            OL_ENV_PATH,
            {
                "OL_S3_ACCESS_KEY": access,
                "OL_S3_SECRET_KEY": secret,
                "OL_USERNAME": body.email,
                "LENNY_LENDING_ENABLED": "true",
            },
        )
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "env_write_failed",
                "message": f"Authenticated but could not persist credentials: {exc}",
            },
        )

    _apply_ol_env_in_process(access, secret, body.email, lending_enabled=True)

    return JSONResponse(
        {
            "logged_in": True,
            "username": body.email,
            "screenname": screenname,
            "lending_enabled": True,
            "message": f"Logged in as {screenname or body.email}.",
        }
    )


@router.post("/admin/ol/logout", status_code=status.HTTP_200_OK)
async def admin_ol_logout(request: Request):
    """Clear the IA S3 keys from .env (and from the running process).

    Leaves `LENNY_LENDING_ENABLED` alone — that's an operator-intent toggle
    set separately. Callers wanting to fully disable lending should follow
    up with a config change.
    """
    _require_admin(request)

    previous_user = configs.OL_USERNAME

    try:
        ol_bootstrap.update_env_file(
            OL_ENV_PATH,
            {
                "OL_S3_ACCESS_KEY": "",
                "OL_S3_SECRET_KEY": "",
                "OL_USERNAME": "",
            },
        )
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "env_write_failed",
                "message": f"Could not clear credentials from .env: {exc}",
            },
        )

    _apply_ol_env_in_process(None, None, None)

    return JSONResponse(
        {
            "logged_in": False,
            "previous_username": previous_user,
            "message": (
                f"Logged out of {previous_user}." if previous_user
                else "No credentials were configured."
            ),
        }
    )