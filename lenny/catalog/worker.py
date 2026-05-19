"""Catalog worker — run as: python -m lenny.catalog.worker"""
from __future__ import annotations
import datetime
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from lenny.catalog.models import ImportJob, ImportItem
from lenny.catalog.types import PipelineStage, JobStatus

_TERMINAL_STAGES = frozenset({PipelineStage.DONE, PipelineStage.ERROR, PipelineStage.SKIPPED})
from lenny.catalog.pipeline import process_item
from lenny.catalog.resolver import APIResolver

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2  # seconds between job-discovery polls


def make_worker_session(engine):
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=True, autocommit=False)


class CatalogWorker:
    """ThreadPoolExecutor-based catalog worker."""

    def __init__(self, concurrency: int, db_engine, s3_client=None):
        self.concurrency = concurrency
        self._engine = db_engine
        self._s3 = s3_client
        self._stop_event = threading.Event()
        self._SessionFactory = make_worker_session(db_engine)
        from lenny.configs import GOOGLE_BOOKS_API_KEY
        if not GOOGLE_BOOKS_API_KEY:
            logger.warning("GOOGLE_BOOKS_API_KEY not set — Google Books fallback disabled")

    def run(self, max_iterations: Optional[int] = None) -> None:
        """Main blocking loop. Runs until stop() is called or max_iterations reached."""
        logger.info("Catalog worker starting (concurrency=%d)", self.concurrency)

        with self._SessionFactory() as session:
            n = self._reset_stale(session)
            if n:
                logger.info("Reset %d stale items on startup", n)

        iteration = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            while not self._stop_event.is_set():
                if max_iterations is not None and iteration >= max_iterations:
                    break

                did_work = self._run_one_iteration(executor)
                iteration += 1

                if not did_work:
                    self._stop_event.wait(timeout=_POLL_INTERVAL)

        logger.info("Catalog worker stopped")

    def stop(self) -> None:
        """Signal the worker to stop after finishing in-flight items."""
        self._stop_event.set()

    def _run_one_iteration(self, executor: ThreadPoolExecutor) -> bool:
        """Claim and dispatch one batch of pending items. Returns True if work was done."""
        claimed: list = []  # [(item_id, job_id)]

        with self._SessionFactory() as session:
            jobs = self._find_active_jobs(session)
            if not jobs:
                return False

            for job in jobs:
                if self._stop_event.is_set():
                    break
                # claim_pending uses SELECT FOR UPDATE SKIP LOCKED.
                # We immediately advance each item to EXTRACTING inside this transaction
                # so the row is not re-claimable once the lock releases on session close.
                items = ImportItem.claim_pending(session, job.id, limit=self.concurrency)
                if not items:
                    self._check_job_completion(job, session)
                    continue
                for item in items:
                    item.advance_stage(PipelineStage.EXTRACTING, session)
                    claimed.append((item.id, job.id))
            # Session closes here — SKIP LOCKED locks released; items are already EXTRACTING

        if not claimed:
            return False

        futures = [
            executor.submit(self._process_one, item_id, job_id)
            for item_id, job_id in claimed
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logger.error("Worker thread error: %s", e)

        return True

    def _process_one(self, item_id: int, job_id: int) -> None:
        """Process a single item in a worker thread. Creates its own DB session."""
        with self._SessionFactory() as session:
            item = session.get(ImportItem, item_id)
            job = session.get(ImportJob, job_id)
            if not item or not job:
                logger.warning("Item %d or job %d not found", item_id, job_id)
                return

            resolver = self._make_resolver(job)
            process_item(item, job, resolver, session, s3_client=self._s3)

            session.refresh(item)
            counter = _outcome_counter(item)
            if counter:
                job.increment(counter, session)

    def _make_resolver(self, job: ImportJob) -> APIResolver:
        from lenny.configs import CATALOG_DUMP_THRESHOLD, GOOGLE_BOOKS_API_KEY
        if job.total and job.total >= CATALOG_DUMP_THRESHOLD:
            logger.info("Job %d has %d items; DumpResolver not yet available, using API", job.id, job.total)
        return APIResolver(google_books_api_key=GOOGLE_BOOKS_API_KEY)

    def _find_active_jobs(self, session: Session) -> List[ImportJob]:
        return (
            session.query(ImportJob)
            .filter(ImportJob.status == JobStatus.RUNNING)
            .all()
        )

    def _check_job_completion(self, job: ImportJob, session: Session) -> None:
        """Mark job COMPLETED when all items are terminal, AWAITING_REVIEW when gated."""
        non_terminal = (
            session.query(ImportItem)
            .filter(
                ImportItem.job_id == job.id,
                ImportItem.pipeline_stage.notin_(_TERMINAL_STAGES),
            )
            .count()
        )
        if non_terminal == 0:
            new_status = JobStatus.COMPLETED
            job.completed_at = datetime.datetime.now(datetime.timezone.utc)
        else:
            in_review = (
                session.query(ImportItem)
                .filter(
                    ImportItem.job_id == job.id,
                    ImportItem.pipeline_stage == PipelineStage.NEEDS_REVIEW,
                )
                .count()
            )
            if in_review < non_terminal or job.status == JobStatus.AWAITING_REVIEW:
                return
            new_status = JobStatus.AWAITING_REVIEW

        job.status = new_status
        session.add(job)
        try:
            session.commit()
            logger.info("Job %d marked %s", job.id, new_status.value)
        except Exception:
            session.rollback()
            logger.exception("Failed to update job %d status to %s", job.id, new_status.value)

    def _reset_stale(self, session: Session) -> int:
        from lenny.configs import CATALOG_STALE_TIMEOUT
        return ImportItem.reset_stale(session, stale_after_seconds=CATALOG_STALE_TIMEOUT)


def _outcome_counter(item: ImportItem) -> Optional[str]:
    stage = item.pipeline_stage
    if stage == PipelineStage.DONE:
        from lenny.catalog.types import ActionTaken
        if item.action_taken == ActionTaken.CREATE_FULL:
            return "created_ol"
        if item.action_taken == ActionTaken.LINK_ONLY:
            return "linked"
        return None
    if stage == PipelineStage.ERROR:
        return "errors"
    if stage == PipelineStage.NEEDS_REVIEW:
        return "needs_review"
    if stage == PipelineStage.SKIPPED:
        return "skipped"
    return None


def main() -> None:
    """Entry point for `python -m lenny.catalog.worker`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from lenny.configs import CATALOG_CONCURRENCY, DB_URI
    from lenny.core.s3 import LennyS3

    engine = create_engine(
        DB_URI,
        pool_size=CATALOG_CONCURRENCY + 2,
        max_overflow=2,
    )

    try:
        s3 = LennyS3()
    except Exception as e:
        logger.warning("Could not initialize S3 client: %s — upload stages will be skipped", e)
        s3 = None

    worker = CatalogWorker(concurrency=CATALOG_CONCURRENCY, db_engine=engine, s3_client=s3)

    def _handle_signal(signum, frame):
        logger.info("Received signal %d — stopping worker", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    worker.run()


if __name__ == "__main__":
    main()
