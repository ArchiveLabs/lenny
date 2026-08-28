#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import json
import logging
import os
import re
import httpx
from functools import wraps
from typing import Optional, Generator, List
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_MAX_LIMIT  = 1000
_MAX_OFFSET = 100_000
from fastapi import (
    APIRouter,
    Request,
    UploadFile,
    File,
    Form,
    HTTPException,
    status,
    Body,
    Cookie,
    Query,
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
from lenny.core.patron_auth import AuthModeManager as _AuthModeManager
from lenny.core.exceptions import (
    INVALID_ITEM,
    InvalidFileError,
    ItemExistsError,
    ItemNotFoundError,
    LoanNotRequiredError,
    DatabaseInsertError,
    DatabaseDeleteError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
    BookUnavailableError,
    PatronLoanLimitError,
    LendingNotConfiguredError,
    LoanNotFoundError,
    OTPGenerationError,
    RateLimitError,
)
from lenny.schemas.ol import OLLoginRequest
from lenny.core.readium import ReadiumAPI
from lenny.core.models import Item
from lenny.core.utils import parse_modified_since
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
    if scheme.lower() != "bearer" or not token:
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
        client_ip = request.client.host if request.client else "unknown"
    email_data = auth.verify_session_cookie(session, client_ip=client_ip)
    if not email_data:
        return None
    return email_data.get("email") if isinstance(email_data, dict) else email_data


def is_direct_auth_mode(auth_mode: Optional[str] = None, beta: bool = False) -> bool:
    """Determine if direct auth mode (OTP) is enabled vs OAuth."""
    return (auth_mode == "direct") or beta or configs.AUTH_MODE_DIRECT


def _wants_html(request: Request) -> bool:
    """True when the caller is a browser (not a native OPDS client).

    Used to decide between OPDS JSON responses (auth doc, publication) and
    user-friendly redirects (auth flow, reader). OPDS clients send specific
    ``application/opds*`` Accept values; browsers send ``text/html``.
    """
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _external_auth_ready() -> bool:
    """True only when external OIDC is enabled AND fully configured (client_id + discovery_url + redirect_uri set).

    Used to route patrons to OIDC vs OTP. A flag-only check (EXTERNAL_AUTH_ENABLED)
    is insufficient — a half-configured provider sends the user to a broken auth page.
    """
    return _AuthModeManager().is_external_ready()


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
            return JSONResponse(status_code=404, content={"detail": "Item not found"})
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
async def get_items(fields: Optional[str]=None, offset: Optional[int]=None, limit: Optional[int]=None, encrypted: Optional[bool]=None, modified_since: Optional[str]=None):
    fields = fields.split(",") if fields else None
    if limit  is not None: limit  = max(0, min(limit,  _MAX_LIMIT))
    if offset is not None: offset = max(0, min(offset, _MAX_OFFSET))
    try:
        modified_since_dt = parse_modified_since(modified_since)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid `modified_since`: expected an ISO 8601 date or timestamp",
        )
    return LennyAPI.get_enriched_items(
        fields=fields, offset=offset, limit=limit, encrypted=encrypted,
        modified_since=modified_since_dt,
    )

@router.get("/opds")
async def get_opds_catalog(request: Request, offset: Optional[int]=None, limit: Optional[int]=None, beta: bool = False, auth_mode: Optional[str] = None, modified_since: Optional[str] = None, session: Optional[str] = Cookie(None)):
    session = extract_session(request, session)
    email = get_authenticated_email(request, session)
    if limit  is not None: limit  = max(0, min(limit,  _MAX_LIMIT))
    if offset is not None: offset = max(0, min(offset, _MAX_OFFSET))

    # Parse before the try/except below, so a malformed filter is a 400 the
    # caller can fix rather than a 503 that looks like Lenny is down — and so it
    # never degrades into silently serving the entire catalogue unfiltered.
    try:
        modified_since_dt = parse_modified_since(modified_since)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid `modified_since`: expected an ISO 8601 date or timestamp, "
                "e.g. 2026-08-01 or 2026-08-01T12:30:00Z"
            ),
        )

    try:
        feed = LennyAPI.opds_feed(offset=offset, limit=limit, auth_mode_direct=is_direct_auth_mode(auth_mode, beta), email=email, modified_since=modified_since_dt)
    except Exception as e:
        logger.exception("OPDS feed error")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    return Response(
        content=json.dumps(feed),
        media_type="application/opds+json"
    )

