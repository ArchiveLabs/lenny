#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import requests
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
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from lenny.core import auth
from lenny.core.api import LennyAPI
from lenny.core.exceptions import (
    ItemExistsError,
    ItemNotFoundError,
    InvalidFileError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
)
from lenny.core.readium import ReadiumAPI
from urllib.parse import quote

router = APIRouter()

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
async def get_opds(request: Request, offset: Optional[int]=None, limit: Optional[int]=None):
    return LennyAPI.opds_feed(offset=offset, limit=limit)

# Redirect to the Thorium Web Reader
@router.get("/items/{book_id}/read")
async def redirect_reader(book_id: str, format: str = "epub"):
    if not LennyAPI.auth_check(book_id):
        raise HTTPException(status_code=400, detail="Unauthorized request")
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    # URL encode the manifest URI for use as a path parameter
    encoded_manifest_uri = quote(manifest_uri, safe='')
    reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
    return RedirectResponse(url=reader_url, status_code=307)

@router.get("/items/{book_id}/readium/manifest.json")
async def get_manifest(book_id: str, format: str=".epub"):
    if not LennyAPI.auth_check(book_id):
        raise HTTPException(status_code=400, detail="Unauthorized request")
    try:
        return ReadiumAPI.get_manifest(book_id, format)
    except ItemNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

# Proxy all other readium requests
@router.get("/items/{book_id}/readium/{readium_path:path}")
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub"):
    if not LennyAPI.auth_check(book_id):
        raise HTTPException(status_code=400, detail="Unauthorized request")
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
    r = requests.get(readium_url, params=dict(request.query_params))
    if readium_url.endswith('.json'):
        return r.json()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=content_type)

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
async def authenticate(response: Response, email: str = Form(...), otp: str = Form(...)):
    if session_cookie := auth.OTP.authenticate(email, otp):
        response.set_cookie(
            key="session",
            value=session_cookie,
            max_age=auth.COOKIE_TTL,
            httponly=True,   # Prevent JavaScript access
            secure=True,     # Only over HTTPS in production
            samesite="Lax",  # Helps mitigate CSRF
            path="/"
        )
        return {"success": True}
    return {"success": False}

@router.post('/items/{book_id}/borrow', status_code=status.HTTP_200_OK)
async def borrow_item(book_id: int, email: str = Body(..., embed=True)):
    """
    Handles the borrow process for a single book . Requires patron's email.
    Calls borrow_redirect to process the borrow and redirect logic.
    """
    try:
        result = LennyAPI.borrow_redirect(book_id, email)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.post('/items/checkout', status_code=status.HTTP_200_OK)
async def checkout_items(openlibrary_editions: List[int] = Body(...), email: str = Body(..., embed=True)):
    """
    Handles the checkout process for multiple books. Requires patron's email and a list of openlibrary_editions.
    Calls checkout_items to process the borrow for all books.
    """
    try:
        loans = LennyAPI.checkout_items(openlibrary_editions, email)
        return {
            "success": True,
            "loan_ids": [loan.id for loan in loans],
            "count": len(loans)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.post('/items/{book_id}/return', status_code=status.HTTP_200_OK)
async def return_item(book_id: int, email: str = Body(..., embed=True)):
    """
    Handles the return process for a single borrowed book. Requires patron's email.
    Calls return_items to mark the loan as returned.
    """
    try:
        loan = LennyAPI.return_items(book_id, email)
        return {"success": True, "loan_id": loan.id, "returned_at": str(loan.returned_at)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.post('/items/borrowed', status_code=status.HTTP_200_OK)
async def get_borrowed_items(email: str = Body(..., embed=True)):
    """
    Returns a list of active (not returned) borrowed items for the given patron's email.
    Calls get_borrowed_items to fetch the list.
    """
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
