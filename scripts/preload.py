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
import httpx
import os
import sys
from urllib.parse import urlencode
from io import BytesIO
from typing import List, Generator, Optional, Dict, Any
from lenny.core.openlibrary import OpenLibrary
from lenny.core.api import LennyAPI
from lenny.core.client import LennyClient
import logging

logger = logging.getLogger(__name__)


class StandardEbooks:

    BASE_URL = "https://archive.org/download/lenny-open-access-preloads"
    HTTP_TIMEOUT = 15
    EPUB_HEADER = b'PK\x03\x04'

    @classmethod
    def construct_download_url(cls, identifier: str) -> str:
        identifier_file = identifier.replace("/", "_")
        return f"{cls.BASE_URL}/{identifier_file}.epub"

    @classmethod
    def verify_download(cls, content: Optional[BytesIO]) -> Optional[BytesIO]:
        if not content or not content.getbuffer().nbytes:
            return None
        header = content.read(4)
        content.seek(0)
        if not header.startswith(cls.EPUB_HEADER):
            logger.warning(f"Downloaded file failed EPUB verification (bad magic bytes: {header!r})")
            return None
        return content

    @classmethod
    def download(cls, identifier: str, timeout: Optional[int] = None) -> Optional[BytesIO]:
        url = cls.construct_download_url(identifier)
        try:
            with httpx.Client() as client:
                with client.stream("GET", url, headers=LennyClient.HTTP_HEADERS, follow_redirects=True, timeout=timeout or cls.HTTP_TIMEOUT) as response:
                    if response.status_code == 404:
                        logger.warning(f"EPUB not in preload set (404): {url}")
                        return None
                    response.raise_for_status()
                    content = BytesIO()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        content.write(chunk)
                    content.seek(0)
                    return content
        except httpx.TimeoutException:
            logger.error(f"Timed out downloading {url}")
            return None
        except httpx.HTTPError as e:
            logger.error(f"Error downloading {url}: {e}")
            return None


def import_standardebooks(limit=None, offset=0):
    logger.info("[Preloading] Fetching StandardEbooks from Open Library...")

    stats = {"uploaded": 0, "skipped": 0, "not_in_set": 0, "failed": 0, "ol_error": False}

    books = OpenLibrary.search('id_standard_ebooks:*', offset=offset, fields=['id_standard_ebooks'])

    try:
        for i, book in enumerate(books):
            try:
                olid = int(book.olid)
            except (ValueError, AttributeError, TypeError) as e:
                logger.warning(f"Skipping record {i}: could not parse OLID ({e})")
                stats["skipped"] += 1
                continue

            standardebooks_id = book.standardebooks_id
            if not standardebooks_id:
                logger.warning(f"Skipping OLID {olid}: no Standard Ebooks ID in OL record")
                stats["skipped"] += 1
                continue

            try:
                epub = StandardEbooks.download(standardebooks_id)
                if epub is None:
                    stats["not_in_set"] += 1
                    continue

                if not StandardEbooks.verify_download(epub):
                    logger.warning(f"Skipping OLID {olid}: EPUB verification failed")
                    stats["failed"] += 1
                    continue

                uploaded = LennyClient.upload(olid, epub, encrypted=False)
                if uploaded:
                    stats["uploaded"] += 1
                    if limit is not None and stats["uploaded"] >= limit:
                        break
                else:
                    stats["failed"] += 1

            except Exception as e:
                logger.error(f"Unexpected error processing OLID {olid}: {e}")
                stats["failed"] += 1

    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Open Library search failed: {e}")
        stats["ol_error"] = True

    logger.info(
        f"[Preloading] Done — uploaded: {stats['uploaded']}, "
        f"skipped: {stats['skipped']}, not in set: {stats['not_in_set']}, "
        f"failed: {stats['failed']}"
    )
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preload StandardEbooks from Open Library")
    parser.add_argument("-n", type=int, help="Number of books to preload", default=None)
    parser.add_argument("-o", type=int, help="Offset", default=0)
    args = parser.parse_args()
    stats = import_standardebooks(limit=args.n, offset=args.o)
    if stats["ol_error"]:
        sys.exit(1)
