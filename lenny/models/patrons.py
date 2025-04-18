from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func
from . import Base

class Patron(Base):
    __tablename__ = 'patrons'

    patron_id = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    accessibility = Column(String(50))
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())