"""
Preload README

1. Asks OpenLibrary.org/search.json API for info about every standardebook it knows
2. Loops over these records, downloads and verifies the corresponding epubs
3. Uses the LennyClient to upload each book to the LennyAPI `/upload` endpoint
    - This creates a new Lenny Item, keyed by openlibrary_edition_id (i.e. olid) in the db
    - Book files are stored in MinIO s3 w/ bucket `bookshelf/` + the book's `olid` as int + ext
        - e.g. An `olid` of OL32941311M -> 32941311
        - "/bookshelf/32941311.epub"
"""

import argparse
import requests
import os
from urllib.parse import urlencode
from io import BytesIO
from typing import List, Generator, Optional, Dict, Any
from lenny.core.openlibrary import OpenLibrary
from lenny.core.api import LennyAPI
from lenny.core.client import LennyClient
import logging

logger = logging.getLogger(__name__)


class StandardEbooks:

    BASE_URL = "https://standardebooks.org/ebooks"
    HTTP_TIMEOUT = 5
    EPUB_HEADER = b'PK\x03\x04'

    @classmethod
    def construct_download_url(cls, identifier: str) -> str:
        identifier_path = identifier
        identifier_file = identifier.replace("/", "_")
        return f"{cls.BASE_URL}/{identifier_path}/downloads/{identifier_file}.epub?source=download"

    @classmethod
    def verify_download(cls, content):
        if content and content.getbuffer().nbytes and content.read(4).startswith(cls.EPUB_HEADER):
            content.seek(0)
            return content
        return None

    @classmethod
    def download(cls, identifier: str, timeout: Optional[int] = None) -> Optional[BytesIO]:
        url = cls.construct_download_url(identifier)
        try:
            response = requests.get(
                url,
                headers=LennyClient.HTTP_HEADERS,
                allow_redirects=True,
                timeout=timeout or cls.HTTP_TIMEOUT,
                stream=True
            )
            response.raise_for_status()
            content = BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                content.write(chunk)
            content.seek(0)
            return content
        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading {url}: {e}")
            return None

def import_standardebooks(limit=None, offset=0):
    logger.info("[Preloading] Fetching StandardEbooks from Open Library...")
    query = 'id_standard_ebooks:*'
    for i, book in enumerate(OpenLibrary.search(query, offset=offset, fields=['id_standard_ebooks'])):
        if limit is not None and i >= limit:
            break
        if int(book.olid) and book.standardebooks_id:
            epub = StandardEbooks.download(book.standardebooks_id)
            if StandardEbooks.verify_download(epub):
                LennyClient.upload(int(book.olid), epub, encrypted=False)

if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
    parser = argparse.ArgumentParser(description="Preload StandardEbooks from Open Library")
    parser.add_argument("-n", type=int, help="Number of books to preload", default=None)
    parser.add_argument("-o", type=int, help="Offset", default=0)
    args = parser.parse_args()
    import_standardebooks(limit=args.n, offset=args.o)

