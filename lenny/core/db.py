
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base
from lenny.configs import DB_URI, DEBUG

logger = logging.getLogger(__name__)
engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
session = scoped_session(sessionmaker(
    bind=engine, autocommit=False, autoflush=False))

class LennyBase:
    @classmethod
    def get_many(cls, offset=None, limit=None):
        return session.query(cls).offset(offset).limit(limit).all()

Base = declarative_base(cls=LennyBase)

def init():
    try:
        
        Base.metadata.create_all(bind=engine)
        return session
    except Exception as e:
        logger.warning(f"[WARNING] Database initialization failed: {e}")        
