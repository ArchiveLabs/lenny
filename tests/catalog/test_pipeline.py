import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lenny.core.db import Base
import lenny.catalog.models  # noqa: F401
import lenny.core.models  # noqa: F401
from lenny.catalog.models import ImportJob, ImportItem
from lenny.catalog.types import (
    PipelineStage, JobStatus, JobMode, Persona, ResolverType,
    InputMethod, EncryptionPolicy, OLStatus, ActionTaken,
    BookMetadata, OLResult,
)
from lenny.catalog.pipeline import process_item


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def make_job(session, **kwargs) -> ImportJob:
    defaults = dict(
        mode=JobMode.FULL_IMPORT,
        persona=Persona.LIBRARY,
        resolver_type=ResolverType.API,
        input_method=InputMethod.EPUB_FOLDER,
        encryption_policy=EncryptionPolicy.ALL_ENCRYPTED,
        dry_run=False,
        gate_a_enabled=False,
        gate_b_enabled=False,
        skip_ol=False,
        total=1,
    )
    defaults.update(kwargs)
    job = ImportJob(**defaults)
    session.add(job)
    session.commit()
    return job


def make_item(session, job_id, **kwargs) -> ImportItem:
    defaults = dict(
        job_id=job_id,
        pipeline_stage=PipelineStage.PENDING,
        source_path="/tmp/test.epub",
        sha256="abc123",
        retry_count=0,
        action_log=[],
    )
    defaults.update(kwargs)
    item = ImportItem(**defaults)
    session.add(item)
    session.commit()
    return item


def mock_resolver(status=OLStatus.OL_MATCH_CLEAN, olid=12345, confidence=0.99,
                  action=ActionTaken.LINK_ONLY):
    resolver = MagicMock()
    resolver.lookup.return_value = OLResult(
        status=status, olid=olid, confidence=confidence, action=action,
    )
    resolver.create_edition.return_value = 12345
    return resolver


# --- Basic path tests ---

def test_process_item_link_only_reaches_ol_done(db_session, tmp_path):
    """LINK_ONLY path: PENDING → OL_DONE (metadata sync)."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, mode=JobMode.METADATA_SYNC)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver(status=OLStatus.OL_MATCH_CLEAN, action=ActionTaken.LINK_ONLY)

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    # METADATA_SYNC stops at OL_DONE (no upload)
    assert item.pipeline_stage in (PipelineStage.OL_DONE, PipelineStage.DONE)
    assert item.olid == 12345


def test_process_item_full_import_reaches_done(db_session, tmp_path):
    """FULL_IMPORT LINK_ONLY path: PENDING → DONE."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub content")
    job = make_job(db_session, mode=JobMode.FULL_IMPORT, encryption_policy=EncryptionPolicy.ALL_OPEN)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver(status=OLStatus.OL_MATCH_CLEAN, action=ActionTaken.LINK_ONLY)

    mock_s3 = MagicMock()
    mock_s3.upload_fileobj.return_value = None

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session, s3_client=mock_s3)

    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.DONE
    assert item.olid == 12345


def test_process_item_dry_run_stops_at_resolved(db_session, tmp_path):
    """dry_run=True: pipeline stops after RESOLVED — no OL writes, no upload."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, dry_run=True)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver()

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.RESOLVED
    resolver.create_edition.assert_not_called()


def test_process_item_create_full_calls_create_edition(db_session, tmp_path):
    """OL_NOT_FOUND → CREATE_FULL path calls create_edition."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, mode=JobMode.METADATA_SYNC)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver(
        status=OLStatus.OL_NOT_FOUND, olid=None, confidence=0.0, action=ActionTaken.CREATE_FULL
    )
    resolver.create_edition.return_value = 99999

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="New Book", authors=["New Author"])
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    resolver.create_edition.assert_called_once()
    assert item.olid == 99999


def test_process_item_skip_ol_skips_resolution(db_session, tmp_path):
    """skip_ol=True: item goes EXTRACTED → OL_DONE without calling resolver."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, skip_ol=True, mode=JobMode.METADATA_SYNC)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver()

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    resolver.lookup.assert_not_called()
    assert item.action_taken == ActionTaken.SKIPPED_OL
    assert item.pipeline_stage in (PipelineStage.OL_DONE, PipelineStage.DONE)


def test_process_item_gate_a_pauses_at_needs_review(db_session, tmp_path):
    """gate_a_enabled=True with no ISBN → pauses at NEEDS_REVIEW."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, gate_a_enabled=True)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver()

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        # Metadata without ISBN — low confidence
        mock_extract.return_value = BookMetadata(title="Dune")
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.NEEDS_REVIEW
    resolver.lookup.assert_not_called()


def test_process_item_insufficient_metadata_goes_to_needs_review(db_session, tmp_path):
    """Empty metadata → NEEDS_REVIEW."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver(
        status=OLStatus.INSUFFICIENT_METADATA, olid=None, confidence=0.0,
        action=ActionTaken.NEEDS_REVIEW
    )

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata()  # completely empty
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.NEEDS_REVIEW


def test_process_item_encryption_all_encrypted(db_session, tmp_path):
    """ALL_ENCRYPTED policy sets encrypted=True on item."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, encryption_policy=EncryptionPolicy.ALL_ENCRYPTED,
                   mode=JobMode.FULL_IMPORT)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver()
    mock_s3 = MagicMock()

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session, s3_client=mock_s3)

    db_session.refresh(item)
    assert item.encrypted is True


def test_process_item_encryption_all_open(db_session, tmp_path):
    """ALL_OPEN policy sets encrypted=False on item."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, encryption_policy=EncryptionPolicy.ALL_OPEN,
                   mode=JobMode.FULL_IMPORT)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver()
    mock_s3 = MagicMock()

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="Dune", authors=["Frank Herbert"])
        process_item(item, job, resolver, db_session, s3_client=mock_s3)

    db_session.refresh(item)
    assert item.encrypted is False


def test_process_item_gate_b_pauses_create_full(db_session, tmp_path):
    """gate_b_enabled=True with CREATE_FULL action → NEEDS_REVIEW before OL write."""
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"fake epub")
    job = make_job(db_session, gate_b_enabled=True, mode=JobMode.METADATA_SYNC)
    item = make_item(db_session, job.id, source_path=str(epub))
    resolver = mock_resolver(
        status=OLStatus.OL_NOT_FOUND, olid=None, confidence=0.0, action=ActionTaken.CREATE_FULL
    )

    with patch("lenny.catalog.pipeline.extract_epub") as mock_extract:
        mock_extract.return_value = BookMetadata(title="New Book", authors=["New Author"])
        process_item(item, job, resolver, db_session)

    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.NEEDS_REVIEW
    resolver.create_edition.assert_not_called()
