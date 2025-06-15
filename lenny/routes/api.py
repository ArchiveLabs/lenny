#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from pathlib import Path
import requests
from typing import Optional, Generator
from fastapi import (
    APIRouter,
    Request,
    UploadFile,
    File,
    Form,
    HTTPException,
    status
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response
)
from lenny.core.itemsUpload import upload_items
from lenny.core.api import LennyAPI
from lenny.models import db
from lenny.models.items import Item

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
    
@router.get("/items/{book_id}/manifest.json")
async def get_manifest(book_id: str, format: str=".epub"):
    # TODO: permission/auth checks go here, or decorate this route
    readium_url = LennyAPI.make_readium_url(book_id, format, "manifest.json")
    manifest = requests.get(readium_url).json()
    return LennyAPI.patch_readium_manifest(manifest, book_id)

# Proxy all other readium requests
@router.get("/items/{book_id}/{readium_path:path}")
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub"):
    # TODO: permission/auth checks go here, or decorate this route
    readium_url = LennyAPI.make_readium_url(book_id, format, readium_path)
    r = requests.get(readium_url, params=dict(request.query_params))
    if readium_url.endswith('.json'):
        return r.json()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=content_type)

# Redirect to the Thorium Web Reader
@router.get("/read/{book_id}")
async def redirect_reader(book_id: str, format: str = "epub"):
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    reader_url = LennyAPI.make_reader_url(manifest_uri)
    return RedirectResponse(url=reader_url, status_code=307)

@router.post('/upload', status_code=status.HTTP_200_OK)
async def create_items(
    openlibrary_edition: int = Form(..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(..., description="The PDF or EPUB file to upload (max 50MB)")
    ):
    allowed_extensions = {".pdf", ".epub"}
    allowed_mime_types = {"application/pdf", "application/epub+zip"}

    if file.size is not None and file.size > LennyAPI.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File '{file.filename}' is too large. Maximum size is {LennyAPI.MAX_FILE_SIZE // (1024 * 1024)}MB."
        )

    existing_item = db.query(Item).filter(Item.openlibrary_edition == openlibrary_edition).first()
    if existing_item:
        db.close()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An item with OpenLibrary Edition ID '{openlibrary_edition}' already exists."
        )

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A file with no name was uploaded.")

    file_extension = Path(file.filename).suffix.lower()
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File '{file.filename}' has an invalid extension. Only PDF and EPUB files are allowed."
        )

    if file.content_type not in allowed_mime_types:
        if not (file_extension == ".epub" and file.content_type == "application/octet-stream"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file.filename}' has an invalid MIME type. Only PDF (application/pdf) and EPUB (application/epub+zip) files are allowed."
            )

    try:
        upload_items(
            openlibrary_edition=openlibrary_edition,
            encrypted=encrypted,
            files=[file]
        )
        return HTMLResponse(status_code=status.HTTP_200_OK, content="File uploaded successfully.")
    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An unexpected error occurred during upload: {str(e)}")
    finally:
        db.close()
