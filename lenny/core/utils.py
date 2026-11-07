import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional, Union

logger = logging.getLogger(__name__)

# Real EPUBs carry a few thousand entries at most. The cap exists because
# zipfile loads the whole central directory into memory, so a hostile zip
# claiming millions of entries could exhaust a small machine.
EPUB_MAX_ENTRIES = 20_000
EPUB_MIMETYPE = b"application/epub+zip"


def is_valid_epub(fp) -> bool:
    """True if `fp` (a seekable binary file) is a zip holding an EPUB `mimetype` entry.

    Reads the 22-byte end-of-central-directory record first so the entry count
    is checked before zipfile parses the directory, then reads at most
    len(EPUB_MIMETYPE)+1 bytes of the `mimetype` entry. Leaves `fp` at 0.
    """
    import struct
    import zipfile
    import zlib
    try:
        fp.seek(0, 2)
        size = fp.tell()
        fp.seek(max(0, size - (22 + 65535)))
        tail = fp.read()
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0 or len(tail) - eocd < 22:
            return False
        entries = struct.unpack("<H", tail[eocd + 10:eocd + 12])[0]
        if entries == 0 or entries >= 0xFFFF or entries > EPUB_MAX_ENTRIES:
            return False
        fp.seek(0)
        with zipfile.ZipFile(fp) as zf:
            with zf.open("mimetype") as m:
                return m.read(len(EPUB_MIMETYPE) + 1) == EPUB_MIMETYPE
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, NotImplementedError,
            RuntimeError, zlib.error, EOFError):
        return False
    finally:
        try:
            fp.seek(0)
        except (OSError, ValueError):
            pass

def encode_book_path(book_id: str, format=".epub") -> str:
    """This should be moved to a general utils.py within core"""
    if not "." in book_id:
        book_id += format
    path = f"s3://bookshelf/{book_id}"
    logger.info(f"path: {path}")
    encoded = base64.b64encode(path.encode()).decode()
    return encoded.replace('/', '_').replace('+', '-').replace('=', '')

def hash_email(email: str) -> str:
    """Patron identity: HMAC-SHA256 keyed with LENNY_EMAIL_HASH_KEY over sha256(normalized email).

    The inner sha256 hex is what Lenny stored before the hash was keyed, so the
    migration (alembic e2b4c6d8f0a1) can rewrite old rows as HMAC(key, old).
    """
    from lenny import configs  # read at call time so tests can swap the key
    digest = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()
    return hmac.new(configs.EMAIL_HASH_KEY.encode('utf-8'), digest.encode('ascii'), hashlib.sha256).hexdigest()


def parse_modified_since(value: Union[str, datetime, None]) -> Optional[datetime]:
    """Parse an OPDS `modified_since` filter into a timezone-aware UTC datetime.

    Accepts a bare date (`2026-08-01`) — which is what Open Library's BookWorm
    harvester sends, via `since.date().isoformat()` in
    `openlibrary/bookworm/harvest.py` — as well as a full ISO 8601 timestamp with
    a `Z` suffix or an explicit offset. A value with no timezone is read as UTC,
    so a bare date means midnight UTC on that day.

    Returns None for None/blank. Raises ValueError on anything unparseable, so
    callers can turn a bad filter into a 400 rather than silently serving the
    whole catalogue as if no filter had been asked for.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        if not (raw := value.strip()):
            return None
        # `datetime.fromisoformat` only accepts a `Z` suffix from Python 3.11 on.
        # Normalize it ourselves so behaviour does not depend on the interpreter.
        if raw[-1] in "Zz":
            raw = f"{raw[:-1]}+00:00"
        dt = datetime.fromisoformat(raw)
    return (
        dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Render a datetime as the ISO 8601 UTC string OPDS wants for `modified`.

    Returns None when there is no timestamp, so callers can omit the key rather
    than emit a null. Naive datetimes are assumed UTC, matching how
    `parse_modified_since` reads them back.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
