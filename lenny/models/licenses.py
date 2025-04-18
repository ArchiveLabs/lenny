from sqlalchemy import Column, String, DateTime, ForeignKey
from . import Base

class License(Base):
    __tablename__ = 'licenses'

    license_id = Column(String(50), primary_key=True)
    loan_id = Column(String(50), ForeignKey('loans.loan_id', ondelete='CASCADE'), unique=True, nullable=False)
    status = Column(String(20), nullable=False)
    end = Column(DateTime(timezone=True), nullable=False)
    content_id = Column(String(50))