@router.get("/opds/search")
async def opds_search(request: Request, query: Optional[str] = "", limit: Optional[int] = None, auth_mode: Optional[str] = None, beta: bool = False):
    """
    OPDS 2.0 search endpoint. Public — no authentication required.
    """
    if limit is not None:
        limit = max(0, min(limit, _MAX_LIMIT))
    sf_kwargs = {"query": query, "auth_mode_direct": is_direct_auth_mode(auth_mode, beta)}
    if limit is not None:
        sf_kwargs["limit"] = limit
    return Response(
        content=json.dumps(LennyAPI.search_feed(**sf_kwargs)),
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

    Decides between standard OPDS 401 response (OAuth mode) or interactive
    OTP flow (Direct mode) based on configuration and authentication state.

    Lending credentials (OL S3 keys) are NOT required at this gate — they are
    only needed for the OTP direct-mode path (which calls the OL API to issue
    codes) and will surface naturally from item.borrow() for encrypted items.
    External OAuth users can reach this endpoint without lending being enabled.
    """
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
        except PatronLoanLimitError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception:
            logger.exception("Unexpected error in borrow_item book_id=%s", book_id)
            raise HTTPException(status_code=500, detail="Internal server error")

        if is_direct_mode:
            return RedirectResponse(
                url=f"/v1/api/items/{book_id}/read",
                status_code=303
            )

        # OPDS OAuth mode + browser → send the user to the reader instead of
        # returning the publication JSON (which a browser can't act on).
        # Native OPDS clients (Accept: application/opds+json) still get JSON.
        if _wants_html(request):
            return RedirectResponse(
                url=f"/v1/api/items/{book_id}/read",
                status_code=303,
            )

        return Response(
            content=json.dumps(build_post_borrow_publication(book_id, auth_mode_direct=is_direct_mode)),
            media_type="application/opds-publication+json"
        )

    if not is_direct_mode:
        # Browser callers can't usefully consume a JSON 401 + auth doc — send
        # them through the right auth flow with the borrow URL as the post-login
        # target so the borrow can complete and they land on the reader.
        if _wants_html(request):
            borrow_url = f"/v1/api/items/{book_id}/borrow"
            if beta:
                borrow_url += "?beta=true"
            if _external_auth_ready():
                # External OIDC fully configured — go directly, skip the OTP hop.
                return RedirectResponse(
                    url=f"/v1/api/oauth/external/start?opds_redirect_uri={quote(borrow_url, safe='')}",
                    status_code=303,
                )
            # Fall back to OTP flow (or prompt if lending not configured).
            return RedirectResponse(
                url=f"/v1/api/oauth/authorize?redirect_uri={quote(borrow_url, safe='')}",
                status_code=303,
            )
        return JSONResponse(
            status_code=401,
            content=LennyDataProvider.get_authentication_document(),
            media_type="application/opds-authentication+json"
        )

    # Direct mode (OTP form) — this path calls the OL API to issue/verify codes,
    # so OL credentials are genuinely required here.
    _require_lending()

    client_ip = request.client.host if request.client else "unknown"
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
            try:
                session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
            except LendingNotConfiguredError as e:
                context["error"] = str(e)
                return request.app.templates.TemplateResponse("otp_issue.html", context)
            except RateLimitError as e:
                context["error"] = str(e)
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)
            except OTPGenerationError as e:
                # Open Library refused for a reason that is not "wrong password"
                # — stale credentials, rate limiting, an outage. Say which,
                # instead of blaming the patron's typing.
                context["error"] = str(e)
                context["email"] = post_email
                return request.app.templates.TemplateResponse("otp_redeem.html", context)
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
            # Only advance to the "enter your code" screen if Open Library
            # actually said it issued one. It answers HTTP 200 with a JSON error
            # body on every failure path, so this used to promise the patron an
            # email that was never sent, with nothing logged on either side.
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
    except LoanNotFoundError:
        raise HTTPException(status_code=404, detail="No active loan found for this item")
    except Exception:
        logger.exception("Unexpected error in return_item book_id=%s", book_id)
        raise HTTPException(status_code=500, detail="Internal server error")


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
            uploader_ip=request.client.host if request.client else "unknown",
            encrypt=encrypted,
        )
        return HTMLResponse(
            status_code=status.HTTP_200_OK,
            content="File uploaded successfully."
        )
    except UploaderNotAllowedError:
        raise HTTPException(status_code=403, detail="Upload not permitted from this host.")
    except ItemExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseInsertError as e:
        logger.exception("Upload DB insert error")
        raise HTTPException(status_code=500, detail="Internal server error")
    except FileTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except S3UploadError as e:
        logger.exception("Upload S3 error")
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        logger.exception("Unexpected upload error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/items")
async def admin_get_items(
    request: Request,
    fields: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    encrypted: Optional[bool] = None,
):
    """
    Admin-scoped catalog listing used by the lenny-app admin UI.
    Same payload as the public /items endpoint, but gated behind admin auth
    so the UI can present management actions (delete, edit) without exposing
    a separate data shape. Kept as a thin wrapper so the underlying enrichment
    logic stays in one place.
    """
    _require_admin(request)
    fields = fields.split(",") if fields else None
    if limit is not None:
        limit = max(0, min(limit, _MAX_LIMIT))
    if offset is not None:
        offset = max(0, min(offset, _MAX_OFFSET))
    return LennyAPI.get_enriched_items(
        fields=fields, offset=offset, limit=limit, encrypted=encrypted
    )


@router.delete("/admin/items/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(request: Request, book_id: int):
    """
    Delete an item from the catalog (S3 files + DB record, loans cascade).
    Requires admin authentication.
    """
    _require_admin(request)
    try:
        LennyAPI.delete(book_id)
    except ItemNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    except DatabaseDeleteError as e:
        logger.exception("Delete DB error")
        raise HTTPException(status_code=500, detail="Internal server error")


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

# oauth_implicit and oauth_authorize have moved to lenny/routes/oauth.py

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
# returned IA S3 keys to ol.env. They mirror `docker/utils/ol_configure.sh` so
# an operator can log in either from the UI or from a shell.
#
# Every /admin/ol/* route requires BOTH X-Admin-Internal-Secret (server-side
# shared secret — proxied by lenny-app, never reachable through nginx) AND a
# valid admin Bearer token (proof the admin user is signed in). This matches
# the /admin/auth + /admin/verify pair already exposed on this router.

LENNY_ENV_PATH = "/app/.env"
OL_ENV_PATH = "/app/ol.env"
AUTH_ENV_PATH = "/app/auth.env"
LOAN_ENV_PATH = "/app/loan.env"
OL_LOGIN_RATE_LIMIT = 5
OL_LOGIN_RATE_WINDOW = 300

# Coarse lock serializing every write that touches the lending invariant
# (LENNY_LENDING_MODE in ol.env + LENNY_EXTERNAL_AUTH_ENABLED in auth.env) or OL
# credentials. Held across the *whole* multi-file mutation so concurrent workers
# can never interleave the two writes and break
# EXTERNAL_AUTH_ENABLED == (mode == "external"). Reentrant (see env_lock), so
# nested reconcilers sharing it don't self-deadlock.
_LENDING_STATE_LOCK = os.path.join(os.path.dirname(OL_ENV_PATH) or ".", ".lending_state.lock")

_VALID_LENDING_MODES = {"none", "ol", "external"}


def _require_lending() -> None:
    """Raise 503/501 if the active lending mode cannot serve a borrow request."""
    _AuthModeManager().require_patron_login_available()


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
    lending_mode: Optional[str] = None,
) -> None:
    """Update lenny.configs so the running worker uses new credentials
    without a container restart. `ol_auth_headers()` reads these at call-time."""
    configs.OL_S3_ACCESS_KEY = access or None
    configs.OL_S3_SECRET_KEY = secret or None
    configs.OL_USERNAME = username or None
    if lending_mode is not None:
        configs.LENDING_MODE = lending_mode
        configs.LENDING_ENABLED = lending_mode != "none"


def _external_configured() -> bool:
    """True when external OIDC has the minimum fields set (regardless of the
    enabled flag). Read from auth.env so all workers agree."""
    from lenny.core.external_auth import OAuthConfig
    return OAuthConfig.from_auth_env(AUTH_ENV_PATH).is_configured()


def _apply_lending_state(mode: str) -> None:
    """Single source of truth for switching the active lending mode.

    Enforces the invariant ``EXTERNAL_AUTH_ENABLED == (mode == "external")`` by
    persisting BOTH ``LENNY_LENDING_MODE`` (ol.env) and
    ``LENNY_EXTERNAL_AUTH_ENABLED`` (auth.env) atomically, then mirroring the
    change into this worker's in-process config so it takes effect immediately.

    Readiness is enforced here so an unusable mode can never become active:
      - ``ol``       requires OL S3 credentials,
      - ``external`` requires a fully-configured OIDC provider.

    Raises HTTPException(422) when the requested mode is not ready, or
    HTTPException(500) when an env file cannot be written.
    """
    if mode == "ol" and not (configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY):
        raise HTTPException(
            status_code=422,
            detail="Cannot enable OL lending: credentials not configured. Run 'make ol-login' first.",
        )
    if mode == "external" and not _external_configured():
        raise HTTPException(
            status_code=422,
            detail="Cannot enable external lending: OAuth provider not fully configured "
                   "(client_id, discovery_url, and redirect_uri are required).",
        )

    external_enabled = mode == "external"
    # Two separate files must move together to keep the invariant
    # EXTERNAL_AUTH_ENABLED == (mode == "external"). Snapshot the old mode so a
    # failure on the second write can roll back the first — otherwise a partial
    # write leaves the instance half-switched (e.g. mode=ol on disk but the
    # external-auth flag still true), which silently misroutes /oauth/authorize.
    # The whole read-decide-write runs under the coarse lock so concurrent
    # workers can't interleave the two writes (or race the snapshot).
    with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
        prev_mode = configs.read_lending_mode()
        try:
            ol_bootstrap.update_env_file(OL_ENV_PATH, {"LENNY_LENDING_MODE": mode})
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not persist lending mode: {exc}",
            )
        try:
            ol_bootstrap.update_env_file(
                AUTH_ENV_PATH,
                {"LENNY_EXTERNAL_AUTH_ENABLED": str(external_enabled).lower()},
            )
        except OSError as exc:
            # Best-effort rollback of the first write so disk never holds a
            # half-switch. Plain env files can't do true 2-phase commit, so if
            # the rollback itself fails we log loudly rather than silently.
            try:
                ol_bootstrap.update_env_file(OL_ENV_PATH, {"LENNY_LENDING_MODE": prev_mode})
            except OSError:
                logger.error(
                    "Rollback of LENNY_LENDING_MODE failed: ol.env=%r but auth.env "
                    "write failed — instance may be in an inconsistent lending state.",
                    mode,
                )
            raise HTTPException(
                status_code=500,
                detail=f"Could not persist external-auth flag: {exc}",
            )

        configs.LENDING_MODE = mode
        configs.LENDING_ENABLED = mode != "none"
        configs.EXTERNAL_AUTH_ENABLED = external_enabled


def _apply_external_enabled(enabled: bool) -> None:
    """Toggle external auth and keep the lending mode in lockstep.

    Enabling (requires the provider to be configured) promotes the active mode
    to ``external``; disabling demotes ``external`` back to ``none`` while
    leaving an ``ol`` mode untouched. Delegates to :func:`_apply_lending_state`
    so the same invariant and persistence path are used everywhere.
    """
    # Coarse lock makes the read-decide-write atomic vs. other workers; it is
    # reentrant, so the nested _apply_lending_state calls re-acquire it safely.
    with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
        if enabled:
            if not _external_configured():
                raise HTTPException(
                    status_code=422,
                    detail="Cannot enable external auth: OAuth provider not fully configured "
                           "(client_id, discovery_url, and redirect_uri are required).",
                )
            _apply_lending_state("external")
            return

        # Disabling: drop out of external mode if that's where we are; otherwise
        # the only effect is clearing the flag (an ``ol`` or ``none`` mode is
        # preserved).
        if configs.read_lending_mode() == "external":
            _apply_lending_state("none")
        else:
            try:
                ol_bootstrap.update_env_file(
                    AUTH_ENV_PATH, {"LENNY_EXTERNAL_AUTH_ENABLED": "false"}
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Could not persist auth.env: {exc}",
                )
            configs.EXTERNAL_AUTH_ENABLED = False


@router.get("/admin/ol/status", status_code=status.HTTP_200_OK)
async def admin_ol_status(request: Request):
    """Current Lenny ↔ OL auth state. Used by the admin UI to render the
    "Logged in as …" banner and decide whether to show the login form."""
    _require_admin(request)
    return JSONResponse(ol_auth_status())


@router.post("/admin/ol/login", status_code=status.HTTP_200_OK)
async def admin_ol_login(request: Request, body: OLLoginRequest = Body(...)):
    """Exchange IA email/password for S3 keys and persist them to ol.env.

    Stores credentials and activates OL lending mode. Rate-limited by
    (client IP, email) to 5 attempts / 5 minutes. Refuses to overwrite an
    existing login unless `replace=true` is sent.
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

    # Hold the coarse lock across the write + in-process apply so this can't
    # interleave with a concurrent lending-mode switch on another worker.
    try:
        with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
            ol_bootstrap.update_env_file(
                OL_ENV_PATH,
                {
                    "OL_S3_ACCESS_KEY": access,
                    "OL_S3_SECRET_KEY": secret,
                    "OL_USERNAME": body.email,
                    "LENNY_LENDING_MODE": "ol",
                },
            )
            _apply_ol_env_in_process(access, secret, body.email, lending_mode="ol")
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "env_write_failed",
                "message": f"Authenticated but could not persist credentials: {exc}",
            },
        )

    return JSONResponse(
        {
            "logged_in": True,
            "username": body.email,
            "screenname": screenname,
            "lending_mode": "ol",
            "message": f"Logged in as {screenname or body.email}.",
        }
    )


