#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from fastapi import APIRouter, status, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi import HTTPException
from pathlib import Path
from lenny import app
from lenny.core import db, s3
from lenny.core.api import LennyAPI

router = APIRouter()

@router.get('/', status_code=status.HTTP_200_OK)
async def root():
    return request.app.templates("index.html", {"request": request})


@router.post('/upload', status_code=status.HTTP_200_OK)
async def upload(
    openlibrary_edition: int = Form(..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(..., description="The PDF or EPUB file to upload (max 50MB)")
    ):

    LennyAPI.add(
        openlibrary_edition=openlibrary_edition,
        encrypted=encrypted,
        files=[file]  # TODO expand to allow multiple 
    )
    return HTMLResponse(status_code=status.HTTP_200_OK, content="File uploaded successfully.")
