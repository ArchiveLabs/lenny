
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base
from lenny.configs import DB_URI, DEBUG

logger = logging.getLogger(__name__)
# Only use client_encoding for PostgreSQL, not SQLite
engine_kwargs = {'echo': DEBUG}
if not DB_URI.startswith('sqlite'):
    engine_kwargs['client_encoding'] = 'utf8'
engine = create_engine(DB_URI, **engine_kwargs)
session = scoped_session(sessionmaker(
    bind=engine, autocommit=False, autoflush=False))

class LennyBase:
    @classmethod
    def get_many(cls, offset=None, limit=None):
        return session.query(cls).offset(offset).limit(limit).all()

Base = declarative_base(cls=LennyBase)

def init():
    """Initialize database session.

    Schema creation is handled by Alembic migrations.
    Run `alembic upgrade head` to apply migrations.
    """
    return session