@router.post("/admin/ol/logout", status_code=status.HTTP_200_OK)
async def admin_ol_logout(request: Request):
    """Clear the IA S3 keys from ol.env and set lending mode to none."""
    _require_admin(request)

    previous_user = configs.OL_USERNAME

    # Coarse lock so the clear + in-process apply can't interleave with a
    # concurrent lending-mode switch on another worker.
    try:
        with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
            ol_bootstrap.update_env_file(
                OL_ENV_PATH,
                {
                    "OL_S3_ACCESS_KEY": "",
                    "OL_S3_SECRET_KEY": "",
                    "OL_USERNAME": "",
                    "LENNY_LENDING_MODE": "none",
                },
            )
            _apply_ol_env_in_process(None, None, None, lending_mode="none")
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "env_write_failed",
                "message": f"Could not clear credentials from ol.env: {exc}",
            },
        )

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


# ─── Lending mode admin endpoints ────────────────────────────────────────────
# Explicit 3-state control: none | ol | external
# Separate from OL credential management (/admin/ol/*) and patron auth (/admin/auth/*).

@router.get("/admin/lending/mode", status_code=status.HTTP_200_OK)
async def get_lending_mode(request: Request):
    """Return the active lending mode and readiness of each provider.

    Reads the mode fresh from ol.env and the external flag fresh from auth.env so
    the value is consistent across all workers right after an admin change
    (per-worker in-process globals can otherwise be stale)."""
    _require_admin(request)
    from lenny.core.external_auth import OAuthConfig
    cfg = OAuthConfig.from_auth_env(AUTH_ENV_PATH)
    ol_ready = bool(configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY)
    return JSONResponse({
        "mode": configs.read_lending_mode(),
        "ol_ready": ol_ready,
        "external_auth_enabled": cfg.enabled,
        "external_auth_ready": cfg.enabled and cfg.is_configured(),
        "ia_auth_enabled": configs.read_ia_auth_enabled(),
    })


