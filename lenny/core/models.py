#!/usr/bin/env python 

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy  import Column, String, Boolean, BigInteger, Integer, DateTime, Enum as SQLAlchemyEnum
from datetime import timedelta, datetime
from sqlalchemy.sql import func
from lenny.core.db import session as db, Base
from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
import enum

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
    
class Loan(Base):
    __tablename__ = 'loans'

    id = Column(BigInteger, primary_key=True)
    item_id = Column(BigInteger, ForeignKey('items.id'), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    returned_at = Column(DateTime(timezone=True), nullable=True)

    item = relationship('Item', back_populates='loans')
    
    @classmethod 
    def Loan_exists(cls, item_id, patron_email_hash):
        return db.query(Loan).filter(
            Loan.item_id == item_id,
            Loan.patron_email_hash == patron_email_hash,
            Loan.returned_at == None
        ).first() is not None


Item.loans = relationship('Loan', back_populates='item', cascade='all, delete-orphan')

class Auth(Base):
    __tablename__ = 'auth'
    
    email_token = Column(String, primary_key=True)
    session_token = Column(String, nullable=True) # e.g IP Address
    code = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    attempts = Column(Integer, default=0)
    
    @classmethod
    def expire(cls, minutes: int =5):
        """Delete auth less than N minutes."""
        threshold = datetime.utcnow() - timedelta(minutes=minutes)
        db.query(Auth).filter(cls.created_at < threshold).delete()
        db.commit()