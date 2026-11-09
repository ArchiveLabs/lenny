"""Startup integrity guard for the patron-email-hash key.

Since #268 the patron email hash is HMAC-keyed from ``LENNY_SEED``
(``configs.EMAIL_HASH_KEY``), so the seed is now load-bearing for stored DATA,
not just for sessions. The two failure modes are not symmetric:

* An **absent** seed already fails loudly at import (``lenny/configs``), and the
  migration runs inside that import, so alembic rolls back. Correct; untouched.
* A **changed** seed was silent: the app started cleanly, every stored
  ``patron_email_hash`` matched nobody, and every loan and OAuth grant became
  unreachable with no error anywhere (ArchiveLabs/lenny#269).

This module stores a NON-SECRET fingerprint of the key beside the data and, on
startup, compares the live key to it: absent -> write it (first run / fresh
install), match -> proceed, mismatch -> refuse to start. A fingerprint of a key
is not the key: it is an HMAC of a fixed label under the key, truncated and
one-way, so it adds no secret to the database and the seed itself is never
stored, logged, or printed.
"""

import hashlib
import hmac
import logging

from sqlalchemy import Column, String, inspect

from lenny import configs
from lenny.core.db import Base, engine
from lenny.core.db import session as db

logger = logging.getLogger(__name__)

# Fixed label the fingerprint is computed over. Versioned so the construction
# can change deliberately in future without being mistaken for a key change.
# MUST stay identical to the literal frozen in the alembic migration that writes
# the row (alembic/versions/*_store_email_hash_key_fingerprint.py); the two are
# kept in sync by hand on purpose, the same way e2b4c6d8f0a1 freezes its hash.
_FINGERPRINT_LABEL = b"lenny:email-hash-key-fingerprint:v1"

# Well-known single-row key in the fingerprints table.
_ROW_NAME = "email_hash_key"


class KeyFingerprint(Base):
    """One non-secret fingerprint per named key, written beside the data it
    guards. Currently holds exactly one row, the patron-email-hash key."""

    __tablename__ = "key_fingerprints"

    name = Column(String(64), primary_key=True)
    fingerprint = Column(String(64), nullable=False)


class SeedMismatchError(RuntimeError):
    """Raised when the live LENNY_SEED does not match the data on disk."""


def email_hash_key_fingerprint(key: bytes) -> str:
    """Non-secret, one-way fingerprint of the patron-email-hash key.

    HMAC of a fixed label under the key, truncated. Reveals nothing usable about
    the key or the seed it is derived from; safe to store and to log.
    """
    return hmac.new(key, _FINGERPRINT_LABEL, hashlib.sha256).hexdigest()[:16]


def verify_email_hash_key(session=db) -> None:
    """Refuse to run against data written under a different LENNY_SEED.

    * table absent  -> skip (migrations have not created it yet; nothing to check)
    * row absent    -> write the live fingerprint and proceed (first run)
    * fingerprints match    -> proceed
    * fingerprints mismatch -> refuse to start

    Never stores, logs, or raises the seed or the key itself.
    """
    # A database whose migrations have not yet created the table cannot be
    # checked; skipping is correct (there is no stored fingerprint to disagree
    # with), and avoids crashing an older DB that is mid-upgrade.
    if not inspect(engine).has_table(KeyFingerprint.__tablename__):
        return

    live = email_hash_key_fingerprint(configs.EMAIL_HASH_KEY)
    row = session.get(KeyFingerprint, _ROW_NAME)
    if row is None:
        session.add(KeyFingerprint(name=_ROW_NAME, fingerprint=live))
        session.commit()
        return
    if row.fingerprint != live:
        raise SeedMismatchError(
            "This database was written with a different LENNY_SEED than the one "
            "now configured. Every patron's loans and OAuth grants are keyed to "
            "the original seed and are unreachable under this one. Do NOT run "
            "with this seed. Restore the matching LENNY_SEED from your .env "
            "backup, or restore the database backup taken before the seed "
            "changed (backups/). The seed value is not shown.")