@router.put("/admin/lending/mode", status_code=status.HTTP_200_OK)
async def set_lending_mode(request: Request, body: dict = Body(...)):
    """Switch the active lending mode.

    - ``ol``: lend via Open Library (requires OL credentials to be stored first)
    - ``external``: lend via external OAuth provider (not yet implemented)
    - ``none``: lending disabled

    Only one mode can be active at a time. Setting a mode implicitly deactivates
    the previous one. OL credentials stored via /admin/ol/login are not cleared
    when switching away from ``ol`` mode.

    The mode and the external-auth flag are persisted together (ol.env +
    auth.env) and mirrored into this worker's in-process config so the change is
    visible immediately. Other workers read both files fresh on the next request,
    so no restart is required even with LENNY_WORKERS > 1.

    Switching to ``external`` requires a fully-configured OIDC provider; setting
    any non-external mode disables external auth (the invariant
    ``EXTERNAL_AUTH_ENABLED == (mode == "external")`` is always maintained).
    """
    _require_admin(request)

    if "mode" not in body:
        raise HTTPException(status_code=400, detail="Request body must include 'mode'.")

    mode = str(body.get("mode", "")).lower()
    if mode not in _VALID_LENDING_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(_VALID_LENDING_MODES))}.",
        )

    _apply_lending_state(mode)

    return JSONResponse({"mode": mode})


