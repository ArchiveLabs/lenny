
from io import BytesIO
import requests
from lenny.models import db
from lenny.models.items import Item
from lenny.core.openlibrary import OpenLibrary
from lenny.core.utils import encode_book_path
from lenny.core.opds import (
    Author,
    OPDSFeed,
    Publication,
    Link,
    OPDS_REL_ACQUISITION
)
from lenny.configs import (
    SCHEME, HOST, PORT,
    READER_PORT, READIUM_BASE_URL,
    LENNY_HTTP_HEADERS
)


class LennyAPI:

    OPDS_TITLE = "Lenny Catalog"
    MAX_FILE_SIZE = 50 * 1024 * 1024
    Item = Item
    
    @classmethod
    def make_manifest_url(cls, book_id):
        return cls.make_url(f"/v1/api/items/{book_id}/manifest.json")

    @classmethod
    def make_readium_url(cls, book_id, format, readium_path):
        ebp = encode_book_path(book_id, format=format)
        readium_url = f"{READIUM_BASE_URL}/{ebp}/{readium_path}"
        return readium_url

    @classmethod
    def make_reader_url(cls, manifest_uri):
        path = f"/read?book={manifest_uri}"
        return cls.make_url(path, port=READER_PORT)

    @classmethod
    def make_url(cls, path, port=PORT):
        """Constructs a public Lenny URL that points to the public HOST and PORT
        """        
        url = f"{SCHEME}://{HOST}"
        if port and port not in {80, 443}:
            url += f":{port}"
        return f"{url}{path}"

    @classmethod
    def get_items(cls, offset=None, limit=None):
        return db.query(Item).offset(offset).limit(limit).all()

    @classmethod
    def _enrich_items(cls, items, fields=None):
        items = cls.get_items(offset=None, limit=None)
        imap = dict((i.openlibrary_edition, i) for i in items)
        olids = [f"OL{i}M" for i in imap.keys()]
        q = f"edition_key:({' OR '.join(olids)})"
        return dict((
            # keyed by olid as int
            int(book.olid),
            # openlibrary book with item added as `lenny`
            book + {"lenny": imap[int(book.olid)]}
        ) for book in OpenLibrary.search(query=q, fields=fields))
    
    @classmethod
    def get_enriched_items(cls, fields=None, offset=None, limit=None):
        return cls._enrich_items(
            cls.get_items(offset=offset, limit=limit),
            fields=fields
        )

    @classmethod
    def opds_feed(cls, offset=None, limit=None):
        """
        Convert combined Lenny+OL items to OPDS 2.0 JSON feed.
        """
        read_uri = cls.make_url("/v1/api/read/")
        
        feed = OPDSFeed(
            metadata={"title": cls.OPDS_TITLE},
            publications=[]
        )
        
        items = cls.get_enriched_items(offset=offset, limit=limit)
        for edition_id, data in items.items():
            lenny = data["lenny"]
            edition = data.edition
            title = edition.get("title", "Untitled")
            authors = [Author(name=a) for a in edition.get("author_name", [])]

            # hardcode format for now...
            links = [Link(
                href=f"{read_uri}{edition_id}",
                type="application/epub+zip",
                rel=OPDS_REL_ACQUISITION
            )]
            if data.cover_url:
                links.append(
                    Link(
                        href=data.cover_url,
                        type="image/jpeg",
                        rel="http://opds-spec.org/image"
                    )
                )
            pub = Publication(
                metadata={
                    "title": title,
                    "identifier": f"OL{edition_id}M",
                    "modified": lenny.updated_at,
                    "author": [a.to_dict() for a in authors],
                },
                links=links,
            )

            feed.publications.append(pub)
        return feed.to_dict()

    @classmethod
    def patch_readium_manifest(cls, manifest: dict, book_id: str):
        """Rewrites `self` to link to the correct public url"""
        for i in range(len(manifest['links'])):
            if manifest['links'][i].get('rel') == 'self':
                manifest['links'][i]['href'] = cls.make_url(
                    f"/v1/api/item/{book_id}/manifest.json"
                )
        return manifest        

class LennyClient:

    UPLOAD_API_URL = f"http://localhost:1337/v1/api/upload"
    HTTP_HEADERS = LENNY_HTTP_HEADERS

    @classmethod
    def upload(cls, olid: int, file_content: BytesIO, encrypted: bool = False,  timeout: int = 120) -> bool:
        data_payload = {
            'openlibrary_edition': olid,
            'encrypted': str(encrypted).lower()
        }
        files_payload = {
            'file': ('book.epub', file_content, 'application/epub+zip')
        }
        try:
            response = requests.post(
                cls.UPLOAD_API_URL,
                data=data_payload,
                files=files_payload,
                headers=cls.HTTP_HEADERS,
                timeout=timeout,
                verify=False
            )
            print(response.content)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False
