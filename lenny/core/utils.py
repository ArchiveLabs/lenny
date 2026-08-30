import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, Union

logger = logging.getLogger(__name__)

def encode_book_path(book_id: str, format=".epub") -> str:
    """This should be moved to a general utils.py within core"""
    if not "." in book_id:
        book_id += format
    path = f"s3://bookshelf/{book_id}"
    logger.info(f"path: {path}")
    encoded = base64.b64encode(path.encode()).decode()
    return encoded.replace('/', '_').replace('+', '-').replace('=', '')

def hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()


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
