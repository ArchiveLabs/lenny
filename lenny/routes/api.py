#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.
"""

from lenny.app import limiter

import requests
from functools import wraps
from typing import Optional, List
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
)
from lenny.core.readium import ReadiumAPI
from lenny.core.models import Item
from urllib.parse import quote

COOKIES_MAX_AGE = 604800  # 1 week

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
            if item := Item.exists(book_id):
                result = LennyAPI.auth_check(item, session=session, request=request)
                email = result.get('email')
                if 'error' in result:
                    return JSONResponse(status_code=401, content=result)

                return await func(
                    request=request, book_id=book_id, format=format,
                    session=session, email=email, item=item, *args, **kwargs
                )            
            return JSONResponse(status_code=401, content={"detail": "Invalid item"})    
        return wrapper
    return decorator


# -------------------------------------------
# PUBLIC ROUTES (no rate limiting)
# -------------------------------------------

@router.get('/', status_code=status.HTTP_200_OK)
async def home(request: Request):
    kwargs = {"request": request}
    return request.app.templates.TemplateResponse("index.html", kwargs)


@router.get("/items")
async def get_items(fields: Optional[str] = None, offset: Optional[int] = None, limit: Optional[int] = None):
    fields = fields.split(",") if fields else None
    return LennyAPI.get_enriched_items(fields=fields, offset=offset, limit=limit)


@router.get("/opds")
async def get_opds(request: Request, offset: Optional[int] = None, limit: Optional[int] = None):
    return LennyAPI.opds_feed(offset=offset, limit=limit)


# -------------------------------------------
# READER ROUTES
# -------------------------------------------

@router.get("/items/{book_id}/read")
@requires_item_auth()
async def redirect_reader(request: Request, book_id: str, format: str = "epub", session: Optional[str] = Cookie(None), item=None, email: str = ''):
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    encoded_manifest_uri = quote(manifest_uri, safe='')
    reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
    return RedirectResponse(url=reader_url, status_code=307)


@router.get("/items/{book_id}/readium/manifest.json")
@requires_item_auth()
async def get_manifest(request: Request, book_id: str, format: str = ".epub", session: Optional[str] = Cookie(None), item=None, email: str = ''):
    return ReadiumAPI.get_manifest(book_id, format)


@router.get("/items/{book_id}/readium/{readium_path:path}")
@requires_item_auth()
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str = ".epub", session: Optional[str] = Cookie(None), item=None, email: str = ''):
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
    r = requests.get(readium_url, params=dict(request.query_params))
    if readium_url.endswith('.json'):
        return r.json()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=content_type)


# -------------------------------------------
# BORROW / RETURN (rate-limited)
# -------------------------------------------

@router.post('/items/{book_id}/borrow', status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
@requires_item_auth()
async def borrow_item(request: Request, book_id: int, format: str = ".epub", session: Optional[str] = Cookie(None), item=None, email: str = ''):
    try:
        loan = item.borrow(email)
        return JSONResponse(status_code=200, content={
            "success": True,
            "email": email,
            "loan_id": loan.id,
            "item_id": book_id
        })
    except LoanNotRequiredError:
        return JSONResponse({"error": "open_access", "message": "open_access"})


@router.post('/items/{book_id}/return', status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
@requires_item_auth()
async def return_item(request: Request, book_id: int, format: str = ".epub", session: Optional[str] = Cookie(None), item=None, email: str = ''):
    try:
        loan = item.unborrow(email)
        return {
            "success": True,
            "email": email,
            "loan_id": loan.id,
            "item_id": book_id
        }
    except LoanNotRequiredError:
        return JSONResponse({"error": "open_access", "message": "open_access"})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# -------------------------------------------
# UPLOAD (rate-limited)
# -------------------------------------------

@router.post('/upload', status_code=status.HTTP_200_OK)
@limiter.limit("3/minute")
async def upload(
    request: Request,
    openlibrary_edition: int = Form(..., gt=0, description="OpenLibrary Edition ID"),
    encrypted: bool = Form(False),
    file: UploadFile = File(..., description="PDF or EPUB file (max 50MB)")
):
    try:
        item = LennyAPI.add(
            openlibrary_edition=openlibrary_edition,
            files=[file],
            uploader_ip=request.client.host,
            encrypt=encrypted
        )
        return HTMLResponse(status_code=200, content="File uploaded successfully.")
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


# -------------------------------------------
# AUTHENTICATION (highly rate-limited)
# -------------------------------------------

@router.post("/authenticate")
@limiter.limit("5/minute")
async def authenticate(request: Request, response: Response):
    client_ip = request.client.host
    body = await request.json()
    email = body.get("email")
    otp = body.get("otp")

    if email and not otp:
        try:
            return JSONResponse(auth.OTP.issue(email, client_ip))
        except:
            return JSONResponse({
                "success": False,
                "error": "Failed to issue OTP. Please try again later."
            })

    else:
        try:
            session_cookie = auth.OTP.authenticate(email, otp, client_ip)
        except:
            return JSONResponse({
                "success": False,
                "error": "Failed to verify OTP due to rate limiting. Please try again later."
            })

        if session_cookie:
            response.set_cookie(
                key="session",
                value=session_cookie,
                max_age=auth.COOKIE_TTL,
                httponly=True,
                secure=True,
                samesite="Lax",
                path="/"
            )
            return {"Authentication successful": "OTP verified.", "success": True}
        else:
            return {"Authentication failed": "Invalid OTP.", "success": False}


# -------------------------------------------
# BORROWED ITEMS LIST (rate-limited)
# -------------------------------------------

@router.post('/items/borrowed', status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
async def get_borrowed_items(request: Request, session: Optional[str] = Cookie(None)):
    email = auth.verify_session_cookie(session, request.client.host)
    if not (email and session):
        return JSONResponse(
            {"auth_required": True, "message": "Authentication required."},
            status_code=401
        )

    try:
        loans = LennyAPI.get_borrowed_items(email)
        return {
            "success": True,
            "loans": [
                {
                    "loan_id": loan.id,
                    "openlibrary_edition": getattr(loan, "openlibrary_edition", None),
                    "borrowed_at": str(loan.created_at)
                }
                for loan in loans
            ],
            "count": len(loans)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# -------------------------------------------
# LOGOUT (no rate limit)
# -------------------------------------------

@router.get('/logout', status_code=status.HTTP_200_OK)
async def logout_page(response: Response):
    response.delete_cookie(key="session", path="/")
    return {"success": True, "message": "Logged out successfully."}
