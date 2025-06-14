
from fastapi import Request
from lenny.models import db
from lenny.models.items import Item
from lenny.core.openlibrary import OpenLibrary
from lenny.core.opds import (
    Author,
    OPDSFeed,
    Publication,
    Link,
    OPDS_REL_ACQUISITION
)
from lenny.configs import PORT

class LennyAPI:

    OPDS_TITLE = "Lenny Catalog"
    MAX_FILE_SIZE = 50 * 1024 * 1024

    def __init__(self):
        pass
    
    @classmethod
    def get_uri(cls, request: Request, port=True):
        host = f"{request.url.scheme}://{request.url.hostname}"
        if port and PORT and PORT not in {80, 443}:
            host += f":{PORT}"
        return host

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
    def opds(cls, request: Request, offset=None, limit=None):
        """
        Convert combined Lenny+OL items to OPDS 2.0 JSON feed.
        """
        read_uri = f"{cls.get_uri(request)}/v1/api/read/"
        
        feed = OPDSFeed(
            metadata={"title": cls.OPDS_TITLE},
            publications=[]
        )
        fields = ["key", "title", "editions", "author_key", "author_name", "cover_i"]
        items = cls.get_enriched_items(fields=fields, offset=offset, limit=limit)
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