# ─── Auth-mode alias endpoints ───────────────────────────────────────────────
# The lenny-app admin UI calls /admin/settings/auth-mode (GET + POST) with a
# `lending_mode` field. Backend canonical path is /admin/lending/mode (GET+PUT)
# with a `mode` field. These aliases bridge the naming gap without breaking
# legacy callers of the canonical path. Same auth gate, same persistence path.

@router.get("/admin/settings/auth-mode", status_code=status.HTTP_200_OK)
async def get_auth_mode_settings(request: Request):
    """Alias of GET /admin/lending/mode with lenny-app's expected field names.

    Reads the mode and external flag fresh (ol.env + auth.env) for cross-worker
    consistency."""
    _require_admin(request)
    from lenny.core.external_auth import OAuthConfig
    cfg = OAuthConfig.from_auth_env(AUTH_ENV_PATH)
    ol_ready = bool(configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY)
    return JSONResponse({
        "lending_mode": configs.read_lending_mode(),
        "ol_ready": ol_ready,
        "external_auth_ready": cfg.enabled and cfg.is_configured(),
        "external_auth_enabled": cfg.enabled,
        "ia_auth_enabled": configs.read_ia_auth_enabled(),
    })


@router.post("/admin/settings/auth-mode", status_code=status.HTTP_200_OK)
async def post_auth_mode_settings(request: Request, body: dict = Body(...)):
    """Alias of PUT /admin/lending/mode accepting body {"lending_mode": ...}.

    Delegates to the same reconciler as the canonical endpoint so both paths
    behave identically (no 501-vs-200 divergence for ``external``) and keep the
    external-auth flag in lockstep with the mode."""
    _require_admin(request)

    if "lending_mode" not in body:
        raise HTTPException(status_code=400, detail="Request body must include 'lending_mode'.")

    mode = str(body.get("lending_mode", "")).lower()
    if mode not in _VALID_LENDING_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid lending_mode '{mode}'. Must be one of: {', '.join(sorted(_VALID_LENDING_MODES))}.",
        )

    _apply_lending_state(mode)

    return JSONResponse({"lending_mode": mode})


