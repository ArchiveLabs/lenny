#!/usr/bin/env python 

"""
    Item Model for Lenny,
    including the definition of the Item table and its attributes.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy  import Column, String, Boolean, BigInteger, DateTime, Enum as SQLAlchemyEnum
from sqlalchemy.sql import func
from lenny.core import db
import enum

class FormatEnum(enum.Enum):
    EPUB = 1
    PDF = 2
    EPUB_PDF = 3

class Item(db.Base):
    __tablename__ = 'items'
    
    id = Column(BigInteger, primary_key=True)
    openlibrary_edition = Column(BigInteger, nullable=False)
    encrypted = Column(Boolean, default= False, nullable=False)
    formats = Column(SQLAlchemyEnum(FormatEnum), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    @classmethod
    def exists(cls, olid):
        return db.query(Item).filter(Item.openlibrary_edition == olid).first()
