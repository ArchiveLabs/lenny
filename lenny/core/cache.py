"""
    PostgreSQL-backed cache for Lenny.

    Used for OTP email-based rate limiting across multiple Uvicorn workers.
    IP-based rate limiting is handled by nginx (limit_req).

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, String, BigInteger, DateTime, Index
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

    id = Column(BigInteger, primary_key=True)
    scope = Column(String(64), nullable=False)
    key = Column(String(255), nullable=False)
    value = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


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
    def is_throttled(cls, scope, key, limit, ttl):
        """Check if a key has exceeded its rate limit.

        Counts existing unexpired entries, then records the current
        attempt. Returns True if count >= limit (before recording).
        Only records the attempt if not already throttled.
        """
        current_count = cls._count(scope, key)

        if current_count < limit:
            try:
                cls._record(scope, key, ttl)
            except DatabaseInsertError:
                pass

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
