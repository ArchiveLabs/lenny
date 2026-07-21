#!/usr/bin/env python

"""
    BRIET importer for Lenny.

    BRIET (market.briet.app) sells book bundles; a buyer receives a one-time
    redeem code. Redeeming it returns the books in that bundle — each with a
    download link and an Open Library edition key — which are then ingested
    into this Lenny instance exactly like any other book.

    Mirrors the shape of `StandardEbooks` in scripts/preload.py: download the
    bytes, verify them, hand them to `LennyClient.upload`. Everything
    downstream (S3 keying, format detection, dedup) is already handled by
    `LennyAPI.add`.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import logging
import time
from typing import Optional

import httpx

from lenny import configs
from lenny.core.client import LennyClient, download_epub, verify_epub
from lenny.core.imports import ImportJob, PENDING, DOWNLOADING, DONE, FAILED

logger = logging.getLogger(__name__)


def parse_olid(olid) -> Optional[int]:
    """``OL32941311M`` (or a bare int) -> ``32941311``. None if unparseable.

    Same normalization scripts/addbook.py does, which is what the rest of
    Lenny keys books on.
    """
    if olid is None:
        return None
    try:
        return int(str(olid).strip().upper().removeprefix("OL").removesuffix("M"))
    except ValueError:
        return None


class BRIET:

    SOURCE = "briet"
    REDEEM_URL = "https://market.briet.app/api/redeem-lenny"
    HTTP_HEADERS = {**configs.LENNY_HTTP_HEADERS, "User-Agent": "LennyBrietBot/1.0"}
    HTTP_TIMEOUT = 30
    DOWNLOAD_TIMEOUT = 120  # book files, not JSON
    MAX_ATTEMPTS = 3
    MAX_BACKOFF = 30  # never leave an admin's request hanging on a hostile Retry-After

    @classmethod
    def _backoff(cls, response: Optional[httpx.Response], attempt: int) -> float:
        """Seconds to wait: the server's Retry-After if it gave one, else 2**attempt."""
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                return min(float(retry_after), cls.MAX_BACKOFF)
            except ValueError:
                pass  # may be an HTTP-date; not worth parsing, fall through
        return min(2 ** attempt, cls.MAX_BACKOFF)

    @classmethod
    def fetch(cls, code: str) -> dict:
        """GET the redeem endpoint. Retries transient failures, raises otherwise.

        A redeem code is one-shot, so a dropped request costs the buyer their
        purchase — hence real backoff here, unlike the single sleepless retry
        in openlibrary.py. A 4xx other than 429 means the code is bad or spent:
        raise immediately rather than hammering it.
        """
        url = f"{cls.REDEEM_URL.rstrip('/')}/{code}"
        last_exc = None
        for attempt in range(1, cls.MAX_ATTEMPTS + 1):
            try:
                with httpx.Client() as client:
                    response = client.get(
                        url,
                        headers=cls.HTTP_HEADERS,
                        follow_redirects=True,
                        timeout=cls.HTTP_TIMEOUT,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"BRIET returned {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    if attempt < cls.MAX_ATTEMPTS:
                        delay = cls._backoff(response, attempt)
                        logger.warning(
                            f"BRIET {response.status_code} on attempt {attempt}, retrying in {delay}s"
                        )
                        time.sleep(delay)
                        continue
                    raise last_exc
                response.raise_for_status()
                return response.json()
            except httpx.TransportError as e:
                last_exc = e
                if attempt < cls.MAX_ATTEMPTS:
                    delay = cls._backoff(None, attempt)
                    logger.warning(f"BRIET transport error on attempt {attempt}, retrying in {delay}s: {e}")
                    time.sleep(delay)
                    continue
                raise
        raise last_exc

    @classmethod
    def redeem(cls, code: str) -> list:
        """Redeem `code` and return ``[{"olid": int, "url": str}, ...]``.

        Parsing is isolated here: the live BRIET response shape is not yet
        finalized, so adjusting to it should mean touching only this method.
        Both a bare list and a ``{"books": [...]}`` envelope are accepted.
        """
        payload = cls.fetch(code)
        if isinstance(payload, dict):
            raw = payload.get("books") or payload.get("items") or []
        else:
            raw = payload or []

        books = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            olid = parse_olid(entry.get("olid") or entry.get("openlibrary_edition"))
            url = entry.get("url") or entry.get("download_url")
            if olid is None or not url:
                logger.warning(f"Skipping BRIET entry with missing/bad olid or url: {entry}")
                continue
            books.append({"olid": olid, "url": url, "title": entry.get("title")})
        return books


def import_briet_books(books: list, encrypted: bool = True) -> dict:
    """Download and ingest each book, recording progress as it goes.

    Serial on purpose: one request at a time is also what keeps us politely
    under BRIET's rate limit for free.
    ponytail: add concurrency only if bundles get large enough to matter.
    """
    stats = {"uploaded": 0, "failed": 0}

    for book in books:
        olid = book["olid"]
        try:
            ImportJob.record(BRIET.SOURCE, olid, DOWNLOADING)
            epub = download_epub(
                book["url"],
                timeout=BRIET.DOWNLOAD_TIMEOUT,
                headers=BRIET.HTTP_HEADERS,
            )
            if not verify_epub(epub):
                stats["failed"] += 1
                ImportJob.record(BRIET.SOURCE, olid, FAILED, "Download failed or not a valid EPUB")
                continue

            if LennyClient.upload(olid, epub, encrypted=encrypted):
                stats["uploaded"] += 1
                ImportJob.record(BRIET.SOURCE, olid, DONE)
            else:
                stats["failed"] += 1
                ImportJob.record(BRIET.SOURCE, olid, FAILED, "Upload to Lenny failed")
        except Exception as e:
            # One bad book must never abort the rest of the bundle.
            logger.error(f"Unexpected error importing OLID {olid} from BRIET: {e}")
            stats["failed"] += 1
            ImportJob.record(BRIET.SOURCE, olid, FAILED, str(e))

    logger.info(
        f"[BRIET] Done — uploaded: {stats['uploaded']}, failed: {stats['failed']}"
    )
    return stats


def import_briet(code: str, encrypted: bool = True) -> dict:
    """Redeem a code and ingest everything it returns. Used by CLI/tests."""
    books = BRIET.redeem(code)
    for book in books:
        ImportJob.record(BRIET.SOURCE, book["olid"], PENDING)
    stats = import_briet_books(books, encrypted=encrypted)
    stats["redeemed"] = len(books)
    return stats
