import pytest
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from lenny.core.db import Base

pytestmark = pytest.mark.skip(reason="Requires PostgreSQL-compatible DB; skipped in CI")
from lenny.catalog.types import (
    PipelineStage, STAGE_TRANSITIONS, STAGE_CHECKPOINTS,
    JobStatus, JobMode, Persona, EncryptionPolicy,
    InputMethod, ResolverType, OLStatus, ActionTaken,
)


# Import models so Base.metadata picks them up
import lenny.catalog.models  # noqa: F401
from lenny.catalog.models import ImportJob, ImportItem


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
        total=0,
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
        source_path="test.epub",
        sha256="abc123",
        retry_count=0,
        action_log=[],
    )
    defaults.update(kwargs)
    item = ImportItem(**defaults)
    session.add(item)
    session.commit()
    return item


# --- ImportJob tests ---

def test_import_job_creation(db_session):
    job = make_job(db_session)
    assert job.id is not None
    assert job.status == JobStatus.PENDING
    assert job.total == 0
    assert job.processed == 0


def test_import_job_counters_default_to_zero(db_session):
    job = make_job(db_session)
    assert job.linked == 0
    assert job.created_ol == 0
    assert job.needs_review == 0
    assert job.errors == 0
    assert job.skipped == 0


def test_import_job_increment_counter(db_session):
    job = make_job(db_session, total=10)
    job.increment("linked", db_session)
    db_session.refresh(job)
    assert job.linked == 1
    assert job.processed == 1


def test_import_job_increment_unknown_counter_raises(db_session):
    job = make_job(db_session)
    with pytest.raises(ValueError, match="Unknown counter"):
        job.increment("nonexistent", db_session)


# --- ImportItem stage transition tests ---

def test_import_item_creation(db_session):
    job = make_job(db_session)
    item = make_item(db_session, job.id)
    assert item.id is not None
    assert item.pipeline_stage == PipelineStage.PENDING
    assert item.retry_count == 0
    assert item.action_log == []


def test_import_item_advance_stage_valid(db_session):
    job = make_job(db_session)
    item = make_item(db_session, job.id)
    item.advance_stage(PipelineStage.EXTRACTING, db_session)
    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.EXTRACTING
    assert len(item.action_log) == 1
    assert item.action_log[0]["stage"] == "extracting"


def test_import_item_advance_stage_invalid_raises(db_session):
    job = make_job(db_session)
    item = make_item(db_session, job.id)
    with pytest.raises(ValueError, match="Invalid stage transition"):
        item.advance_stage(PipelineStage.DONE, db_session)


def test_import_item_action_log_appends(db_session):
    job = make_job(db_session)
    item = make_item(db_session, job.id)
    item.advance_stage(PipelineStage.EXTRACTING, db_session, isbn="9780441013593")
    item.advance_stage(PipelineStage.EXTRACTED, db_session, title="Dune")
    db_session.refresh(item)
    assert len(item.action_log) == 2
    assert item.action_log[1]["title"] == "Dune"


def test_import_item_mark_error_increments_retry(db_session):
    job = make_job(db_session)
    item = make_item(db_session, job.id, pipeline_stage=PipelineStage.EXTRACTING)
    item.mark_error("something broke", db_session, max_retries=3)
    db_session.refresh(item)
    assert item.retry_count == 1
    assert item.error_message == "something broke"
    # Not yet at max — should reset to checkpoint, not ERROR
    assert item.pipeline_stage == STAGE_CHECKPOINTS[PipelineStage.EXTRACTING]


def test_import_item_mark_error_at_max_retries_sets_error_stage(db_session):
    job = make_job(db_session)
    item = make_item(
        db_session, job.id,
        pipeline_stage=PipelineStage.EXTRACTING,
        retry_count=2,
    )
    item.mark_error("failed again", db_session, max_retries=3)
    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.ERROR
    assert item.retry_count == 3


def test_import_item_reset_stale_returns_to_checkpoint(db_session):
    job = make_job(db_session)
    stale_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    item = make_item(
        db_session, job.id,
        pipeline_stage=PipelineStage.OL_WRITING,
        stage_updated_at=stale_time,
    )
    reset_count = ImportItem.reset_stale(db_session, stale_after_seconds=300)
    db_session.refresh(item)
    assert reset_count == 1
    assert item.pipeline_stage == STAGE_CHECKPOINTS[PipelineStage.OL_WRITING]


def test_import_item_reset_stale_ignores_fresh_items(db_session):
    job = make_job(db_session)
    item = make_item(
        db_session, job.id,
        pipeline_stage=PipelineStage.OL_WRITING,
        # stage_updated_at defaults to now — fresh
    )
    reset_count = ImportItem.reset_stale(db_session, stale_after_seconds=300)
    assert reset_count == 0


def test_import_item_dedup_check(db_session):
    job = make_job(db_session)
    make_item(db_session, job.id, sha256="deadbeef")
    assert ImportItem.sha256_exists(db_session, "deadbeef") is True
    assert ImportItem.sha256_exists(db_session, "different") is False


def test_import_item_mark_error_no_checkpoint_falls_to_error(db_session):
    """mark_error on NEEDS_REVIEW (no checkpoint) should set ERROR directly."""
    job = make_job(db_session)
    item = make_item(
        db_session, job.id,
        pipeline_stage=PipelineStage.NEEDS_REVIEW,
    )
    item.mark_error("stuck in review", db_session, max_retries=3)
    db_session.refresh(item)
    # NEEDS_REVIEW has no checkpoint so it goes straight to ERROR
    assert item.pipeline_stage == PipelineStage.ERROR


def test_import_item_sha256_exists_excludes_error_stage(db_session):
    """A sha256 that only exists in ERROR stage should be re-importable."""
    job = make_job(db_session)
    make_item(db_session, job.id, sha256="errored", pipeline_stage=PipelineStage.ERROR)
    assert ImportItem.sha256_exists(db_session, "errored") is False