@router.post("/admin/ia-auth/toggle", status_code=status.HTTP_200_OK)
async def toggle_ia_auth(request: Request, body: dict = Body(...)):
    """Enable or disable IA S3 patron authentication.

    Persists ``IA_AUTH_ENABLED`` to auth.env and updates the in-process flag.
    Independent of lending_mode — patrons can authenticate via IA S3 credentials
    regardless of the active lending mode.

    Body: ``{"enabled": true|false}``
    """
    _require_admin(request)

    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="Request body must include 'enabled'.")

    enabled: bool = bool(body["enabled"])

    try:
        with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
            ol_bootstrap.update_env_file(
                AUTH_ENV_PATH, {"IA_AUTH_ENABLED": "true" if enabled else "false"}
            )
            configs.IA_AUTH_ENABLED = enabled
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist IA_AUTH_ENABLED to auth.env: {exc}",
        )

    return JSONResponse({"ia_auth_enabled": enabled})


# ─── Loan settings endpoints ─────────────────────────────────────────────────

@router.get("/admin/loan/settings", status_code=status.HTTP_200_OK)
async def get_loan_settings(request: Request):
    """Return current loan settings (read fresh from loan.env, not per-worker cache)."""
    _require_admin(request)
    return JSONResponse({
        "loan_limit": configs.get_loan_limit(),
        "loan_duration_days": configs.get_loan_duration_days(),
    })


@router.put("/admin/loan/settings", status_code=status.HTTP_200_OK)
async def put_loan_settings(request: Request):
    """Update loan_limit and/or loan_duration_days.

    Writes to loan.env immediately. loan.env is the cross-worker source of
    truth: reads go through configs.get_loan_*(), which read the file fresh, so
    every worker reflects the change at once. The in-process globals are also
    refreshed as a fallback for the rare case the file is unreadable.
    """
    _require_admin(request)
    body = await request.json()

    updates: dict = {}

    if "loan_limit" in body:
        try:
            val = int(body["loan_limit"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="loan_limit must be an integer")
        if val < 1:
            raise HTTPException(status_code=400, detail="loan_limit must be >= 1")
        updates["LENNY_LOAN_LIMIT"] = str(val)

    if "loan_duration_days" in body:
        try:
            val = int(body["loan_duration_days"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="loan_duration_days must be an integer")
        if val < 0:
            raise HTTPException(status_code=400, detail="loan_duration_days must be >= 0 (0 = never expire)")
        updates["LENNY_LOAN_DURATION_DAYS"] = str(val)

    if not updates:
        raise HTTPException(status_code=400, detail="Provide loan_limit and/or loan_duration_days")

    # Write env file before mutating in-process state — if the write fails,
    # configs.* is unchanged and the response returns the error.
    try:
        ol_bootstrap.update_env_file(LOAN_ENV_PATH, updates)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "env_write_failed", "message": f"Could not update loan.env: {exc}"},
        )

    if "LENNY_LOAN_LIMIT" in updates:
        configs.LOAN_LIMIT = int(updates["LENNY_LOAN_LIMIT"])
    if "LENNY_LOAN_DURATION_DAYS" in updates:
        configs.LOAN_DURATION_DAYS = int(updates["LENNY_LOAN_DURATION_DAYS"])

    return JSONResponse({
        "loan_limit": configs.get_loan_limit(),
        "loan_duration_days": configs.get_loan_duration_days(),
    })


# ─── Loan listing + alias endpoints used by lenny-app admin UI ───────────────
# The admin UI calls /admin/loans (list) and /admin/settings/loan-limits
# (GET + POST). These bridge to the existing loan model and to /admin/loan/settings.

