#!/usr/bin/env python

"""
    Import job state for Lenny.

    Books normally appear in the catalog only once they are fully ingested
    (S3 + DB row committed) — there is no observable in-between. This module
    records the in-flight state of a book so the admin UI can show what is
    still downloading, and what failed.

    It is deliberately source-agnostic: BRIET is the first producer, but
    `preload.py` (Standard Ebooks) can adopt it without changes here.

    ponytail: state lives in the existing Cache table (7 day TTL, no status
    index) rather than a new table + migration. Move to a real `imports` table
    if these records ever need auditing or need to outlive a week.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import json
import logging
from datetime import datetime, timezone

from lenny.core.cache import Cache, CacheEntry
from lenny.core.db import session as db

logger = logging.getLogger(__name__)

SCOPE = "import"
TTL = 7 * 24 * 3600  # a week is long enough for an admin to notice a failure

PENDING = "pending"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"

# CacheEntry.value is String(1024); leave headroom for the rest of the blob.
_MAX_ERROR = 400


class ImportJob:
    """Per-book import state, keyed by ``{source}:{olid}``."""

    @classmethod
    def record(cls, source: str, olid: int, status: str, error: str = None) -> None:
        """Set the current state of one book. Last write wins.

        Cache._record only ever INSERTs, so the prior row for this key is
        deleted first — otherwise every status transition leaves a duplicate
        behind and `list()` reports the same book twice.
        """
        key = f"{source}:{olid}"
        value = json.dumps({
            "source": source,
            "olid": olid,
            "status": status,
            "error": (error or "")[:_MAX_ERROR] or None,
        })
        try:
            db.query(CacheEntry).filter(
                CacheEntry.scope == SCOPE,
                CacheEntry.key == key,
            ).delete()
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not clear prior import state for {key}: {e}")

        try:
            Cache._record(SCOPE, key, TTL, value=value)
        except Exception as e:
            # Losing progress state must never abort an import in flight.
            logger.warning(f"Could not record import state for {key}: {e}")

    @classmethod
    def list(cls) -> list:
        """Every unexpired import record, newest first."""
        try:
            now = datetime.now(timezone.utc)
            rows = db.query(CacheEntry).filter(
                CacheEntry.scope == SCOPE,
                CacheEntry.expires_at > now,
            ).order_by(CacheEntry.created_at.desc()).all()
            db.rollback()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not list import state: {e}")
            return []

        imports = []
        for row in rows:
            try:
                record = json.loads(row.value)
            except (TypeError, ValueError):
                continue
            record["updated_at"] = row.created_at.isoformat() if row.created_at else None
            imports.append(record)
        return imports
