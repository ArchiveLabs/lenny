from sqlalchemy import Column, String, Boolean, Integer, DateTime, ForeignKey
from sqlalchemy.sql import func
from . import Base

class Loan(Base):
    __tablename__ = 'loans'

    loan_id = Column(String(50), primary_key=True)
    item_identifier = Column(String(50), ForeignKey('items.identifier', ondelete='CASCADE'), nullable=False)
    patron_id = Column(String(50), ForeignKey('patrons.patron_id', ondelete='CASCADE'), nullable=False)
    start_time = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    expire_date = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    returned_at = Column(DateTime(timezone=True))
    license_id = Column(String(50))
    renewal_count = Column(Integer, default=0, nullable=False)