@router.get("/admin/loans", status_code=status.HTTP_200_OK)
async def admin_list_loans(
    request: Request,
    limit: Optional[int] = 500,
    offset: Optional[int] = None,
    loan_status: Optional[str] = Query(None, alias="status"),
    user: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
):
    """Filtered, paginated loans for the admin UI. Logic in core/admin_loans.py.

    Always returns a wrapped object so the response shape is stable regardless
    of which params are supplied::

        {"items": [...], "total": <int>, "limit": <int>, "offset": <int>}

    ``total`` is the count matching the filters (before limit/offset).

    Filters: ``status`` ∈ {all,active,returned,overdue}; ``user`` = hex prefix
    of the patron hash; ``sort`` ∈ {borrowed_at,due_at,returned_at};
    ``order`` ∈ {asc,desc}. All optional; sensible defaults applied.
    """
    _require_admin(request)
    from lenny.core.admin_loans import (
        query_loans_for_admin,
        VALID_STATUSES,
        VALID_SORTS,
    )

    st = (loan_status or "all").lower()
    if st not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{st}'. Must be one of: {', '.join(VALID_STATUSES)}.",
        )
    srt = (sort or "borrowed_at").lower()
    if srt not in VALID_SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort '{srt}'. Must be one of: {', '.join(VALID_SORTS)}.",
        )
    ordr = (order or "desc").lower()
    if ordr not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="Invalid order. Must be 'asc' or 'desc'.")

    usr: Optional[str] = None
    if user is not None:
        usr = user.strip().lower()
        # Hex-only prefix of the SHA-256 hash — also prevents LIKE-wildcard injection.
        if not re.fullmatch(r"[0-9a-f]{1,64}", usr):
            raise HTTPException(
                status_code=400,
                detail="'user' must be a hex prefix of the patron hash (1-64 hex chars).",
            )

    off = offset or 0
    if off < 0:
        raise HTTPException(status_code=400, detail="'offset' must be >= 0.")

    items, total = query_loans_for_admin(
        status=st, user=usr, limit=limit, offset=off, sort=srt, order=ordr
    )
    eff_limit = max(1, min(int(limit or 500), 5000))
    return JSONResponse({"items": items, "total": total, "limit": eff_limit, "offset": off})


@router.get("/admin/settings/loan-limits", status_code=status.HTTP_200_OK)
async def get_loan_limits_settings(request: Request):
    """Alias of GET /admin/loan/settings using lenny-app's field names.

    `max_renewals` and `renewal_duration_days` are not yet implemented in the
    backend — returned as 0 so the UI renders the inputs without errors.
    """
    _require_admin(request)
    return JSONResponse({
        "max_concurrent_loans": configs.get_loan_limit(),
        "max_loan_duration_days": configs.get_loan_duration_days(),
        "max_renewals": 0,
        "renewal_duration_days": 0,
    })


