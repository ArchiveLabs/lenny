#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import requests
from fastapi import (
    APIRouter,
    Request,
    UploadFile,
    File,
    Form,
    HTTPException,
    status
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pathlib import Path
from lenny.core.itemsUpload import upload_items
from lenny.core.utils import encode_book_path
from lenny.models import db
from lenny.models.items import Item
from lenny.configs import PORT

router = APIRouter()

MAX_FILE_SIZE = 50 * 1024 * 1024

def get_lenny_uri(request: Request, port=True):
    host = f"{request.url.scheme}://{request.url.hostname}"
    if port and PORT and PORT not in {80, 443}:
        host += f":{PORT}"
    return host

@router.get('/', status_code=status.HTTP_200_OK)
async def home(request: Request):
    return request.app.templates.TemplateResponse("index.html", {"request": request})

@router.get("/items")
async def get_items(request: Request):
    from lenny.core import s3
    return list(s3.get_bookshelf_keys())
    
@router.get("/items/{book_id}/manifest.json")
async def get_manifest(request: Request, book_id: str, format: str=".epub"):
    # TODO: permission/auth checks go here, or decorate this route
    def rewrite_self(manifest, manifest_uri):
        for i in range(len(manifest['links'])):
            if manifest['links'][i].get('rel') == 'self':
                manifest['links'][i]['href'] = manifest_uri
        return manifest

    readium_uri = f"http://lenny_readium:15080/{encode_book_path(book_id, format=format)}/manifest.json"
    manifest = requests.get(readium_uri).json()
    manifest_uri = f"{get_lenny_uri(request)}/v1/api/item/{book_id}/manifest.json"
    return rewrite_self(manifest, manifest_uri)

# Proxy all other readium requests
@router.get("/items/{book_id}/{readium_uri:path}")
async def proxy_readium(request: Request, book_id: str, readium_uri: str, format: str=".epub"):
    # TODO: permission/auth checks go here, or decorate this route
    readium_url = f"http://lenny_readium:15080/{encode_book_path(book_id, format=format)}/{readium_uri}"
    print(readium_url)
    r = requests.get(readium_url, params=dict(request.query_params))
    if readium_url.endswith('.json'):
        return r.json()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return Response(content=r.content, media_type=content_type)

# Redirect to the Thorium Web Reader
@router.get("/read/{book_id}")
async def redirect_reader(request: Request, book_id: str, format: str = "epub"):
    manifest_uri = f"{get_lenny_uri(request)}/v1/api/items/{book_id}/manifest.json"
    reader_url = f"{get_lenny_uri(request, port=False)}:3000/read?book={manifest_uri}"
    return RedirectResponse(url=reader_url, status_code=307)

@router.post('/upload', status_code=status.HTTP_200_OK)
async def create_items(
    openlibrary_edition: int = Form(..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(..., description="The PDF or EPUB file to upload (max 50MB)")
    ):
    allowed_extensions = {".pdf", ".epub"}
    allowed_mime_types = {"application/pdf", "application/epub+zip"}

    if file.size is not None and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File '{file.filename}' is too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB."
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
