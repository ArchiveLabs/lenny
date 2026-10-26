"""
    PostgreSQL-backed cache for Lenny.

    Used for OTP email-based rate limiting across multiple Uvicorn workers.
    IP-based rate limiting is handled by nginx (limit_req).

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, String, BigInteger, Integer, DateTime, Index, text
from sqlalchemy.sql import func

from lenny.core.db import session as db, Base
from lenny.core.exceptions import DatabaseInsertError
from lenny import configs

logger = logging.getLogger(__name__)

PURGE_PROBABILITY = 0.01  # 1-in-100 chance per rate limit check

# UNLOGGED tables skip WAL for faster writes — ideal for ephemeral cache.
# SQLite (used in tests) doesn't support UNLOGGED, so we only apply it on PostgreSQL.
_cache_table_opts = {'prefixes': ['UNLOGGED']} if not configs.TESTING else {}


class CacheEntry(Base):
    __tablename__ = 'cache'
    __table_args__ = (
        Index('idx_cache_scope_key_expires', 'scope', 'key', 'expires_at'),
        Index('idx_cache_expires', 'expires_at'),
        _cache_table_opts,
    )

    # BigInteger PKs don't autoincrement on SQLite (only INTEGER aliases rowid),
    # so inserts fail under TESTING. Postgres still gets BIGSERIAL.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    scope = Column(String(64), nullable=False)
    key = Column(String(255), nullable=False)
    value = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


def _lock_throttle_key(scope: str, key: str) -> None:
    """Hold an exclusive lock on one (scope, key) for the rest of this
    transaction — same technique as models.py's _lock_patron, and the same
    reason: the thing being protected is a *count* across rows that may not
    exist yet, which row locks can't express. Without this, two callers
    hitting is_throttled for the same key at nearly the same instant can
    both read the pre-record count, both conclude they're under the limit,
    and both proceed — letting the limit be exceeded in exactly the case it
    exists to prevent (e.g. two imports starting at once).

    A no-op on SQLite, which serializes writers anyway, and where the test
    suite runs on a single connection.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(
        hashlib.sha256(f"{scope}:{key}".encode("utf-8")).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


class Cache:
    """PostgreSQL-backed cache with built-in rate limiting."""

    @classmethod
    def _record(cls, scope, key, ttl, value=None):
        """Insert a cache entry that expires after ttl seconds."""
        try:
            entry = CacheEntry(
                scope=scope,
                key=key,
                value=value,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            )
            db.add(entry)
            db.commit()
            return entry
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to record cache entry: {str(e)}")

    @classmethod
    def _count(cls, scope, key):
        """Count unexpired entries for a given scope and key."""
        try:
            now = datetime.now(timezone.utc)
            count = db.query(CacheEntry).filter(
                CacheEntry.scope == scope,
                CacheEntry.key == key,
                CacheEntry.expires_at > now,
            ).count()
            db.rollback()
            return count
        except Exception as e:
            db.rollback()
            logger.warning(f"Cache count failed: {str(e)}")
            return 0

    @classmethod
    def is_throttled(cls, scope, key, limit, ttl, fail_closed=False):
        """Check if a key has exceeded its rate limit.

        Counts existing unexpired entries, then records the current
        attempt. Returns True if count >= limit (before recording).
        Only records the attempt if not already throttled.

        The count and the conditional record happen under
        _lock_throttle_key, in one uninterrupted transaction — count and
        record must not be split across separate commits/rollbacks here,
        since a Postgres advisory lock releases at transaction end, and an
        early release is the same race this exists to close.

        If the cache itself fails (count or record), `fail_closed=True` reports
        the key as throttled instead of letting the attempt through. Use it
        where the limit is a security control (OTP guesses and sends): a
        database fault must not become unlimited guesses.
        """
        _lock_throttle_key(scope, key)

        now = datetime.now(timezone.utc)
        try:
            current_count = db.query(CacheEntry).filter(
                CacheEntry.scope == scope,
                CacheEntry.key == key,
                CacheEntry.expires_at > now,
            ).count()
        except Exception as e:
            db.rollback()
            logger.warning(f"Cache count failed: {str(e)}")
            if fail_closed:
                return True
            current_count = 0

        if current_count < limit:
            try:
                db.add(CacheEntry(
                    scope=scope, key=key, value=None,
                    expires_at=now + timedelta(seconds=ttl),
                ))
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Cache record failed: {str(e)}")
                if fail_closed:
                    return True
        else:
            db.commit()  # nothing written — releases the lock

        if random.random() < PURGE_PROBABILITY:
            cls.purge()

        return current_count >= limit

    @classmethod
    def purge(cls):
        """Delete all expired cache entries."""
        try:
            now = datetime.now(timezone.utc)
            deleted = db.query(CacheEntry).filter(
                CacheEntry.expires_at < now,
            ).delete()
            db.commit()
            if deleted:
                logger.debug(f"Cache purge: removed {deleted} expired entries")
        except Exception as e:
            db.rollback()
            logger.warning(f"Cache purge failed: {str(e)}")
