#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

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
from lenny.core.api import LennyAPI
from lenny.core.readium import ReadiumAPI
from lenny.core.exceptions import (
    ItemExistsError,
    InvalidFileError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
)

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
    if not LennyAPI.auth_check(book_id):
        HTTPException(status_code=400, detail="Unauthorized request")
    readium_url = ReadiumAPI.make_url(book_id, format, "manifest.json")
    manifest = requests.get(readium_url).json()
    return ReadiumAPI.patch_manifest(manifest, book_id)

# Proxy all other readium requests
@router.get("/items/{book_id}/{readium_path:path}")
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub"):
    if not LennyAPI.auth_check(book_id):
        HTTPException(status_code=400, detail="Unauthorized request")
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
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
async def upload(
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
            encrypt=encrypted,
            files=[file]  # TODO expand to allow multiple 
        )
        return HTMLResponse(
            status_code=status.HTTP_200_OK,
            content="File uploaded successfully."
        )
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

