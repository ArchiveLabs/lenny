from pathlib import Path
from typing import Optional
from fastapi import UploadFile, Request
from botocore.exceptions import ClientError
import socket
from pyopds2_lenny import LennyDataProvider, LennyDataRecord
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
    READER_PORT, LOAN_LIMIT
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
            email_data = auth.verify_session_cookie(session, ip)
            if not email_data:
                return {
                    "error": "unauthenticated",
                    "url": f"/v1/api/authenticate?redir={redir}",
                    "required": ["email"],
                    "message": "Not authenticated; POST to url to get a one-time-password"
                }
            email = email_data.get("email") if isinstance(email_data, dict) else email_data
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
            email_data = auth.verify_session_cookie(session_cookie)
            return email_data.get("email") if isinstance(email_data, dict) else email_data
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
    def opds_feed(cls, olid=None, offset=None, limit=None, query=None):
        """
        Generate an OPDS 2.0 catalog using the opds2 Catalog.create helper
        and the LennyDataProvider to transform Open Library metadata into
        OPDS Publications with Lenny borrow/return links.
        """
        limit = limit or cls.DEFAULT_LIMIT
        offset = offset or 0
        items = cls.get_enriched_items(olid=olid, offset=offset, limit=limit)
        if not items:
            return cls._build_empty_feed(offset=offset, limit=limit, navigation=cls._navigation(limit))
        query, lenny_ids, total = cls._build_query_and_lenny_ids(items)
        lenny_ids_map = {k: v for k, v in zip(items.keys(), lenny_ids) if v is not None}
        lenny_ids_arg = lenny_ids_map if lenny_ids_map else None
        
        # Build maps for each item's encryption and availability status
        encryption_map = {}
        borrowable_map = {}
        
        for rec in items.values():
            lenny_item = getattr(rec, "lenny", None)
            if lenny_item is None:
                continue
            try:
                edition_id = int(lenny_item.openlibrary_edition)
                encryption_map[edition_id] = lenny_item.encrypted
                borrowable_map[edition_id] = lenny_item.is_borrowable
            except (AttributeError, TypeError, ValueError):
                continue

        search_response = LennyDataProvider.search(
            query=query,
            limit=limit,
            offset=offset,
            lenny_ids=lenny_ids_arg,
            encryption_map=encryption_map,
            borrowable_map=borrowable_map,
        )
        
        if olid:
            pub = search_response.records[0].to_publication().model_dump()
            if "links" in pub:
                pub["links"].append({
                    "rel": "profile",
                    "href": f"{LennyDataProvider.BASE_URL}profile",
                    "type": "application/opds-profile+json",
                    "title": "User Profile"
                })
            return pub
        
        catalog = Catalog.create(
            search_response,
            metadata=Metadata(title=cls.OPDS_TITLE), # type: ignore
            navigation=cls._navigation(limit),
            links=[
                Link(
                    rel="http://opds-spec.org/shelf",
                    href=cls.make_url("/v1/api/shelf"),
                    type="application/opds+json",
                    title="Bookshelf"
                ),
                Link(
                    rel="profile",
                    href=cls.make_url("/v1/api/profile"),
                    type="application/opds-profile+json",
                    title="User Profile"
                )
            ]
        )
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
            Navigation(href=_href("/v1/api/opds"), title="Home", type="application/opds+json", rel="alternate"),
            Navigation(
                href=_href(f"/v1/api/opds?offset=0&limit={limit}"),
                title="Catalog",
                type="application/opds+json",
                rel="collection",
            ),
            Navigation(
                href=_href("/v1/api/oauth/implicit"),
                title="Authentication",
                type="application/opds-authentication+json",
                rel="http://opds-spec.org/auth/oauth/implicit",
            ),
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
        from pyopds2.provider import DataProvider
        from pyopds2.models import Metadata
        empty_response = DataProvider.SearchResponse(
            provider=LennyDataProvider,
            records=[],
            total=0,
            query="",
            limit=limit,
            offset=offset,
            sort=None,
        )
        catalog = Catalog.create(
            empty_response,
            navigation=navigation,
        )
        try:
            if hasattr(catalog, "metadata") and getattr(catalog.metadata, "title", None) is None:
                catalog.metadata.title = cls.cl
        except Exception:
            pass
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
        if client_ip in ("127.0.0.1", "::1"):
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
        enriched_loans = []
        for loan in loans:
            item = db.query(Item).filter(Item.id == loan.item_id).first()
            if item:
                loan.openlibrary_edition = item.openlibrary_edition
                enriched_loans.append(loan)
        return enriched_loans

    @classmethod
    def get_user_profile(cls, email: str, name: Optional[str] = None) -> dict:
        """
        Retrieves loan stats and generates the OPDS User Profile using LennyDataProvider.
        """
        current_loans = cls.get_borrowed_items(email)
        loans_count = len(current_loans)
        
        return LennyDataProvider.get_user_profile(
            name=name,
            email=email,
            active_loans_count=loans_count,
            loan_limit=LOAN_LIMIT
        )

    @classmethod
    def get_shelf_feed(cls, email: str) -> dict:
        """
        Retrieves user loans, fetches their metadata, and generates the OPDS Shelf Feed.
        """
        loans = cls.get_borrowed_items(email)
        
        if not loans:
             return LennyDataProvider.get_shelf_feed([])

        olids = [f"OL{loan.openlibrary_edition}M" for loan in loans if loan.openlibrary_edition]
        lenny_ids = {int(loan.openlibrary_edition): int(loan.openlibrary_edition) for loan in loans if loan.openlibrary_edition}
        
        if not olids:
             return LennyDataProvider.get_shelf_feed([])

        query = f"edition_key:({' OR '.join(olids)})"
        
        resp = LennyDataProvider.search(
            query=query, 
            limit=len(olids), 
            lenny_ids=lenny_ids
        )

        publications = []
        for record in resp.records:
            if isinstance(record, LennyDataRecord):
                 pub = record.to_publication().model_dump()
                 if hasattr(record, 'post_borrow_links'):
                     pub["links"] = [
                         link.model_dump(exclude_none=True) 
                         for link in record.post_borrow_links()
                     ]
                 publications.append(pub)
        
        return LennyDataProvider.get_shelf_feed(publications)

    @classmethod
    def build_oauth_fragment(cls, session_cookie: str, state: str = None) -> dict:
        """Build OAuth token fragment for redirect URL or opds:// callback."""
        auth_doc_id = quote(LennyAPI.make_url("/v1/api/oauth/implicit"), safe='')
        fragment = {
            "id": auth_doc_id,
            "access_token": session_cookie,
            "token_type": "bearer",
            "expires_in": auth.COOKIE_TTL
        }
        if state:
            fragment["state"] = state
        return fragment

    @classmethod
    async def parse_request_body(cls, request: Request) -> dict:
        """Parse request body from JSON or form data, with fallback to empty dict."""
        try:
            return await request.json()
        except:
            try:
                form = await request.form()
                return dict(form)
            except:
                return {}