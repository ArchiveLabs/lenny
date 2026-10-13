#!/usr/bin/env python

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import hashlib

from sqlalchemy import Column, String, Boolean, BigInteger, Integer, DateTime, Enum as SQLAlchemyEnum, Index, or_, text
from sqlalchemy.sql import func
from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
from lenny.core.utils import hash_email
from lenny.core.db import session as db, Base
from lenny.core.exceptions import (
    LoanNotRequiredError,
    LoanNotFoundError,
    EmailNotFoundError,
    DatabaseInsertError,
    BookUnavailableError,
    PatronLoanLimitError,
)
import enum
import datetime

class FormatEnum(enum.Enum):
    EPUB = 1
    PDF = 2
    EPUB_PDF = 3

def _xact_lock(name: str) -> None:
    """Hold an exclusive Postgres advisory lock named `name` until this transaction ends.

    The key is derived from the name rather than passed through `hashtext()`, so
    it does not depend on an undocumented server internal. A no-op on SQLite,
    which serialises writers anyway, and where the test suite runs on a single
    connection. Callers must end the transaction (commit or rollback) on every
    path, or the lock outlives the request on a pooled connection.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(
        hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def lock_editions(*olids: int) -> None:
    """Serialise writers of the same Open Library editions.

    Locks are taken in sorted order so two callers touching the same pair
    (a rename and an upload) cannot deadlock. Different books never contend.
    """
    for olid in sorted(set(olids)):
        _xact_lock(f"edition:{olid}")


def _lock_patron(hashed_email: str) -> None:
    """Hold an exclusive lock on one patron for the rest of this transaction.

    A Postgres advisory lock, because the thing being protected is a *count*
    across rows that may not exist yet, which row locks cannot express. The key
    is derived from the patron hash rather than passed through `hashtext()`, so
    it does not depend on an undocumented server internal.
    """
    _xact_lock(hashed_email)


class Item(Base):
    __tablename__ = 'items'
    __table_args__ = (
        Index('idx_items_openlibrary_edition', 'openlibrary_edition'),
        # Serves both the `modified_since` range scan and the (updated_at, id)
        # sort the OPDS feed pages by. Mirrored in alembic f3a91c47b208.
        Index('idx_items_updated_at', 'updated_at', 'id'),
    )

    # BigInteger PKs don't autoincrement on SQLite (only an INTEGER PRIMARY
    # KEY aliases the rowid) — same fix as CacheEntry.id (core/cache.py) and
    # oauth2.py's `_PK`. Only bit under TESTING via a multi-row add_all() +
    # one commit(), which uses SQLAlchemy's batched INSERT...RETURNING path;
    # single-row inserts happened to work by accident. Postgres is unaffected.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    openlibrary_edition = Column(BigInteger, nullable=False)
    encrypted = Column(Boolean, default= False, nullable=False)
    formats = Column(SQLAlchemyEnum(FormatEnum), nullable=False)
    # NULL = use the global LENNY_LOAN_DURATION_DAYS setting (configs.get_loan_duration_days()).
    loan_duration_days = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    @hybrid_property
    def is_login_required(self):
        """True if the item is encrypted and requires login."""
        return self.encrypted

    @hybrid_property
    def num_lendable_total(self):
        """Total number of lendable copies."""
        return 1

    @hybrid_property
    def available_copies(self):
        """Number of copies currently available for lending.

        This queries the Loan table for active (not returned, not expired) loans
        on this item and subtracts from `num_lendable_total`.
        """
        try:
            active_loans = db.query(Loan).filter(
                Loan.item_id == getattr(self, "id"),
                *Loan._active_filters(),
            ).count()
            available = getattr(self, "num_lendable_total", 1) - active_loans
            return max(0, int(available))
        except Exception:
            return int(getattr(self, "num_lendable_total", 1))

    @classmethod
    def available_map(cls, items) -> dict:
        """{item id: copies available now} for many items in ONE query (the per-item
        `available_copies` property asks the database once per book, too slow for a list)."""
        items = [i for i in items if i is not None]
        if not items:
            return {}
        try:
            counts = dict(db.query(Loan.item_id, func.count(Loan.id)).filter(
                Loan.item_id.in_([i.id for i in items]), *Loan._active_filters()).group_by(Loan.item_id).all())
        except Exception:    # same fallback as the per-item property: show the copy as available
            db.rollback()
            return {}
        return {i.id: max(0, int(getattr(i, "num_lendable_total", 1)) - counts.get(i.id, 0)) for i in items}

    @hybrid_property
    def is_borrowable(self):
        """True if the item currently supports borrowing (has available copies).

        This returns False for non-lendable items, otherwise True when
        `available_copies > 0`.
        """
        if not self.is_lendable:
            return False
        return self.available_copies > 0

    @hybrid_property
    def is_readable(self):
        """Publicly readable if not encrypted."""
        return not self.encrypted

    @hybrid_property
    def is_lendable(self):
        """Borrow if encrypted else not."""
        return bool(self.encrypted)

    @hybrid_property
    def is_waitlistable(self):
        """Waitlist if encrypted else not."""
        return bool(self.encrypted)

    @hybrid_property
    def is_printdisabled(self):
        """Always print disabled."""
        return True

    @classmethod
    def _select(cls, encrypted=None, modified_since=None):
        """Shared filter for the catalogue queries, so `get_many` and `count`
        can never drift apart and disagree about how many items match."""
        q = db.query(cls)
        if encrypted is not None:
            q = q.filter(cls.encrypted == encrypted)
        if modified_since is not None:
            q = q.filter(cls.updated_at >= modified_since)
        return q

    @classmethod
    def get_many(cls, offset=None, limit=None, encrypted=None, modified_since=None):
        # Ordered by `updated_at` so that incremental harvesting is coherent: a
        # consumer paging with `modified_since` walks the catalogue oldest-change
        # first and can stop when it has caught up. Without an ORDER BY, Postgres
        # is free to return rows in any order, which makes offset/limit paging
        # non-deterministic and can drop or duplicate items across pages.
        # `id` breaks ties, since a bulk import gives many rows one timestamp.
        return (
            cls._select(encrypted=encrypted, modified_since=modified_since)
            .order_by(cls.updated_at.asc(), cls.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    @classmethod
    def count(cls, encrypted=None, modified_since=None):
        """Total items matching the same filters as `get_many`, ignoring paging.

        Lets the feed report a real `numberOfItems` and decide whether to emit a
        `next` link, rather than guessing from the size of the current page.
        """
        return cls._select(encrypted=encrypted, modified_since=modified_since).count()

    @classmethod
    def exists(cls, olid):
        return db.query(Item).filter(Item.openlibrary_edition == olid).first()

    @classmethod
    def get_all(cls):
        """Return all items as {openlibrary_edition: Item} mapping."""
        items = db.query(cls).all()
        return {item.openlibrary_edition: item for item in items}

    def unborrow(self, email: str, hashed: bool = False):
        """Return a borrowed book.

        `hashed=True` means *email* is already a `hash_email()` digest — the
        OAuth2 resource endpoints (`/oauth2/return`) hold only the hash, never
        the address, exactly as `borrow` does. Forwarding the flag is what keeps
        the value from being hashed a second time and missing the loan.
        """
        if not self.is_login_required:
            raise LoanNotRequiredError

        if not email:
            raise EmailNotFoundError("Email required to return encrypted items.")

        # Use exists_any (no due_date filter) so expired loans can still be
        # explicitly returned — keeps returned_at as a patron action, not auto-expiry.
        if loan := Loan.exists_any(self.id, email, hashed=hashed):
            return loan.finalize()

        raise LoanNotFoundError("Patron has no active loan for this book.")

    def is_encrypted_item(self):
        return self.encrypted

    def borrow(self, email: str, hashed: bool = False):
        """Borrow a book for a patron.

        `hashed=True` means *email* is already a `hash_email()` digest — the
        OAuth resource endpoints hold only the hash, never the address.

        Serializes concurrent borrow attempts for the same item by acquiring a
        row-level lock (SELECT FOR UPDATE) on the Item row before any check.
        All availability and limit checks run inside the lock so the check and
        the INSERT are atomic from the database's perspective.

        Raises:
            LoanNotRequiredError: Item is open-access.
            EmailNotFoundError: No email provided.
            PatronLoanLimitError: Patron has reached their concurrent loan limit.
            BookUnavailableError: No copies available for this item.
        """
        if not self.is_login_required:
            raise LoanNotRequiredError

        if not email:
            raise EmailNotFoundError("Email is required to borrow encrypted items.")

        from lenny import configs

        hashed_email = email if hashed else hash_email(email)

        # Serialise this patron's concurrent borrows before anything is counted.
        #
        # The Item lock below is what stops one copy going to two patrons, and
        # it does nothing for the per-patron limit: N borrows of N *different*
        # editions take N different Item locks, never contend, and all read the
        # same stale count at step 2. There is no row to lock instead — a patron
        # at zero loans has no loan rows for FOR UPDATE to hold — so the lock has
        # to be keyed on the patron rather than on a row.
        #
        # Every exit below MUST end the transaction. `db` is a thread-local
        # scoped_session, and FastAPI's async handlers share one thread, so
        # concurrent requests on a worker share one connection. A borrow that
        # returns or raises still holding the item row lock leaves the next
        # request taking its advisory lock on that same transaction -- the
        # opposite order, and a real deadlock cycle: ~4% of contended borrows
        # returned HTTP 500 before the rollbacks below were added.
        _lock_patron(hashed_email)

        # Acquire row-level lock on the Item. Concurrent borrow() calls for the
        # same item block here until the current transaction commits/rolls back.
        # populate_existing=True forces a DB round-trip even if item is in the
        # session identity map, ensuring we read fresh state under the lock.
        db.query(Item).filter(
            Item.id == self.id
        ).with_for_update().populate_existing().first()

        # 1. Idempotent: patron already has an active loan for this item.
        if existing := Loan.exists(self.id, hashed_email, hashed=True):
            db.rollback()          # release both locks; nothing was written
            return existing

        # 2. Per-patron concurrent loan limit.
        patron_active = db.query(Loan).filter(
            Loan.patron_email_hash == hashed_email,
            *Loan._active_filters(),
        ).count()
        loan_limit = configs.get_loan_limit()
        if patron_active >= loan_limit:
            db.rollback()
            raise PatronLoanLimitError(
                f"Loan limit of {loan_limit} reached. Return a book before borrowing another."
            )

        # 3. Per-item copy availability (fresh COUNT under lock).
        item_active = db.query(Loan).filter(
            Loan.item_id == self.id,
            *Loan._active_filters(),
        ).count()
        if item_active >= self.num_lendable_total:
            db.rollback()
            raise BookUnavailableError("No copies available for borrowing.")

        # Close out any overdue-but-unreturned loans on this item (never active
        # ones — those are already counted in item_active above, so if one
        # existed this borrow would already have failed). A new borrow means
        # the old patron's copy is being handed to someone else now, so treat
        # it as returned rather than leaving a stale row for an admin to close.
        now = datetime.datetime.now(datetime.timezone.utc)
        db.query(Loan).filter(
            Loan.item_id == self.id,
            Loan.returned_at == None,
            Loan.due_date != None,
            Loan.due_date <= now,
        ).update({"returned_at": now}, synchronize_session=False)

        return Loan.create(self.id, hashed_email, hashed=True, duration_days=self.loan_duration_days)


class Loan(Base):
    __tablename__ = 'loans'
    __table_args__ = (
        Index('idx_loans_item_patron_returned', 'item_id', 'patron_email_hash', 'returned_at'),
        Index('idx_loans_item_returned', 'item_id', 'returned_at'),
        Index('idx_loans_due_date', 'due_date'),
        # The three below serve the admin listing (core/admin_loans.py)'s
        # unfiltered default view, status filter, and patron-search filter —
        # none of the indexes above have those columns as a leading column,
        # so without these the admin loans page does a full sequential scan
        # + full sort on every request regardless of table size.
        Index('idx_loans_created_at', 'created_at', 'id'),
        Index('idx_loans_returned_due', 'returned_at', 'due_date'),
        # varchar_pattern_ops: a plain btree index can't serve the admin
        # search's LIKE 'prefix%' scan under a non-C locale (confirmed via
        # EXPLAIN — without it, this silently falls back to a sequential scan).
        Index('idx_loans_patron_email_hash', 'patron_email_hash',
              postgresql_ops={'patron_email_hash': 'varchar_pattern_ops'}),
    )

    # Same SQLite autoincrement fix as Item.id above.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    item_id = Column(BigInteger, ForeignKey('items.id'), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    returned_at = Column(DateTime(timezone=True), nullable=True)
    due_date = Column(DateTime(timezone=True), nullable=True)  # NULL = no expiry

    item = relationship('Item', back_populates='loans')

    @classmethod
    def _active_filters(cls):
        """SQLAlchemy filter clauses for loans that are active right now:
        not manually returned AND not past their due date."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return [
            cls.returned_at == None,
            or_(cls.due_date == None, cls.due_date > now),
        ]

    @classmethod
    def exists(cls, item_id, email, hashed=False):
        """Return an active (non-returned, non-expired) loan, or None."""
        hashed_email = email if hashed else hash_email(email)
        return db.query(Loan).filter(
            Loan.item_id == item_id,
            Loan.patron_email_hash == hashed_email,
            *cls._active_filters(),
        ).first()

    @classmethod
    def exists_any(cls, item_id, email, hashed=False):
        """Return any non-returned loan (including expired), or None.
        Used by unborrow() so patrons can finalize expired loans."""
        hashed_email = email if hashed else hash_email(email)
        return db.query(Loan).filter(
            Loan.item_id == item_id,
            Loan.patron_email_hash == hashed_email,
            Loan.returned_at == None,
        ).first()

    @classmethod
    def create(cls, item_id, email, hashed=False, duration_days=None):
        """`duration_days`: per-item override (Item.loan_duration_days). None
        falls back to the global LENNY_LOAN_DURATION_DAYS setting."""
        from lenny import configs
        hashed_email = email if hashed else hash_email(email)
        due = None
        if duration_days is None:
            duration_days = configs.get_loan_duration_days()
        if duration_days > 0:
            due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=duration_days)
        try:
            loan = cls(item_id=item_id, patron_email_hash=hashed_email, due_date=due)
            db.add(loan)
            db.commit()
            return loan
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to create loan record: {str(e)}.")

    def finalize(self):
        try:
            self.returned_at = datetime.datetime.now(datetime.timezone.utc)
            db.add(self)
            db.commit()
            return self
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to return loan: {str(e)}.")

Item.loans = relationship('Loan', back_populates='item', cascade='all, delete-orphan')
