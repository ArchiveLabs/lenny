#!/usr/bin/env python 

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy  import Column, String, Boolean, BigInteger, Integer, DateTime, Enum as SQLAlchemyEnum
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
    DatabaseInsertError
)
import enum
import datetime

class FormatEnum(enum.Enum):
    EPUB = 1
    PDF = 2
    EPUB_PDF = 3

class Item(Base):
    __tablename__ = 'items'
    
    id = Column(BigInteger, primary_key=True)
    openlibrary_edition = Column(BigInteger, nullable=False)
    encrypted = Column(Boolean, default= False, nullable=False)
    formats = Column(SQLAlchemyEnum(FormatEnum), nullable=False)
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

        This queries the Loan table for active (not returned) loans
        on this item and subtracts from `num_lendable_total`.
        """
        try:
            active_loans = db.query(Loan).filter(
                Loan.item_id == getattr(self, "id"),
                Loan.returned_at == None
            ).count()
            available = getattr(self, "num_lendable_total", 1) - active_loans
            return max(0, int(available))
        except Exception:
            return int(getattr(self, "num_lendable_total", 1))

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
    def exists(cls, olid):
        return db.query(Item).filter(Item.openlibrary_edition == olid).first()

    def unborrow(self, email: str):
        if not self.is_login_required:
            raise LoanNotRequiredError

        if not email:
            raise EmailNotFoundError("Email required to borrow encrypted items.")

        if loan := Loan.exists(self.id, email):
            return loan.finalize()

        raise LoanNotFoundError("Patron has no active loan for this book.")
    
    def is_encrypted_item(self):
        return self.encrypted

    def borrow(self, email: str):
        """
        Borrows a book for a patron. Returns the Loan object if successful.
        """
        if not self.is_login_required:
            raise LoanNotRequiredError
        
        if not email:
            raise EmailNotFoundError("Email is required to borrow encrypted items.")

        hashed_email = hash_email(email)
        if active_loan := Loan.exists(self.id, hashed_email, hashed=True):
            return active_loan
        return Loan.create(self.id, hashed_email, hashed=True)


class Loan(Base):
    __tablename__ = 'loans'

    id = Column(BigInteger, primary_key=True)
    item_id = Column(BigInteger, ForeignKey('items.id'), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    returned_at = Column(DateTime(timezone=True), nullable=True)

    item = relationship('Item', back_populates='loans')
    
    @classmethod 
    def exists(cls, item_id, email, hashed=False):
        hashed_email = email if hashed else hash_email(email)
        return db.query(Loan).filter(
            Loan.item_id == item_id,
            Loan.patron_email_hash == hashed_email,
            Loan.returned_at == None
        ).first()

    @classmethod
    def create(cls, item_id, email, hashed=False):
        hashed_email = email if hashed else hash_email(email)
        try:
            loan = cls(item_id=item_id, patron_email_hash=hashed_email)
            db.add(loan)
            db.commit()
            return loan
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to create loan record: {str(e)}.")
    def finalize(self):
        try:
            self.returned_at = datetime.datetime.utcnow()
            db.add(self)
            db.commit()
            return self
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to return loan: {str(e)}.")

Item.loans = relationship('Loan', back_populates='item', cascade='all, delete-orphan')
