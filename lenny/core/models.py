#!/usr/bin/env python 

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy  import Column, String, Boolean, BigInteger, DateTime, Enum as SQLAlchemyEnum
from sqlalchemy.sql import func
from lenny.core.db import session as db, Base
from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
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
    is_readable = Column(Boolean, default=True, nullable=False)
    is_lendable = Column(Boolean, default=True, nullable=False)  
    is_login_required = Column(Boolean, default=False, nullable=False)  
    num_lendable_total = Column(BigInteger, default=1, nullable=False)
    is_waitlistable = Column(Boolean, default=False, nullable=False)  
    is_printdisabled = Column(Boolean, default=False, nullable=False)  
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    @classmethod
    def exists(cls, olid):
        return db.query(Item).filter(Item.openlibrary_edition == olid).first()

    @classmethod
    def find_encrypted(cls, olid):
        return db.query(Item).filter(Item.openlibrary_edition == olid, Item.encrypted == True).first()

class Loan(Base):
    __tablename__ = 'loans'

    id = Column(BigInteger, primary_key=True)
    item_id = Column(BigInteger, ForeignKey('items.id'), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    returned_at = Column(DateTime(timezone=True), nullable=True)

    item = relationship('Item', back_populates='loans')
    
    @classmethod
    def exists(cls, item_id, patron_email_hash):
        return db.query(Loan).filter(Loan.item_id == item_id, Loan.patron_email_hash == patron_email_hash, Loan.returned_at == None).first()

Item.loans = relationship('Loan', back_populates='item', cascade='all, delete-orphan')