@router.post("/admin/settings/loan-limits", status_code=status.HTTP_200_OK)
async def post_loan_limits_settings(request: Request):
    """Alias of PUT /admin/loan/settings using lenny-app's field names.

    Accepts any subset of {max_concurrent_loans, max_loan_duration_days}.
    Renewal fields are accepted-but-ignored until backend support lands.
    """
    _require_admin(request)
    body = await request.json()

    updates: dict = {}

    if "max_concurrent_loans" in body:
        try:
            val = int(body["max_concurrent_loans"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_concurrent_loans must be an integer")
        if val < 1:
            raise HTTPException(status_code=400, detail="max_concurrent_loans must be >= 1")
        updates["LENNY_LOAN_LIMIT"] = str(val)

    if "max_loan_duration_days" in body:
        try:
            val = int(body["max_loan_duration_days"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_loan_duration_days must be an integer")
        if val < 0:
            raise HTTPException(status_code=400, detail="max_loan_duration_days must be >= 0 (0 = never expire)")
        updates["LENNY_LOAN_DURATION_DAYS"] = str(val)

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="Provide max_concurrent_loans and/or max_loan_duration_days",
        )

    try:
        ol_bootstrap.update_env_file(LOAN_ENV_PATH, updates)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "env_write_failed", "message": f"Could not update loan.env: {exc}"},
        )

    if "LENNY_LOAN_LIMIT" in updates:
        configs.LOAN_LIMIT = int(updates["LENNY_LOAN_LIMIT"])
    if "LENNY_LOAN_DURATION_DAYS" in updates:
        configs.LOAN_DURATION_DAYS = int(updates["LENNY_LOAN_DURATION_DAYS"])

    return JSONResponse({
        "max_concurrent_loans": configs.get_loan_limit(),
        "max_loan_duration_days": configs.get_loan_duration_days(),
        "max_renewals": 0,
        "renewal_duration_days": 0,
    })


# ─── External OAuth / OIDC admin endpoints ───────────────────────────────────
# These let the lenny-app admin UI read and write the external auth config.
# All three require the same internal-secret + bearer-token pair as /admin/ol/*.

_VALID_FLOWS = {"pkce"}


@router.get("/admin/auth/config", status_code=status.HTTP_200_OK)
async def get_auth_config(request: Request):
    """Return the current external OAuth configuration.

    The ``client_secret`` is never returned — the response includes only a
    boolean indicating whether one is set.
    """
    _require_admin(request)
    from lenny.core.external_auth import OAuthConfig
    # Read from auth.env so the value is consistent across workers — see
    # OAuthConfig.from_auth_env. (POST applies in-process to one worker only.)
    cfg = OAuthConfig.from_auth_env(AUTH_ENV_PATH)
    return JSONResponse({
        "enabled":           cfg.enabled,
        "is_configured":     cfg.is_configured(),
        "is_ready":          cfg.enabled and cfg.is_configured(),
        "client_id":         cfg.client_id,
        "client_secret_set": bool(cfg.client_secret),
        "discovery_url":     cfg.discovery_url,
        "redirect_uri":      cfg.redirect_uri,
        "scopes":            cfg.scopes,
        "flow":              cfg.flow,
    })


@router.post("/admin/auth/config", status_code=status.HTTP_200_OK)
async def update_auth_config(request: Request, body: dict = Body(...)):
    """Validate and persist external OAuth configuration to auth.env.

    All fields are optional; only supplied keys are updated.
    The running worker's in-process config is also updated so changes take
    effect immediately without a container restart.
    """
    _require_admin(request)

    from lenny.core.external_auth import ExternalAuthService

    # Validate the enabled flag up front (before any write) so a bad type never
    # leaves a half-applied config. 'enabled' is applied last, via
    # _apply_external_enabled, because it also drives the lending-mode sync.
    has_enabled = "enabled" in body
    if has_enabled and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=400, detail="'enabled' must be a JSON boolean, not a string.")

    updates: dict[str, str] = {}

    if "client_id" in body:
        updates["OAUTH_CLIENT_ID"] = str(body["client_id"])
    if "client_secret" in body:
        updates["OAUTH_CLIENT_SECRET"] = str(body["client_secret"])
    if "discovery_url" in body:
        updates["OAUTH_DISCOVERY_URL"] = str(body["discovery_url"])
    if "redirect_uri" in body:
        updates["OAUTH_REDIRECT_URI"] = str(body["redirect_uri"])
    if "scopes" in body:
        scopes = body["scopes"]
        if isinstance(scopes, list):
            scopes = " ".join(str(s) for s in scopes)
        updates["OAUTH_SCOPES"] = str(scopes)
    if "flow" in body:
        flow = str(body["flow"])
        if flow not in _VALID_FLOWS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid flow '{flow}'. Must be one of: {', '.join(sorted(_VALID_FLOWS))}",
            )
        updates["OAUTH_FLOW"] = flow

    if not updates and not has_enabled:
        raise HTTPException(status_code=400, detail="No recognised fields in request body.")

    # Persist credential fields first so readiness is evaluated against the new
    # values when 'enabled' is reconciled below. The credential write and the
    # enabled/mode reconcile run under one coarse lock (reentrant with the
    # nested _apply_external_enabled) so the two phases can't interleave with a
    # concurrent mode switch on another worker.
    fields = list(updates.keys())
    with ol_bootstrap.env_lock(_LENDING_STATE_LOCK):
        if updates:
            try:
                ExternalAuthService.save_config(updates, AUTH_ENV_PATH)
                ExternalAuthService.apply_in_process(updates)
            except OSError as exc:
                return JSONResponse(
                    status_code=500,
                    content={"error": "env_write_failed", "message": f"Could not persist config: {exc}"},
                )

        if has_enabled:
            # Reconciles the flag AND the lending mode (enable→external when
            # ready, disable→none when currently external). May raise 422 if
            # enabling an unconfigured provider.
            _apply_external_enabled(body["enabled"])
            fields.append("LENNY_EXTERNAL_AUTH_ENABLED")

    return JSONResponse({"updated": True, "fields": fields})


@router.post("/admin/auth/mode", status_code=status.HTTP_200_OK)
async def toggle_auth_mode(request: Request, body: dict = Body(...)):
    """One-field shorthand to enable or disable external auth.

    Equivalent to calling ``POST /admin/auth/config`` with ``{"enabled": …}``.
    Intended for the admin UI toggle switch.
    """
    _require_admin(request)

    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="Request body must include 'enabled' (bool).")

    enabled_raw = body["enabled"]
    if not isinstance(enabled_raw, bool):
        raise HTTPException(status_code=400, detail="'enabled' must be a JSON boolean, not a string.")

    # Reconciles the flag AND the lending mode so the two pages never drift:
    # enabling promotes the mode to external (requires a configured provider, else
    # 422); disabling demotes external back to none.
    _apply_external_enabled(enabled_raw)

    return JSONResponse({"enabled": enabled_raw})