"""
    PostgreSQL-backed cache system for Lenny.

    Provides a shared cache across multiple Uvicorn workers using
    the existing PostgreSQL database and SQLAlchemy ORM.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import logging
import random
from datetime import datetime, timedelta, timezone

from fastapi import Request, HTTPException
from sqlalchemy import Column, String, BigInteger, DateTime, Index
from sqlalchemy.sql import func

from lenny.core.db import session as db, Base
from lenny.core.exceptions import DatabaseInsertError

logger = logging.getLogger(__name__)

PURGE_PROBABILITY = 0.01  # 1-in-100 chance per rate limit check


class CacheEntry(Base):
    __tablename__ = 'cache'
    __table_args__ = (
        Index('idx_cache_scope_key_expires', 'scope', 'key', 'expires_at'),
        Index('idx_cache_expires', 'expires_at'),
        {'prefixes': ['UNLOGGED']},
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
            return db.query(CacheEntry).filter(
                CacheEntry.scope == scope,
                CacheEntry.key == key,
                CacheEntry.expires_at > now,
            ).count()
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
    def get(cls, scope, key):
        """Return the most recent unexpired entry's value, or None."""
        try:
            now = datetime.now(timezone.utc)
            entry = db.query(CacheEntry).filter(
                CacheEntry.scope == scope,
                CacheEntry.key == key,
                CacheEntry.expires_at > now,
            ).order_by(CacheEntry.created_at.desc()).first()
            return entry.value if entry else None
        except Exception as e:
            db.rollback()
            logger.warning(f"Cache get failed: {str(e)}")
            return None

    @classmethod
    def set(cls, scope, key, value, ttl):
        """Set a single-value cache entry, replacing any existing ones."""
        try:
            now = datetime.now(timezone.utc)
            db.query(CacheEntry).filter(
                CacheEntry.scope == scope,
                CacheEntry.key == key,
            ).delete()
            entry = CacheEntry(
                scope=scope,
                key=key,
                value=value,
                expires_at=now + timedelta(seconds=ttl),
            )
            db.add(entry)
            db.commit()
            return entry
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to set cache entry: {str(e)}")

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


class RateGuard:
    """FastAPI dependency for IP-based rate limiting.

    Usage:
        @router.get("/endpoint", dependencies=[Depends(RateGuard("api", limit=10, ttl=60))])
        async def endpoint():
            ...
    """

    def __init__(self, scope: str, limit: int, ttl: int):
        self.scope = scope
        self.limit = limit
        self.ttl = ttl

    def __call__(self, request: Request):
        client_ip = (
            request.headers.get("X-Real-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip().split(":")[0]
            or (request.client.host if request.client else None)
        )
        if not client_ip:
            return
        if Cache.is_throttled(self.scope, client_ip, self.limit, self.ttl):
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
            )
