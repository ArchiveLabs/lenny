#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import json
import requests
from functools import wraps
from typing import Optional, Generator, List
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
                # Email and Item will get magically injected by this decorator and
                # passed in to the wrapped function
                email=None, item=None, *args, **kwargs):
            if item := Item.exists(book_id):
                result = LennyAPI.auth_check(item, session=session, request=request)
                email = result.get('email')
                if 'error' in result:
                    return JSONResponse(status_code=401, content=result)

                # NB: Email and Item will be passed into any function decorated by requires_item_auth
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

@router.get("/items")
async def get_items(fields: Optional[str]=None, offset: Optional[int]=None, limit: Optional[int]=None):
    fields = fields.split(",") if fields else None
    return LennyAPI.get_enriched_items(
        fields=fields, offset=offset, limit=limit
    )

@router.get("/opds")
async def get_opds_catalog(request: Request, offset: Optional[int]=None, limit: Optional[int]=None):
    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(offset=offset, limit=limit)
        ),
        media_type="application/opds+json"
    )

@router.get("/opds/{book_id}")
async def get_opds_item(request: Request, book_id:int):
    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(olid=book_id)
        ),
        media_type="application/opds+json"
    )

# Redirect to the Thorium Web Reader
@router.get("/items/{book_id}/read")
@requires_item_auth()
async def redirect_reader(request: Request, book_id: str, format: str = "epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    # URL encode the manifest URI for use as a path parameter
    encoded_manifest_uri = quote(manifest_uri, safe='')
    reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
    return RedirectResponse(url=reader_url, status_code=307)

@router.get("/items/{book_id}/readium/manifest.json")
@requires_item_auth()
async def get_manifest(request: Request, book_id: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    return ReadiumAPI.get_manifest(book_id, format)

# Proxy all other readium requests
@router.get("/items/{book_id}/readium/{readium_path:path}")
@requires_item_auth()
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
    r = requests.get(readium_url, params=dict(request.query_params))
    if readium_url.endswith('.json'):
        return r.json()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=content_type)

@router.post('/items/{book_id}/borrow')
@requires_item_auth()
async def borrow_item(request: Request, book_id: int, format: str=".epub",  session: Optional[str] = Cookie(None), item=None, email: str=''):
    """
    Handles the borrowing of a book for a patron.
    Redirects to the reader after successful borrow, similar to Internet Archive.
    """
    try:
        loan = item.borrow(email)
        # Redirect to the reader after successful borrow
        manifest_uri = LennyAPI.make_manifest_url(book_id)
        encoded_manifest_uri = quote(manifest_uri, safe='')
        reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
        return RedirectResponse(url=reader_url, status_code=303)
    except LoanNotRequiredError as e:
        # For open-access items, redirect to read
        manifest_uri = LennyAPI.make_manifest_url(book_id)
        encoded_manifest_uri = quote(manifest_uri, safe='')
        reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
        return RedirectResponse(url=reader_url, status_code=303)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post('/items/{book_id}/return', status_code=status.HTTP_200_OK)
@requires_item_auth()
async def return_item(request: Request, book_id: int, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    """
    Handles the return process for a single borrowed book. Requires patron's email.
    Calls return_items to mark the loan as returned.
    """
    try:
        loan = item.unborrow(email)
        return JSONResponse(content={
            "success": True,
            "email": email,
            "loan_id": loan.id,
            "item_id": book_id
        })        
    except LoanNotRequiredError as e:
        return JSONResponse({"error": "open_access","message": "open_access"})
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
        raise HTTPException(status_code=503, details=str(e))
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

@router.post("/authenticate")
async def authenticate(request: Request, response: Response):
    client_ip = request.client.host

    body = await request.json()
    email = body.get("email")
    otp = body.get("otp")

    if email and not otp:
        try:
            return JSONResponse(auth.OTP.issue(email, client_ip))
        except:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Failed to issue OTP. Please try again later."
                }
            )
    else:
        try: 
            session_cookie = auth.OTP.authenticate(email, otp, client_ip)
        except:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Failed to verify OTP due to rate limiting. Please try again later."
                }
            )
        if session_cookie:
            response.set_cookie(
                key="session",
                value=session_cookie,
                max_age=auth.COOKIE_TTL,
                httponly=True,   # Prevent JavaScript access
                secure=True,     # Only over HTTPS in production
                samesite="Lax",  # Helps mitigate CSRF
                path="/"
            )
            return {"Authentication successful": "OTP verified.","success": True}
        else:
            return {"Authentication failed": "Invalid OTP.", "success": False}

    
        
    
@router.post('/items/borrowed', status_code=status.HTTP_200_OK)
async def get_borrowed_items(request: Request, session : Optional[str] = Cookie(None)):
    """
    Returns a list of active (not returned) borrowed items for the given patron's email.
    Calls get_borrowed_items to fetch the list.
    """
    email = auth.verify_session_cookie(session, request.client.host)
    if not (email and session):
        return JSONResponse(
            {
                "auth_required": True,
                "message": "Authentication required to view encrypted borrowed items."
            },
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
                    "borrowed_at": str(loan.created_at),
                }
                for loan in loans
            ],
            "count": len(loans)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get('/logout', status_code=status.HTTP_200_OK)
async def logout_page(response: Response):
    """
    Logs out the user and sends a logout confirmation JSON response.
    """
    response.delete_cookie(key="session", path="/")
    return {"success": True, "message": "Logged out successfully."}