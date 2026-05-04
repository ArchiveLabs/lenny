import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db_session():
    from lenny.catalog.models import ImportJob, ImportItem

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only create the catalog tables (avoids PostgreSQL-specific DDL from other models).
    ImportJob.__table__.create(engine)
    ImportItem.__table__.create(engine)

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
    monkeypatch.setattr(auth_module, "ADMIN_INTERNAL_SECRET", "test-secret")
    from lenny.app import app
    from lenny.catalog.routes import get_db

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def admin_headers():
    return {"X-Admin-Internal-Secret": os.environ.get("ADMIN_INTERNAL_SECRET", "test-secret")}
