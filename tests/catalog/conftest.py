import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db_session():
    from lenny.catalog.models import ImportJob, ImportItem
    from sqlalchemy import text

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create catalog tables (avoids PostgreSQL-specific DDL from other models).
    ImportJob.__table__.create(engine)
    ImportItem.__table__.create(engine)
    # Create the items table with a SQLite-compatible schema.
    # The production model uses BigInteger PK (PostgreSQL sequence); SQLite needs
    # INTEGER PRIMARY KEY AUTOINCREMENT for equivalent behaviour.
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                openlibrary_edition INTEGER NOT NULL,
                encrypted BOOLEAN NOT NULL DEFAULT 0,
                formats VARCHAR NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        conn.commit()

    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()

    yield s

    s.close()
    ImportItem.__table__.drop(engine)
    ImportJob.__table__.drop(engine)


@pytest.fixture
def client(db_session, monkeypatch):
    """TestClient with the catalog router mounted."""
    import lenny.core.auth as auth_module
    import lenny.catalog.routes as routes_module
    monkeypatch.setattr(auth_module, "ADMIN_INTERNAL_SECRET", "test-secret")
    from lenny.app import app
    from lenny.catalog.routes import get_db

    def override_get_db():
        yield db_session

    # SSE endpoints bypass get_db and call _scoped_session directly.
    # Patch it so the test session is used there too.
    class _MockScoped:
        def __call__(self):
            return db_session
        def remove(self):
            pass

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(routes_module, "_scoped_session", _MockScoped())
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def admin_headers():
    return {"X-Admin-Internal-Secret": os.environ.get("ADMIN_INTERNAL_SECRET", "test-secret")}
