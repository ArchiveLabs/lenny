from pathlib import Path
from typing import Optional
from fastapi import UploadFile, Request
from botocore.exceptions import ClientError
import socket
from pyopds2_lenny import LennyDataProvider
from pyopds2 import Catalog, Metadata
from pyopds2.models import Link, Navigation
from lenny.core import db, s3, auth
from lenny.core.utils import hash_email
from lenny.core.models import Item, FormatEnum, Loan
from lenny.core.openlibrary import OpenLibrary
from lenny.core.exceptions import (
    ItemExistsError,
    InvalidFileError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
    EmailNotFoundError,
    ItemNotFoundError,
    LoanNotFoundError
)

from lenny.configs import (
    SCHEME, HOST, PORT, PROXY,
    READER_PORT
)
from urllib.parse import quote

def _make_url(path):
    if PROXY:
        return f"{PROXY}{path}"
    url = f"{SCHEME}://{HOST}"
    if PORT and PORT not in {80, 443}:
        url += f":{PORT}"
    return f"{url}{path}"

LennyDataProvider.BASE_URL = _make_url("/v1/api/")

class LennyAPI:

    DEFAULT_LIMIT = 50
    OPDS_TITLE = "Lenny Catalog"
    MAX_FILE_SIZE = 50 * 1024 * 1024
    VALID_EXTS = {
        ".pdf": FormatEnum.PDF,
        ".epub": FormatEnum.EPUB
    }
    Item = Item
    
    @classmethod
    def make_manifest_url(cls, book_id):
        return cls.make_url(f"/v1/api/items/{book_id}/readium/manifest.json")
    
    @classmethod
    def encoded_manifest_url(cls, book_id):
        manifest_uri = cls.make_manifest_url(book_id)
        return quote(manifest_uri, safe='')

    @classmethod
    def make_url(cls, path):
        """Constructs a public Lenny URL that points to the public HOST and PORT
        """
        
        return _make_url(path)

    @classmethod
    def auth_check(cls, item, session: str=None, request: Request=None):
        """
        Checks if the user is allowed to access the book.
        """
        success = {"success": "authenticated"}
        ip = request.client.host
        redir = request.url.path

        if item.is_login_required:
            if not (email := auth.verify_session_cookie(session, ip)):
                return {
                    "error": "unauthenticated",
                    "url": f"/v1/api/authenticate?redir={redir}",
                    "required": ["email"],
                    "message": "Not authenticated; POST to url to get a one-time-password"
                }
            success['email'] = email
            if not (loan := item.borrow(email)):
                return {
                    "error": "unauthorized",
                    "url": f"/v1/api/items/{item.openlibrary_edition}/borrow",
                    "message": "Book must be borrowed before being read"
                }
        return success
    
    @classmethod
    def make_session_cookie(cls, email: str):
        """Compatibility wrapper: create a session cookie using auth helpers."""
        return auth.create_session_cookie(email)

    @classmethod
    def validate_session_cookie(cls, session_cookie: str):
        """Validates the session cookie and returns the email if valid."""
        if session_cookie:
            return auth.verify_session_cookie(session_cookie)
        return None

    @classmethod
    def _enrich_items(cls, items, fields=None, limit=None):
        imap = dict((i.openlibrary_edition, i) for i in items)
        olids = [f"OL{i}M" for i in imap.keys()]
        if olids:
            q = f"edition_key:({' OR '.join(olids)})"
            return dict((
                int(book.olid),
                book + {"lenny": imap[int(book.olid)]}
            ) for book in OpenLibrary.search(query=q, fields=fields))
        return {}
    
    @classmethod
    def get_enriched_items(cls, olid=None, fields=None, offset=None, limit=None):
        """Returns a dict whose keys are int `olid` Open Library
        edition IDs and whose values are OpenLibraryRecords wwith an
        additional `lenny` field containing Lenny's record for this
        item in the LennyDB
        """
        limit = limit or cls.DEFAULT_LIMIT
        items = [Item.exists(olid)] if olid else Item.get_many(offset=offset, limit=limit)
        return cls._enrich_items(items, fields=fields)

    @classmethod
    def opds_feed(cls, olid=None, offset=None, limit=None):
        """
        Generate an OPDS 2.0 catalog using the opds2 Catalog.create helper
        and the LennyDataProvider to transform Open Library metadata into
        OPDS Publications with Lenny borrow/return links.
        """
        limit = limit or cls.DEFAULT_LIMIT
        offset = offset or 0
        navigation = cls._navigation(limit)
        items = cls.get_enriched_items(olid=olid, offset=offset, limit=limit)
        if not items:
            return cls._build_empty_feed(offset=offset, limit=limit, navigation=navigation)
        query, lenny_ids, total = cls._build_query_and_lenny_ids(items)
        records, numfound = LennyDataProvider.search(
            query=query,
            numfound=total,
            limit=limit,
            offset=offset,
            lenny_ids=lenny_ids,
        )
        if olid:
            return records[0].to_publication().model_dump()
        catalog = Catalog.create(
            metadata=Metadata(title=cls.OPDS_TITLE, numberOfItems=numfound),
            publications=[r.to_publication() for r in records],
            navigation=navigation,
        )
        catalog.links = cls._catalog_links(offset, limit)
        return catalog.model_dump()

    @classmethod
    def _navigation(cls, limit: Optional[int]):
        """Return a minimal OPDS navigation array as list[dict].
        Includes a Home link (HTML) and a Catalog link (OPDS JSON).
        Using dicts keeps it compatible with both Catalog.create (Pydantic will
        coerce to Navigation) and provider-returned dict feeds.
        """
        limit = limit or cls.DEFAULT_LIMIT
        def _href(path: str) -> str:
            return cls.make_url(path)
        return [
            Navigation(href=_href("/"), title="Home", type="text/html"),
            Navigation(
                href=_href(f"/v1/api/opds?offset=0&limit={limit}"),
                title="Catalog",
                type="application/opds+json",
                rel="collection",
            ),
        ]

    @classmethod
    def _catalog_links(cls, offset: int, limit: int):
        """Return standard self/next links for this catalog page."""
        def _href(path: str) -> str:
            return cls.make_url(path)
        return [
            Link(rel="self", href=_href(f"/v1/api/opds?offset={offset}&limit={limit}")),
            Link(rel="next", href=_href(f"/v1/api/opds?offset={offset + limit}&limit={limit}")),
        ]

    @classmethod
    def _build_query_and_lenny_ids(cls, items):
        """Create Open Library query and determine lenny_ids alignment."""
        olids = [f"OL{olid}M" for olid in items.keys()]
        query = f"edition_key:({' OR '.join(olids)})" if olids else ""
        lenny_ids: list[Optional[int]] = []
        for olid, rec in items.items():
            try:
                lenny_ids.append(int(getattr(rec, "lenny").openlibrary_edition))
            except Exception:
                lenny_ids.append(int(olid) if isinstance(olid, int) else None)
        total = len(lenny_ids)
        return query, lenny_ids, total

    @classmethod
    def _build_empty_feed(cls, offset: int, limit: int, navigation):
        """Create an empty OPDS catalog via opds2 with local links + navigation."""
        catalog = Catalog.create(
            metadata=Metadata(title=cls.OPDS_TITLE),
            publications=[],
            navigation=navigation,
        )
        catalog.links = cls._catalog_links(offset, limit)
        return catalog.model_dump()

        catalog.links = cls._catalog_links(offset, limit)
        return catalog.model_dump()

    @classmethod
    def encrypt_file(cls, f, method="lcp"):
        # XXX Not Implemented
        return f

    @classmethod
    def _resolve_ip_to_hostname(cls, client_ip: str) -> str:
        try:
            # Reverse DNS lookup
            client_hostname, _, _ = socket.gethostbyaddr(client_ip)
        except socket.herror:
            return None
    
    @classmethod
    def is_allowed_uploader(cls, client_ip: str) -> bool:
        if client_ip in ("127.0.0.1", "::1") or client_ip.startswith("172.18.0."): # localhost and docker internal
            return True

        if host := cls._resolve_ip_to_hostname(client_ip):
            for allowed_host in ["localhost", "openlibrary.press"]:
                if host == allowed_host or host.endswith(allowed_host):
                    return True
        return False

    @classmethod
    def upload_file(cls, fp, filename):
        if not fp.size or fp.size > cls.MAX_FILE_SIZE:
            one_mb = (1024 * 1024)
            raise FileTooLargeError(
                f"{fp.filename} exceeds {cls.MAX_FILE_SIZE // one_mb}MB."
            )
        fp.file.seek(0)

        try:
            return s3.upload_fileobj(
                fp.file,
                s3.BOOKSHELF_BUCKET,
                filename,
                ExtraArgs={'ContentType': fp.content_type}
            )
        except ClientError as e:
            raise S3UploadError(
                f"Failed to upload '{fp.filename}' to S3: "
                f"{e.response.get('Error', {}).get('Message', str(e))}."
            )
        except ValueError as e:
            raise S3UploadError(
                f"File '{fp.filename}' is closed or unreadable: {e}"
            )
    
    @classmethod
    def upload_files(cls, files: list[UploadFile], filename, encrypt=False):
        from io import BytesIO
        formats = 0
        for fp in files:
            if not fp.filename:
                continue

            ext = Path(fp.filename).suffix.lower()

            if ext in cls.VALID_EXTS:
                formats += cls.VALID_EXTS[ext].value
                
                if encrypt:
                    fp.file.seek(0)
                    file_content = fp.file.read()
                    
                    fp.file.seek(0)
                    cls.upload_file(fp, f"{filename}{ext}")
                    
                    encrypted_fp = BytesIO(file_content)
                    class TempFile:
                        def __init__(self, file, filename, content_type, size):
                            self.file = file
                            self.filename = filename
                            self.content_type = content_type
                            self.size = size
                    
                    temp_file = TempFile(
                        cls.encrypt_file(encrypted_fp),
                        fp.filename,
                        fp.content_type,
                        fp.size
                    )
                    cls.upload_file(temp_file, f"{filename}_encrypted{ext}")
                else:
                    cls.upload_file(fp, f"{filename}{ext}")
            else:
                raise InvalidFileError("Invalid format {ext} for {fp.filename}")
        if not formats:
            raise InvalidFileError("No valid files provided")
        return formats

    @classmethod
    def add(cls, openlibrary_edition: int, files: list[UploadFile], uploader_ip:str, encrypt: bool=False):
        """Adds a book into s3 and the database"""
        if not cls.is_allowed_uploader(uploader_ip):
            raise UploaderNotAllowedError(f"IP {uploader_ip} not in allow list")

        if Item.exists(openlibrary_edition):
            raise ItemExistsError(f"Item '{openlibrary_edition}' already exists.")

        if formats:= cls.upload_files(files, openlibrary_edition, encrypt=encrypt):
            try:
                item = Item(
                    openlibrary_edition=openlibrary_edition,
                    encrypted=encrypt,
                    formats=FormatEnum(formats)
                )
                db.add(item)
                db.commit()
                return item
            except Exception as e:
                db.rollback()
                raise DatabaseInsertError(f"Failed to add item to db: {str(e)}.")

    @classmethod
    def get_borrowed_items(cls, email: str):
        """
        Returns a list of active (not returned) Loan objects for the given user email.
        Ensures openlibrary_edition is set for each loan.
        """
        email_hash = hash_email(email)
        loans = db.query(Loan).filter(
            Loan.patron_email_hash == email_hash,
            Loan.returned_at == None
        ).all()
        # Always enrich with openlibrary_edition and filter out loans with missing items
        enriched_loans = []
        for loan in loans:
            item = db.query(Item).filter(Item.id == loan.item_id).first()
            if item:
                loan.openlibrary_edition = item.openlibrary_edition
                enriched_loans.append(loan)
        return enriched_loans
