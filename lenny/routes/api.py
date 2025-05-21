#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get('/', status_code=status.HTTP_200_OK)
async def root():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lenny API</title>
    </head>
    <body>
        <h1 style="text-align: center;">Lenny: A Free, Open Source Lending System for Libraries</h1>
        <img src="/static/lenny.png" alt="Lenny Logo" style="display: block; margin: 0 auto;">
        <p style="text-align: center;">You can download & deploy it from <a href="https://github.com/ArchiveLabs/lenny">Github</a> </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, media_type="text/html")

@router.post('/upload', status_code=status.HTTP_200_OK)
async def upload():
    return {"message": "Upload endpoint"}

