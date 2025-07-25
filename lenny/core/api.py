import requests
from pathlib import Path
from fastapi import UploadFile
from botocore.exceptions import ClientError
import socket
from lenny.core import db, s3
from lenny.core.models import Item, FormatEnum
from lenny.core.openlibrary import OpenLibrary
from lenny.core.exceptions import (
    ItemExistsError,
    InvalidFileError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
)
from lenny.core.opds import (
    Author,
    OPDSFeed,
    Publication,
    Link,
    OPDS_REL_ACQUISITION
)
from lenny.configs import (
    SCHEME, HOST, PORT, PROXY,
    READER_PORT
)

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
    def make_url(cls, path):
        """Constructs a public Lenny URL that points to the public HOST and PORT
        """
        if PROXY:
            return f"{PROXY}{path}"
        url = f"{SCHEME}://{HOST}"
        if PORT and PORT not in {80, 443}:
            url += f":{PORT}"
        return f"{url}{path}"

    @classmethod
    def auth_check(cls, book_id: int, email=None):
        # TODO: permission/auth checks go here
        # for userid, store/check hashed email in db?
        return True
    

    @classmethod
    def _enrich_items(cls, items, fields=None, limit=None):
        imap = dict((i.openlibrary_edition, i) for i in items)
        olids = [f"OL{i}M" for i in imap.keys()]
        if olids:
            q = f"edition_key:({' OR '.join(olids)})"
            return dict((
                # keyed by olid as int
                int(book.olid),
                # openlibrary book with item added as `lenny`
                book + {"lenny": imap[int(book.olid)]}
            ) for book in OpenLibrary.search(query=q, fields=fields))
        return {}
    
    @classmethod
    def get_enriched_items(cls, fields=None, offset=None, limit=None):
        """Returns a dict whose keys are int `olid` Open Library
        edition IDs and whose values are OpenLibraryRecords wwith an
        additional `lenny` field containing Lenny's record for this
        item in the LennyDB
        """
        limit = limit or cls.DEFAULT_LIMIT
        return cls._enrich_items(
            Item.get_many(offset=offset, limit=limit),
            fields=fields
        )

    @classmethod
    def opds_feed(cls, offset=None, limit=None):
        """
        Convert combined Lenny+OL items to OPDS 2.0 JSON feed, including borrow and return URLs.
        """
        read_uri = cls.make_url("/v1/api/items/")
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

            links = [
                Link(
                    href=f"{read_uri}{edition_id}/borrow",
                    type="application/json",
                    rel=OPDS_REL_ACQUISITION
                ),
                Link(
                    href=f"{read_uri}{edition_id}/return",
                    type="application/json",
                    rel=OPDS_REL_ACQUISITION
                )
            ]
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
        formats = 0
        for fp in files:
            if not fp.filename:
                continue

            ext = Path(fp.filename).suffix.lower()

            if ext in cls.VALID_EXTS:
                formats += cls.VALID_EXTS[ext].value
                cls.upload_file(fp, f"{filename}{ext}")
                if encrypt:
                    cls.upload_file(cls.encrypt_file(fp), f"{filename}_encrypted{ext}")
            else:
                raise InvalidFileError("Invalid format {ext} for {fp.filename}")
        if not formats:
            raise InvalidFileError("No valid files provided")
        return formats

    @classmethod
    def add(cls, openlibrary_edition: int, files: list[UploadFile], uploader_ip:str, encrypt: bool=False):
        if not cls.is_allowed_uploader(uploader_ip):
            raise UploaderNotAllowedError(f"IP {uploader_ip} not in allow list")

        if Item.exists(openlibrary_edition):
            raise ItemExistsError(f"Item '{openlibrary_edition}' already exists.")

        is_readable = not encrypt
        is_lendable = encrypt
        is_login_required = encrypt
        is_waitlistable = encrypt
        is_printdisabled = True

        if formats:= cls.upload_files(files, openlibrary_edition, encrypt=encrypt):
            try:
                item = Item(
                    openlibrary_edition=openlibrary_edition,
                    encrypted=encrypt,
                    formats=FormatEnum(formats),
                    is_readable=is_readable,
                    is_lendable=is_lendable,
                    is_login_required=is_login_required,
                    num_lendable_total=1,
                    is_waitlistable=is_waitlistable,
                    is_printdisabled=is_printdisabled,
                )
                db.add(item)
                db.commit()
                return item
            except Exception as e:
                db.rollback()
                raise DatabaseInsertError(f"Failed to add item to db: {str(e)}.")

    @staticmethod
    def hash_email(email: str) -> str:
        import hashlib
        return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
    
    @classmethod
    def read_redirect(cls, openlibrary_edition: int):
        """
        Redirects to the reader URL for a book that is open access or requires borrowing.
        """
        return cls.make_url(f"/v1/api/items/{openlibrary_edition}/read")
    
    @classmethod
    def borrow(cls, openlibrary_edition: int, email: str):
        """
        Lending: A patron finds a book, clicks "borrow", and a Loan record is created in the LennyDB
        after auth_check succeeds. DB will have a hash of the email and (optionally) the plain email for now.
        Only encrypted items can be borrowed; non-encrypted items cannot have loans created.
        """
        from lenny.core.models import Loan, Item
        item = Item.exists(openlibrary_edition)
        if item is None:
            raise Exception(f"Item with openlibrary_edition {openlibrary_edition} not found.")
        if not item.encrypted:
            raise Exception("This is a non-encrypted book, you cannot borrow it.")
        if not cls.auth_check(item.id, email):
            raise Exception("Patron is not authorized to borrow this book.")
        email_hash = cls.hash_email(email)
        if Loan.exists(item.id, email_hash):
            raise Exception("Book is already borrowed by this patron and not yet returned.")
        try:
            item.num_lendable_total -= 1
            item.is_lendable = item.num_lendable_total > 0
            db.commit() 
            loan = Loan(
                item_id=item.id,
                patron_email_hash=email_hash,
            )
            db.add(loan)
            db.commit()
            return loan
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to create loan record: {str(e)}.")
        
    @classmethod
    def borrow_redirect(cls, openlibrary_edition: int, email: str):
        """
        Borrows a book and redirects user to the reader if successful.
        """
        loan = cls.borrow(openlibrary_edition, email)
        if loan is None:
            raise Exception("Failed to borrow book.")
        return cls.read_redirect(openlibrary_edition)
    

    @classmethod
    def checkout_items(cls, openlibrary_editions: list, email: str):
        """
        Checks out multiple books for a patron.
        Returns a list of Loan objects. Rolls back all if any fail.
        """
        loans = []
        try:
            for openlibrary_edition in openlibrary_editions:
                loan = cls.borrow(openlibrary_edition, email)
                loans.append(loan)
            return loans
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to borrow one or more books: {str(e)}.")

    @classmethod
    def get_borrowed_items(cls, email: str):
        """
        Returns a list of active (not returned) Loan objects for the given user email.
        Ensures openlibrary_edition is set for each loan.
        """
        from lenny.core.models import Loan, Item
        email_hash = cls.hash_email(email)
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
    def return_items(cls, openlibrary_edition: int, email: str):
        """
        Marks a loan as returned for the patron and book.
        Returns the updated Loan object if successful. Rolls back on error.
        """
        from lenny.core.models import Loan, Item
        item =  Item.exists(openlibrary_edition)
        if item is None:
            raise Exception(f"Item with openlibrary_edition {openlibrary_edition} not found.")
        email_hash = cls.hash_email(email)
        loan = Loan.exists(item.id, email_hash)
        if not loan:
            raise Exception("No active loan found for this patron and book.")
        
        import datetime
        try:
            num_lendable_total = item.num_lendable_total + 1
            item.num_lendable_total = num_lendable_total
            item.is_lendable = num_lendable_total > 0
            db.commit()  
            
            loan.returned_at = datetime.datetime.utcnow()
            db.commit()
            return loan
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to return loan: {str(e)}.")
    
    @classmethod
    def borrow_redirect_url(cls, openlibrary_edition: int, email: str):
        """
        Redirect URL for borrowing a book.
        """
        return cls.make_url(f"/v1/api/items/{openlibrary_edition}/borrow?email={email}")
        
    @classmethod
    def open_access_redirect_url(cls, openlibrary_edition: int,):
        """
        Open access URL redirect for a book reader.
        """
        return cls.make_url(f"/v1/api/items/{openlibrary_edition}/read")
        
    @classmethod
    def open_access(cls, openlibrary_edition: int, email: str):
        item = Item.exists(openlibrary_edition)
        if not item:
            raise Exception(f"Item with openlibrary_edition {openlibrary_edition} not found.")
        if item.encrypted and item.is_readable is False:
            return cls.borrow_redirect_url(openlibrary_edition, email)
        if item.is_login_required and item.is_readable is True:
            login = cls.auth_check(item.id, email)
            if not login:
                raise Exception("Patron is not authorized to read this book.")
            return cls.open_access_redirect_url(openlibrary_edition)
        else:
            return cls.open_access_redirect_url(openlibrary_edition)
        
    @classmethod
    def get_item(cls, openlibrary_edition: int):
        """
        Returns the Item object for the given Open Library edition ID.
        """
        item = Item.exists(openlibrary_edition)
        if item is None:
            raise Exception(f"Item with openlibrary_edition {openlibrary_edition} not found.")
        return item
        
    @classmethod
    def has_active_loan(cls, openlibrary_edition: int, email: str) -> bool:
        """
        Checks if the user has an active loan for the given book (by OpenLibrary edition).
        Returns True if an active loan exists, False otherwise.
        """
        from lenny.core.models import Loan, Item
        item = Item.exists(openlibrary_edition)
        if not item:
            return False
        email_hash = cls.hash_email(email)
        return Loan.exists(item.id, email_hash) is not None

