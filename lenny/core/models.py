#!/usr/bin/env python 

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy import Column, String, Boolean, BigInteger, Integer, DateTime, Enum as SQLAlchemyEnum, Index
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
    BookUnavailableError
)
import enum
import datetime
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

import logging as _models_logging
_models_logger = _models_logging.getLogger(__name__)

ph = PasswordHasher()

class FormatEnum(enum.Enum):
    EPUB = 1
    PDF = 2
    EPUB_PDF = 3

class Item(Base):
    __tablename__ = 'items'
    __table_args__ = (
        Index('idx_items_openlibrary_edition', 'openlibrary_edition'),
    )
    
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
        try:
            olid_int = int(olid)
        except (ValueError, TypeError):
            return None
        return db.query(Item).filter(Item.openlibrary_edition == olid_int).first()

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
        Borrow a book for a patron.
        
        Args:
            email: Patron's email address for loan tracking.
            
        Returns:
            Loan object (existing if already borrowed, new otherwise).
            
        Raises:
            LoanNotRequiredError: If item is open-access (no login needed).
            EmailNotFoundError: If email is not provided.
            BookUnavailableError: If no copies are available.
        """
        if not self.is_login_required:
            raise LoanNotRequiredError
        
        if not email:
            raise EmailNotFoundError("Email is required to borrow encrypted items.")

        hashed_email = hash_email(email)
        
        if active_loan := Loan.exists(self.id, hashed_email, hashed=True):
            return active_loan
        
        if not self.is_borrowable:
            raise BookUnavailableError("No copies available for borrowing.")
        
        return Loan.create(self.id, hashed_email, hashed=True)


class Loan(Base):
    __tablename__ = 'loans'
    __table_args__ = (
        Index('idx_loans_item_patron_returned', 'item_id', 'patron_email_hash', 'returned_at'),
        Index('idx_loans_item_returned', 'item_id', 'returned_at'),
    )

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
            self.returned_at = datetime.datetime.now(datetime.timezone.utc)
            db.add(self)
            db.commit()
            return self
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to return loan: {str(e)}.")

## OAuth Models
class Client(Base):
    __tablename__ = 'clients'
    
    client_id = Column(String, primary_key=True)
    client_secret_hash = Column(String, nullable=True) # For confidential clients, hashed
    redirect_uris = Column(String, nullable=False) # Comma-separated list (also supports JSON array)
    is_confidential = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

    def set_secret(self, secret):
        """Hashes and sets the client secret."""
        self.client_secret_hash = ph.hash(secret)
    
    def verify_secret(self, secret):
        """Verifies the client secret against the hash."""
        if not self.client_secret_hash:
            return False
        try:
            return ph.verify(self.client_secret_hash, secret)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError) as e:
            _models_logger.error(
                f"Hash integrity error for client '{self.client_id}': {type(e).__name__}: {e}"
            )
            return False

    @classmethod
    def get_by_id(cls, client_id):
        return db.query(cls).filter(cls.client_id == client_id).first()

    def is_valid_redirect_uri(self, redirect_uri):
        if not self.redirect_uris:
            return False
        
        uris_str = self.redirect_uris.strip()
        if uris_str.startswith('['):
            try:
                import json
                allowed_uris = [u.strip() for u in json.loads(uris_str)]
            except (json.JSONDecodeError, TypeError):
                allowed_uris = [uri.strip() for uri in uris_str.split(',')]
        else:
            allowed_uris = [uri.strip() for uri in uris_str.split(',')]
        return redirect_uri in allowed_uris

class AuthCode(Base):
    __tablename__ = 'auth_codes'
    __table_args__ = (
        Index('idx_auth_codes_client_id', 'client_id'),
        Index('idx_auth_codes_expires_at', 'expires_at'),
    )
    
    code = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('clients.client_id'), nullable=False)
    redirect_uri = Column(String, nullable=False)
    email_encrypted = Column(String, nullable=False) # Encrypted
    scope = Column(String, default="openid")
    state = Column(String, nullable=False) # CSRF Token
    code_challenge = Column(String, nullable=False)
    code_challenge_method = Column(String, nullable=False) # Must be S256
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    
    client = relationship('Client')

    @classmethod
    def get_by_code(cls, code):
        return db.query(cls).filter(cls.code == code).first()

    @classmethod
    def create(cls, **kwargs):
        try:
            auth_code = cls(**kwargs)
            db.add(auth_code)
            db.commit()
            return auth_code
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to create auth code: {str(e)}")

    @classmethod
    def mark_as_used(cls, code):
        """Atomic update to mark code as used and prevent race conditions."""
        try:
            rows_updated = db.query(cls).filter(
                cls.code == code,
                cls.used.is_(False)
            ).update({"used": True})
            db.commit()
            return rows_updated > 0
        except Exception:
            db.rollback()
            return False

    @classmethod
    def cleanup_expired(cls):
        """Delete auth codes that are expired or used."""
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            deleted = db.query(cls).filter(
                (cls.expires_at < now) | (cls.used.is_(True))
            ).delete(synchronize_session=False)
            db.commit()
            return deleted
        except Exception:
            db.rollback()
            return 0

class RefreshToken(Base):
    __tablename__ = 'refresh_tokens'
    __table_args__ = (
        Index('idx_refresh_tokens_client_id', 'client_id'),
    )

    token = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('clients.client_id'), nullable=False)
    email_encrypted = Column(String, nullable=False)
    scope = Column(String, default="openid")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

    client = relationship('Client')

    @classmethod
    def create(cls, **kwargs):
        try:
            token = cls(**kwargs)
            db.add(token)
            db.commit()
            return token
        except Exception as e:
            db.rollback()
            raise DatabaseInsertError(f"Failed to create refresh token: {str(e)}")

    @classmethod
    def get_by_token(cls, token):
        return db.query(cls).filter(cls.token == token).first()

    @classmethod
    def revoke(cls, token):
        """Atomic revoke to prevent race conditions."""
        try:
            rows_updated = db.query(cls).filter(
                cls.token == token,
                cls.revoked.is_(False)
            ).update({"revoked": True})
            db.commit()
            return rows_updated > 0
        except Exception:
            db.rollback()
            return False

    @classmethod
    def cleanup_expired(cls):
        """Delete refresh tokens that are expired or revoked."""
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            deleted = db.query(cls).filter(
                (cls.expires_at < now) | (cls.revoked.is_(True))
            ).delete(synchronize_session=False)
            db.commit()
            return deleted
        except Exception:
            db.rollback()
            return 0

Item.loans = relationship('Loan', back_populates='item', cascade='all, delete-orphan')
