import httpx
from typing import List, Generator, Optional, Dict, Any
from urllib.parse import urlencode
import logging

from lenny.configs import LENNY_HTTP_HEADERS

logger = logging.getLogger(__name__)


def _redact_auth_header(response: httpx.Response) -> None:
    """Scrub OL S3 keys from the request record hanging off a completed response,
    so they cannot surface in httpx logs or exception reprs.

    This MUST stay a ``response`` hook. httpx invokes ``request`` hooks with the
    live outgoing Request *before* it is transmitted, so mutating the header
    there does not redact a log line — it redacts the real credential on the
    wire. Open Library then sees ``Authorization: LOW [REDACTED]``, fails to find
    a ``:`` to split on, and answers ``missing_or_invalid_authorization`` with
    HTTP 200, which is why this went unnoticed for so long.

    Redacting after the exchange gets the same protection: by the time anything
    can log or repr the request, the credential is gone.
    """
    if response.request.headers.get("Authorization", "").startswith("LOW "):
        response.request.headers["Authorization"] = "LOW [REDACTED]"


_REDACT_HOOKS = {"response": [_redact_auth_header]}


def ol_auth_headers() -> Dict[str, str]:
    """Build headers for an OL request, adding `Authorization: LOW <access>:<secret>`
    when IA S3 keys are configured. Returns a copy so callers can mutate safely."""
    # Import at call time so a test that patches lenny.configs picks up the new values.
    from lenny import configs
    headers = dict(LENNY_HTTP_HEADERS)
    if configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY:
        headers["Authorization"] = (
            f"LOW {configs.OL_S3_ACCESS_KEY}:{configs.OL_S3_SECRET_KEY}"
        )
    return headers


def ol_auth_status() -> Dict[str, Any]:
    """Current Lenny<->OL auth state for status/UI consumption. Never returns secrets."""
    from lenny import configs
    ol_ready = bool(configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY)
    return {
        "logged_in": ol_ready,
        "username": configs.OL_USERNAME,
        "lending_mode": configs.read_lending_mode(),
        "ol_ready": ol_ready,
        "ol_indexed": configs.OL_INDEXED,
    }


class OpenLibrary:
    SEARCH_URL = "https://openlibrary.org/search.json"
    HTTP_HEADERS = LENNY_HTTP_HEADERS
    HTTP_TIMEOUT = 30
    DEFAULT_FIELDS = [
        'key', 'title', 'author_key', 'author_name', 'editions', 'editions.*',
    ]
    COVER_SERVER = "https://covers.openlibrary.org"
    
    @classmethod
    def _construct_search_url(cls, query: str, fields: Optional[List[str]] = None, page: int = 1, limit: int = 100) -> str:
        fields = list(set(cls.DEFAULT_FIELDS + (fields or [])))
        params = {
            'q': query,
            'fields': ','.join(fields),
            'page': page,
            'limit': limit
        }
        return f"{cls.SEARCH_URL}?{urlencode(params)}"

    @classmethod
    def search(
        cls,
        query: str,
        fields: Optional[List[str]] = None,
        offset: int = 0,
        limit: int = 100,
        max_results: Optional[int] = None,
    ) -> Generator["OpenLibraryRecord", None, None]:
        page = offset // limit + 1
        start_doc = (offset % limit)
        num_yielded = 0
        
        while True:
            data = cls.search_json(query, fields=fields, page=page, limit=limit)
            docs = data.get("docs", []) if isinstance(data, dict) else []
            page += 1

            if start_doc is not None:
                docs = docs[start_doc:]
                start_doc = None
            
            for doc in docs:
                yield OpenLibraryRecord(doc)
                num_yielded += 1
                if max_results and num_yielded >= max_results:
                    return

            if not docs or len(docs) < limit:
                break

    @classmethod
    def search_json(cls, query: str, fields: Optional[List[str]] = None, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        url = cls._construct_search_url(query, fields, page, limit)
        # One retry on transient transport errors (e.g. SSL EOF, connection reset)
        # which OL occasionally returns under load; a fresh client + new TCP socket
        # almost always succeeds the second time.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                with httpx.Client(event_hooks=_REDACT_HOOKS) as client:
                    response = client.get(url, headers=ol_auth_headers(), timeout=cls.HTTP_TIMEOUT)
                    response.raise_for_status()
                    return response.json()
            except httpx.TransportError as e:
                last_exc = e
                logger.warning(f"OL search transport error (attempt {attempt}): {e}")
                continue
            except (httpx.HTTPError, ValueError) as e:
                logger.error(f"Error searching Open Library: {e}")
                raise
        logger.error(f"Error searching Open Library after retry: {last_exc}")
        raise last_exc  # type: ignore[misc]

    
class OpenLibraryRecord(dict):
    def __init__(self, data=None, **kwargs):
        data = data or {}
        super().__init__()
        for key, value in {**data, **kwargs}.items():
            self[key] = self._wrap(value)

    @property
    def cover_url(self) -> Optional[str]:
        if cover_i := self.edition.get('cover_i'):
            return f"{OpenLibrary.COVER_SERVER}/b/id/{cover_i}-M.jpg"
    
    @property
    def edition(self) -> Optional[str]:
        return self.editions['docs'][0]
        
    @property
    def olid(self) -> Optional[str]:
        """Uses a helper class OpenLibraryID to return a special
        string of the re form `OL[0-9]+M` which, when cast to int will
        return only the [0-9]+ value
        """
        class OpenLibraryID(str):
            def __new__(cls, value):
                return super().__new__(cls, value)
            def __int__(self):
                # e.g., "OL123M" -> 123
                return int(self.strip("OLM"))
        return OpenLibraryID(self.edition.key.split('/')[-1])

    @property
    def standardebooks_id(self):
        try:
            return self['id_standard_ebooks'][0]
        except KeyError as e:
            return None

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            try:
                return super().__getattribute__(key)
            except AttributeError:
                raise AttributeError(f"'OpenLibraryRecord' object has no attribute '{key}'")

    def __setattr__(self, key, value):
        self[key] = self._wrap(value)

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError:
            raise AttributeError(f"'OpenLibraryRecord' object has no attribute '{key}'")

    def __add__(self, other):
        if isinstance(other, dict):
            merged = dict(self)  # create a copy
            merged.update(other)
            return OpenLibraryRecord(merged)
        return NotImplemented

    @classmethod
    def _wrap(cls, value):
        if isinstance(value, dict):
            return cls(value)
        elif isinstance(value, list):
            return [cls._wrap(v) for v in value]
        return value
