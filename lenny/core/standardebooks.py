#!/usr/bin/env python

"""
    Standard Ebooks importer for Lenny.

    1. Asks OpenLibrary.org/search.json API for info about every standardebook it knows
    2. Loops over these records, downloads and verifies the corresponding epubs
    3. Uses the LennyClient to upload each book to the LennyAPI `/upload` endpoint
        - This creates a new Lenny Item, keyed by openlibrary_edition_id (i.e. olid) in the db
        - Book files are stored in Garage s3 w/ bucket `bookshelf/` + the book's `olid` as int + ext
            - e.g. An `olid` of OL32941311M -> 32941311
            - "/bookshelf/32941311.epub"

    Lives in core/ (rather than scripts/) so both the CLI and the admin import
    endpoint can drive it — same shape as core/briet.py.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import logging
from io import BytesIO
from typing import Optional

import httpx

from lenny.core.client import LennyClient, download_epub, verify_epub
from lenny.core.imports import ImportJob, DOWNLOADING, DONE, FAILED
from lenny.core.models import Item
from lenny.core.openlibrary import OpenLibrary

logger = logging.getLogger(__name__)

SOURCE = "standardebooks"


class StandardEbooks:

    BASE_URL = "https://archive.org/download/lenny-open-access-preloads"
    HTTP_TIMEOUT = 15

    @classmethod
    def construct_download_url(cls, identifier: str) -> str:
        identifier_file = identifier.replace("/", "_")
        return f"{cls.BASE_URL}/{identifier_file}.epub"

    @classmethod
    def verify_download(cls, content: Optional[BytesIO]) -> Optional[BytesIO]:
        return verify_epub(content)

    @classmethod
    def download(cls, identifier: str, timeout: Optional[int] = None) -> Optional[BytesIO]:
        return download_epub(
            cls.construct_download_url(identifier),
            timeout=timeout or cls.HTTP_TIMEOUT,
            headers=LennyClient.HTTP_HEADERS,
        )


def import_standardebooks(limit=None, offset=0):
    logger.info("[Preloading] Fetching StandardEbooks from Open Library...")

    stats = {"uploaded": 0, "skipped": 0, "not_in_set": 0, "failed": 0, "ol_error": False}

    # Books already in the library are skipped *before* downloading. Without this,
    # `limit` would be spent re-fetching the same first N books on every run (the
    # upload 409s and counts as a success), so asking for "50 more" would import
    # nothing. Cheap: one query, and the catalog is small enough to hold.
    existing = set(Item.get_all())

    try:
        books = OpenLibrary.search('id_standard_ebooks:*', offset=offset, fields=['id_standard_ebooks'])
        for i, book in enumerate(books):
            try:
                olid = int(book.olid)
            except (ValueError, AttributeError, TypeError) as e:
                logger.warning(f"Skipping record {i}: could not parse OLID ({e})")
                stats["skipped"] += 1
                continue

            if olid in existing:
                stats["skipped"] += 1
                continue

            standardebooks_id = book.standardebooks_id
            if not standardebooks_id:
                logger.warning(f"Skipping OLID {olid}: no Standard Ebooks ID in OL record")
                stats["skipped"] += 1
                continue

            try:
                ImportJob.record(SOURCE, olid, DOWNLOADING)
                epub = StandardEbooks.download(standardebooks_id)
                if epub is None:
                    stats["not_in_set"] += 1
                    ImportJob.record(SOURCE, olid, FAILED, "Not in the preload set")
                    continue

                if not StandardEbooks.verify_download(epub):
                    logger.warning(f"Skipping OLID {olid}: EPUB verification failed")
                    stats["failed"] += 1
                    ImportJob.record(SOURCE, olid, FAILED, "EPUB verification failed")
                    continue

                uploaded = LennyClient.upload(olid, epub, encrypted=False)
                if uploaded:
                    stats["uploaded"] += 1
                    existing.add(olid)
                    ImportJob.record(SOURCE, olid, DONE)
                    if limit is not None and stats["uploaded"] >= limit:
                        break
                else:
                    stats["failed"] += 1
                    ImportJob.record(SOURCE, olid, FAILED, "Upload to Lenny failed")

            except Exception as e:
                logger.error(f"Unexpected error processing OLID {olid}: {e}")
                stats["failed"] += 1
                ImportJob.record(SOURCE, olid, FAILED, str(e))

    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Open Library search failed: {e}")
        stats["ol_error"] = True

    logger.info(
        f"[Preloading] Done — uploaded: {stats['uploaded']}, "
        f"skipped: {stats['skipped']}, not in set: {stats['not_in_set']}, "
        f"failed: {stats['failed']}"
    )
    return stats
