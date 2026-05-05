import pytest
import time
import threading
from unittest.mock import MagicMock, patch, call
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lenny.core.db import Base
import lenny.catalog.models  # noqa: F401
import lenny.core.models  # noqa: F401

pytestmark = pytest.mark.skip(reason="Requires PostgreSQL-compatible DB; skipped in CI")
from lenny.catalog.models import ImportJob, ImportItem
from lenny.catalog.types import (
    PipelineStage, JobStatus, JobMode, Persona, ResolverType,
    InputMethod, EncryptionPolicy,
)
from lenny.catalog.worker import CatalogWorker, make_worker_session


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def make_job(session, status=JobStatus.RUNNING, **kwargs) -> ImportJob:
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
        status=status,
        total=5,
    )
    defaults.update(kwargs)
    job = ImportJob(**defaults)
    session.add(job)
    session.commit()
    return job


def make_item(session, job_id, stage=PipelineStage.PENDING, **kwargs) -> ImportItem:
    defaults = dict(
        job_id=job_id,
        pipeline_stage=stage,
        source_path="/tmp/test.epub",
        sha256=f"hash_{time.time()}",
        retry_count=0,
        action_log=[],
    )
    defaults.update(kwargs)
    item = ImportItem(**defaults)
    session.add(item)
    session.commit()
    return item


# --- make_worker_session ---

def test_make_worker_session_returns_callable(engine):
    Session = make_worker_session(engine)
    assert callable(Session)
    s = Session()
    s.close()


# --- CatalogWorker initialization ---

def test_catalog_worker_init(engine):
    worker = CatalogWorker(concurrency=2, db_engine=engine)
    assert worker.concurrency == 2
    assert worker._stop_event is not None


# --- reset_stale on startup ---

def test_worker_resets_stale_items_on_startup(engine, session):
    import datetime
    job = make_job(session)
    stale_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
    make_item(session, job.id, stage=PipelineStage.OL_WRITING, stage_updated_at=stale_time)

    worker = CatalogWorker(concurrency=1, db_engine=engine)
    with make_worker_session(engine)() as s:
        reset_count = worker._reset_stale(s)
    assert reset_count >= 1


# --- find_active_jobs ---

def test_find_active_jobs_returns_running_jobs(engine, session):
    job_running = make_job(session, status=JobStatus.RUNNING)
    job_pending = make_job(session, status=JobStatus.PENDING)
    job_completed = make_job(session, status=JobStatus.COMPLETED)

    worker = CatalogWorker(concurrency=2, db_engine=engine)
    with make_worker_session(engine)() as s:
        active = worker._find_active_jobs(s)
    active_ids = [j.id for j in active]
    assert job_running.id in active_ids
    assert job_pending.id not in active_ids
    assert job_completed.id not in active_ids


# --- stop event ---

def test_stop_event_halts_run_loop(engine, session):
    """Worker run() returns quickly when stop_event is pre-set."""
    worker = CatalogWorker(concurrency=1, db_engine=engine)
    worker._stop_event.set()  # Set before run

    start = time.time()
    worker.run(max_iterations=1)
    elapsed = time.time() - start
    assert elapsed < 2.0  # Should return almost immediately


# --- job completion detection ---

def test_worker_marks_job_completed_when_all_items_done(engine, session):
    job = make_job(session, total=2)
    # All items are DONE
    make_item(session, job.id, stage=PipelineStage.DONE)
    make_item(session, job.id, stage=PipelineStage.DONE)

    worker = CatalogWorker(concurrency=1, db_engine=engine)
    with make_worker_session(engine)() as s:
        refreshed_job = s.get(ImportJob, job.id)
        worker._check_job_completion(refreshed_job, s)
        s.refresh(refreshed_job)
    assert refreshed_job.status == JobStatus.COMPLETED


# --- _outcome_counter ---

def test_outcome_counter_linked(engine, session):
    from lenny.catalog.worker import _outcome_counter
    from lenny.catalog.types import ActionTaken
    job = make_job(session)
    item = make_item(session, job.id, stage=PipelineStage.DONE)
    item.action_taken = ActionTaken.LINK_ONLY
    assert _outcome_counter(item) == "linked"


def test_outcome_counter_created_ol(engine, session):
    from lenny.catalog.worker import _outcome_counter
    from lenny.catalog.types import ActionTaken
    job = make_job(session)
    item = make_item(session, job.id, stage=PipelineStage.DONE)
    item.action_taken = ActionTaken.CREATE_FULL
    assert _outcome_counter(item) == "created_ol"


def test_outcome_counter_error(engine, session):
    from lenny.catalog.worker import _outcome_counter
    job = make_job(session)
    item = make_item(session, job.id, stage=PipelineStage.ERROR)
    assert _outcome_counter(item) == "errors"


def test_outcome_counter_needs_review(engine, session):
    from lenny.catalog.worker import _outcome_counter
    job = make_job(session)
    item = make_item(session, job.id, stage=PipelineStage.NEEDS_REVIEW)
    assert _outcome_counter(item) == "needs_review"


def test_outcome_counter_in_progress_returns_none(engine, session):
    from lenny.catalog.worker import _outcome_counter
    job = make_job(session)
    item = make_item(session, job.id, stage=PipelineStage.RESOLVING)
    assert _outcome_counter(item) is None


def test_check_job_not_completed_when_pending_items_remain(engine, session):
    """Job is NOT marked completed while items are still PENDING."""
    job = make_job(session, total=2)
    make_item(session, job.id, stage=PipelineStage.DONE)
    make_item(session, job.id, stage=PipelineStage.PENDING)

    worker = CatalogWorker(concurrency=1, db_engine=engine)
    with make_worker_session(engine)() as s:
        refreshed_job = s.get(ImportJob, job.id)
        worker._check_job_completion(refreshed_job, s)
        s.refresh(refreshed_job)
    assert refreshed_job.status == JobStatus.RUNNING
