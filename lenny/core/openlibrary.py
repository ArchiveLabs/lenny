
import requests
from typing import List, Generator, Optional, Dict, Any
from urllib.parse import urlencode

from lenny.configs import LENNY_HTTP_HEADERS

class OpenLibrary:
    
    SEARCH_URL = "https://openlibrary.org/search.json"
    HTTP_HEADERS = LENNY_HTTP_HEADERS
    HTTP_TIMEOUT = 5
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
        try:
            response = requests.get(url, headers=cls.HTTP_HEADERS, timeout=cls.HTTP_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"Error searching Open Library: {e}")
            return {}

    
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
