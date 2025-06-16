
import requests
from pathlib import Path
from fastapi import UploadFile
from botocore.exceptions import ClientError
from lenny.core import db, s3
from lenny.core.models import Item, FormatEnum
from lenny.core.openlibrary import OpenLibrary
from lenny.core.exceptions import (
    ItemExistsError,
    InvalidFileError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
)
from lenny.core.opds import (
    Author,
    OPDSFeed,
    Publication,
    Link,
    OPDS_REL_ACQUISITION
)
from lenny.configs import (
    SCHEME, HOST, PORT,
    READER_PORT,
)


class LennyAPI:

    OPDS_TITLE = "Lenny Catalog"
    MAX_FILE_SIZE = 50 * 1024 * 1024
    VALID_EXTS = {
        ".pdf": FormatEnum.PDF,
        ".epub": FormatEnum.EPUB
    }
    Item = Item
    
    @classmethod
    def make_manifest_url(cls, book_id):
        return cls.make_url(f"/v1/api/items/{book_id}/manifest.json")

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
    def auth_check(cls, book_id: int, email=None):
        # TODO: permission/auth checks go here
        # for userid, store/check hashed email in db?
        return True

    @classmethod
    def _enrich_items(cls, items, fields=None):
        items = Item.get_many(offset=None, limit=None)
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
        """Returns a dict whose keys are int `olid` Open Library
        edition IDs and whose values are OpenLibraryRecords wwith an
        additional `lenny` field containing Lenny's record for this
        item in the LennyDB
        """
        return cls._enrich_items(
            Item.get_many(offset=offset, limit=limit),
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
    def encrypt_file(cls, f, method="lcp"):
        # XXX Not Implemented
        return f

    @classmethod
    def upload_file(cls, fp, filename):
        if not fp.size or fp.size > cls.MAX_FILE_SIZE:
            raise FileTooLargeError(
                f"{fp.filename} exceeds {cls.MAX_FILE_SIZE // (1024 * 1024)}MB."
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
    
    @classmethod
    def upload_files(cls, files: list[UploadFile], filename, encrypt=False):
        formats = 0
        for fp in files:
            if not fp.filename:
                continue

            ext = Path(fp.filename).suffix.lower()

            if ext in cls.VALID_EXTS:
                formats += cls.VALID_EXTS[ext].value

                # Upload the unencrypted file to s3
                cls.upload_file(fp, f"{filename}{ext}")
                if encrypt:
                    cls.upload_file(cls.encrypt_file(fp), f"{filename}_encrypted{ext}")
            else:
                raise InvalidFileError("Invalid format {ext} for {fp.filename}")
        if not formats:
            raise InvalidFileError("No valid files provided")
        return formats

    @classmethod
    def add(cls, openlibrary_edition: int, files: list[UploadFile], encrypt: bool=False):
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
