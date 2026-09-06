
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base
from lenny.configs import DB_URI, DEBUG

logger = logging.getLogger(__name__)
# Only use client_encoding for PostgreSQL, not SQLite
engine_kwargs = {'echo': DEBUG}
if not DB_URI.startswith('sqlite'):
    engine_kwargs['client_encoding'] = 'utf8'
elif ':memory:' in DB_URI:
    # An in-memory SQLite database lives inside a single connection, so the
    # default pool hands each checkout a fresh, empty database. That is
    # invisible until something returns a connection mid-test — the app's
    # `db_session.remove()` teardown does exactly that after every request —
    # at which point the schema appears to vanish. StaticPool keeps one
    # connection for the process; `check_same_thread` lets TestClient's worker
    # thread reuse it. Applies only under TESTING, where DB_URI is :memory:.
    from sqlalchemy.pool import StaticPool
    engine_kwargs['poolclass'] = StaticPool
    engine_kwargs['connect_args'] = {'check_same_thread': False}
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
