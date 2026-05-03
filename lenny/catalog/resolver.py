from __future__ import annotations
import logging
import re
from typing import Optional, List, runtime_checkable, Protocol

import httpx
from rapidfuzz import fuzz

from lenny.configs import LENNY_HTTP_HEADERS
from lenny.catalog.types import (
    BookMetadata, OLResult, OLCandidate,
    OLStatus, ActionTaken,
    OL_AUTO_LINK_THRESHOLD, OL_REVIEW_THRESHOLD,
)
from lenny.catalog.exceptions import OLRateLimited, OLAuthRequired, OLWriteError

logger = logging.getLogger(__name__)

_TITLE_MISMATCH_FLOOR = 0.80  # ISBN match rejected if titles diverge more than this
_OLID_RE = re.compile(r"OL(\d+)[MAWBP]?$")


@runtime_checkable
class OLResolver(Protocol):
    """Contract that all resolver implementations must satisfy.

    The worker imports only this Protocol — swapping APIResolver for
    DumpResolver (Phase 2) requires no worker changes.
    """
    def lookup(self, metadata: BookMetadata) -> OLResult: ...
    def create_edition(self, metadata: BookMetadata) -> int: ...


class APIResolver:
    """OL lookup via live API + Google Books fallback.

    Used for jobs below CATALOG_DUMP_THRESHOLD. All I/O is synchronous
    (no asyncio) — called from ThreadPoolExecutor worker threads.
    """

    OL_BASE = "https://openlibrary.org"
    GB_BASE = "https://www.googleapis.com/books/v1"

    def __init__(
        self,
        ol_session_cookie: Optional[str] = None,
        ol_access_key: Optional[str] = None,
        ol_secret_key: Optional[str] = None,
        google_books_api_key: Optional[str] = None,
        timeout: int = 10,
    ):
        self._ol_access = ol_access_key
        self._ol_secret = ol_secret_key
        self._google_key = google_books_api_key
        self._timeout = timeout
        self._headers = dict(LENNY_HTTP_HEADERS)
        self._ol_session: Optional[str] = ol_session_cookie

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def lookup(self, metadata: BookMetadata) -> OLResult:
        """Run the full resolution cascade. Never raises — returns OLResult."""
        if not metadata.is_resolvable:
            return OLResult(
                status=OLStatus.INSUFFICIENT_METADATA,
                action=ActionTaken.NEEDS_REVIEW,
            )

        # 1. ISBN → OL direct lookup
        if metadata.best_isbn:
            result = self._lookup_isbn(metadata.best_isbn, metadata)
            if result.confidence >= OL_AUTO_LINK_THRESHOLD:
                return result

        # 2 + 3. OL title/author search (exact → fuzzy scoring inside)
        if metadata.title:
            result = self._search_exact(metadata)
            if result.confidence >= OL_AUTO_LINK_THRESHOLD:
                return result
            if result.needs_review:
                return result

        # 4. Google Books fallback
        if self._google_key and metadata.title:
            result = self._google_books_lookup(metadata)
            if result.confidence >= OL_AUTO_LINK_THRESHOLD:
                return result

        # 5. Not found — caller will create OL record
        return OLResult(status=OLStatus.OL_NOT_FOUND, action=ActionTaken.CREATE_FULL)

    def create_edition(self, metadata: BookMetadata) -> int:
        """Create a new OL edition record. Returns the integer OLID."""
        session_cookie = self._ensure_ol_session()
        author_key = self._find_or_create_author(metadata.primary_author or "Unknown", session_cookie)
        payload = self._build_edition_payload(metadata, author_key)

        headers = {**self._headers, "Cookie": f"session={session_cookie}", "Content-Type": "application/json"}
        try:
            with httpx.Client(headers=headers, timeout=30) as client:
                r = client.post(f"{self.OL_BASE}/api/import", json=payload)
                if r.status_code == 429:
                    raise OLRateLimited("OL import API rate limited (429)")
                if r.status_code == 409:
                    data = r.json()
                    olid = self._parse_olid(data.get("id", ""))
                    if not olid:
                        raise OLWriteError(f"OL conflict response has no parseable ID: {data}")
                    return olid
                r.raise_for_status()
                data = r.json()
                olid = self._parse_olid(data.get("id", ""))
                if not olid:
                    raise OLWriteError(f"OL import returned no ID: {data}")
                return olid
        except OLRateLimited:
            raise
        except httpx.HTTPStatusError as e:
            raise OLWriteError(f"OL import failed ({e.response.status_code}): {e}") from e

    # ------------------------------------------------------------------
    # Private: OL read methods
    # ------------------------------------------------------------------

    def _lookup_isbn(self, isbn: str, metadata: BookMetadata) -> OLResult:
        url = f"{self.OL_BASE}/isbn/{isbn}.json"
        try:
            with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
                r = client.get(url)
                if r.status_code == 429:
                    raise OLRateLimited(f"OL rate limited on ISBN lookup for {isbn}")
                if r.status_code == 404:
                    return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)
                r.raise_for_status()
                data = r.json()
        except OLRateLimited:
            raise
        except httpx.HTTPStatusError:
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)
        except Exception as e:
            logger.warning("ISBN lookup error for %s: %s", isbn, e)
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        olid = self._parse_olid(data.get("key", ""))
        if not olid:
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        ol_title = data.get("title", "")
        if metadata.title and ol_title:
            title_score = fuzz.token_sort_ratio(metadata.title.lower(), ol_title.lower()) / 100.0
            if title_score < _TITLE_MISMATCH_FLOOR:
                logger.info(
                    "ISBN %s rejected: title mismatch (expected %r, got %r, score=%.2f)",
                    isbn, metadata.title, ol_title, title_score,
                )
                return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        candidate = OLCandidate(
            olid=olid,
            title=ol_title,
            authors=[],
            year=str(data.get("publish_date", "")),
            publisher=(data.get("publishers") or [None])[0],
            score=0.99,
        )
        return OLResult(
            status=OLStatus.OL_MATCH_CLEAN,
            olid=olid,
            confidence=0.99,
            candidates=[candidate],
            action=ActionTaken.LINK_ONLY,
        )

    def _search_exact(self, metadata: BookMetadata) -> OLResult:
        params = {
            "title": metadata.title,
            "author": metadata.primary_author,
            "fields": "key,title,author_name,editions,editions.key,editions.publish_date,editions.publishers",
            "limit": 5,
        }
        try:
            with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
                r = client.get(f"{self.OL_BASE}/search.json", params=params)
                if r.status_code == 429:
                    raise OLRateLimited("OL rate limited on search")
                r.raise_for_status()
                docs = r.json().get("docs", [])
        except OLRateLimited:
            raise
        except Exception as e:
            logger.warning("OL search error: %s", e)
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        candidates: List[OLCandidate] = []
        for doc in docs:
            try:
                editions = doc.get("editions", {}).get("docs", [])
                if not editions:
                    continue
                edition = editions[0]
                olid = self._parse_olid(edition.get("key", ""))
                if not olid:
                    continue

                ol_title = doc.get("title", "")
                ol_authors = doc.get("author_name", [])

                title_score = fuzz.token_sort_ratio(
                    (metadata.title or "").lower(), ol_title.lower()
                ) / 100.0

                author_score = 0.0
                if metadata.primary_author and ol_authors:
                    author_score = max(
                        fuzz.token_sort_ratio(metadata.primary_author.lower(), a.lower()) / 100.0
                        for a in ol_authors
                    )

                combined = round(title_score * 0.6 + author_score * 0.4, 3)
                candidates.append(OLCandidate(
                    olid=olid,
                    title=ol_title,
                    authors=ol_authors,
                    year=(edition.get("publish_date") or [""])[0] if isinstance(edition.get("publish_date"), list) else edition.get("publish_date", ""),
                    publisher=(edition.get("publishers") or [None])[0],
                    score=combined,
                ))
            except (ValueError, KeyError, IndexError, TypeError):
                continue

        if not candidates:
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0]

        if best.score >= OL_AUTO_LINK_THRESHOLD:
            return OLResult(
                status=OLStatus.OL_MATCH_CLEAN,
                olid=best.olid,
                confidence=best.score,
                candidates=candidates,
                action=ActionTaken.LINK_ONLY,
            )
        if best.score >= OL_REVIEW_THRESHOLD:
            return OLResult(
                status=OLStatus.OL_MATCH_FUZZY,
                olid=best.olid,
                confidence=best.score,
                candidates=candidates,
                action=ActionTaken.NEEDS_REVIEW,
            )
        return OLResult(
            status=OLStatus.OL_NOT_FOUND,
            confidence=best.score,
            candidates=candidates,
        )

    def _google_books_lookup(self, metadata: BookMetadata) -> OLResult:
        if metadata.best_isbn:
            q = f"isbn:{metadata.best_isbn}"
        else:
            q = f'intitle:"{metadata.title}"'
            if metadata.primary_author:
                q += f' inauthor:"{metadata.primary_author}"'

        params = {"q": q, "key": self._google_key, "maxResults": 3}
        try:
            with httpx.Client(timeout=self._timeout) as client:
                r = client.get(f"{self.GB_BASE}/volumes", params=params)
                r.raise_for_status()
                items = r.json().get("items", [])
        except Exception as e:
            logger.warning("Google Books lookup error: %s", e)
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        if not items:
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        vol = items[0].get("volumeInfo", {})
        gb_title = vol.get("title", "")
        title_score = fuzz.token_sort_ratio(
            (metadata.title or "").lower(), gb_title.lower()
        ) / 100.0

        if title_score < OL_REVIEW_THRESHOLD:
            return OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)

        return OLResult(
            status=OLStatus.OL_NOT_FOUND,
            confidence=title_score,
            action=ActionTaken.CREATE_FULL,
        )

    # ------------------------------------------------------------------
    # Private: OL write methods
    # ------------------------------------------------------------------

    def _ensure_ol_session(self) -> str:
        if self._ol_session:
            return self._ol_session
        if self._ol_access and self._ol_secret:
            self._ol_session = self._ol_login(self._ol_access, self._ol_secret)
            return self._ol_session
        raise OLAuthRequired("No OL credentials provided. Pass ol_session_cookie or ol_access_key+ol_secret_key.")

    def _ol_login(self, access_key: str, secret_key: str) -> str:
        with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
            r = client.post(
                f"{self.OL_BASE}/account/login",
                json={"access": access_key, "secret": secret_key},
            )
            if r.status_code == 429:
                raise OLRateLimited("OL login rate limited (429)")
            r.raise_for_status()
            session = r.cookies.get("session")
            if not session:
                raise OLAuthRequired("OL login succeeded but returned no session cookie")
            return session

    def _find_or_create_author(self, name: str, session_cookie: str) -> str:
        try:
            with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
                r = client.get(
                    f"{self.OL_BASE}/search/authors.json",
                    params={"q": name, "limit": 1},
                )
                r.raise_for_status()
                docs = r.json().get("docs", [])
                if docs:
                    key = docs[0].get("key", "")
                    if key:
                        return key if key.startswith("/") else f"/authors/{key}"
        except Exception as e:
            logger.warning("OL author search failed for %r: %s", name, e)

        payload = {"name": name, "type": {"key": "/type/author"}}
        headers = {**self._headers, "Cookie": f"session={session_cookie}", "Content-Type": "application/json"}
        with httpx.Client(headers=headers, timeout=self._timeout) as client:
            r = client.post(f"{self.OL_BASE}/api/import", json=payload)
            if r.status_code == 429:
                raise OLRateLimited("OL rate limited creating author")
            r.raise_for_status()
            data = r.json()
            key = data.get("id", "")
            if not key:
                raise OLWriteError(f"Failed to create OL author for {name!r}: {data}")
            return key if key.startswith("/") else f"/authors/{key}"

    def _build_edition_payload(self, metadata: BookMetadata, author_key: str) -> dict:
        payload: dict = {
            "title": metadata.title,
            "authors": [{"key": author_key}],
            "physical_format": "ebook",
            "source_records": [f"lenny:{metadata.source}"],
        }
        if metadata.publisher:
            payload["publishers"] = [metadata.publisher]
        if metadata.publish_date:
            payload["publish_date"] = metadata.publish_date
        if metadata.isbn_13:
            payload["isbn_13"] = [metadata.isbn_13]
        if metadata.isbn_10:
            payload["isbn_10"] = [metadata.isbn_10]
        if metadata.language:
            payload["languages"] = [{"key": f"/languages/{metadata.language}"}]
        if metadata.description:
            payload["description"] = {"type": "/type/text", "value": metadata.description}
        if metadata.subjects:
            payload["subjects"] = metadata.subjects
        return payload

    @staticmethod
    def _parse_olid(key: str) -> Optional[int]:
        """Extract integer OLID from OL keys like '/books/OL123M' or 'OL123M'."""
        if not key:
            return None
        part = key.split("/")[-1]
        m = _OLID_RE.match(part)
        return int(m.group(1)) if m else None
