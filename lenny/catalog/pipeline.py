from __future__ import annotations
import logging
import os
from typing import Optional

from lenny.catalog.extractor import extract_epub, extract_json_sidecar, extract_csv_row
from lenny.catalog.models import ImportJob, ImportItem
from lenny.catalog.resolver import OLResolver
from lenny.catalog.types import (
    PipelineStage, JobMode, EncryptionPolicy, InputMethod,
    OLStatus, ActionTaken, BookMetadata,
)
from lenny.catalog.exceptions import OLRateLimited, OLWriteError, InsufficientMetadata

logger = logging.getLogger(__name__)


def _extract_metadata(item: ImportItem, job: ImportJob) -> BookMetadata:
    """Dispatch to the right extractor based on job input method and file type."""
    path = item.source_path or ""
    if job.input_method in (InputMethod.EPUB_FOLDER, InputMethod.EPUB_SIDECAR):
        if path.endswith(".json"):
            return extract_json_sidecar(path)
        if path.endswith(".csv"):
            row = {}
            if item.extracted_metadata:
                row = item.extracted_metadata
            return extract_csv_row(row)
        return extract_epub(path)
    if job.input_method == InputMethod.CSV:
        row = item.extracted_metadata or {}
        return extract_csv_row(row)
    return extract_epub(path)


def _determine_encrypted(job: ImportJob, metadata: BookMetadata) -> bool:
    """Return the encrypted flag for this item based on the job's encryption policy."""
    policy = job.encryption_policy
    if policy == EncryptionPolicy.ALL_ENCRYPTED:
        return True
    if policy == EncryptionPolicy.ALL_OPEN:
        return False
    if policy == EncryptionPolicy.MIXED_AUTO:
        # Phase 2: inspect DRM markers; for now default to open
        return False
    # MIXED_MANUAL — default to encrypted, admin will decide per-item
    return True


def process_item(
    item: ImportItem,
    job: ImportJob,
    resolver,
    session,
    s3_client=None,
) -> None:
    """Drive a single ImportItem through all pipeline stages.

    Never raises — catches all exceptions and calls mark_error.
    """
    try:
        _run_pipeline(item, job, resolver, session, s3_client)
    except OLRateLimited as e:
        logger.warning("OL rate limited on item %d: %s", item.id, e)
        from lenny.configs import CATALOG_MAX_RETRIES
        item.mark_error(str(e), session, max_retries=CATALOG_MAX_RETRIES)
    except Exception as e:
        logger.exception("Unexpected error on item %d: %s", item.id, e)
        from lenny.configs import CATALOG_MAX_RETRIES
        item.mark_error(str(e), session, max_retries=CATALOG_MAX_RETRIES)


def _run_pipeline(
    item: ImportItem,
    job: ImportJob,
    resolver,
    session,
    s3_client,
) -> None:
    """Inner pipeline — raises on error, process_item catches."""
    # --- Stage: PENDING → EXTRACTING ---
    # Worker pre-advances to EXTRACTING inside the claim transaction to release
    # SKIP LOCKED immediately; skip the transition if already there.
    if item.pipeline_stage == PipelineStage.PENDING:
        item.advance_stage(PipelineStage.EXTRACTING, session)
    elif item.pipeline_stage != PipelineStage.EXTRACTING:
        raise ValueError(f"process_item called on item in unexpected stage: {item.pipeline_stage!r}")

    # --- Stage: EXTRACTING → EXTRACTED ---
    metadata = _extract_metadata(item, job)
    item.extracted_title = metadata.title
    item.extracted_author = metadata.primary_author
    item.extracted_isbn = metadata.best_isbn
    item.extracted_metadata = {
        "title": metadata.title,
        "authors": metadata.authors,
        "isbn_13": metadata.isbn_13,
        "isbn_10": metadata.isbn_10,
        "publisher": metadata.publisher,
        "publish_date": metadata.publish_date,
        "language": metadata.language,
        "source": metadata.source,
    }
    item.advance_stage(PipelineStage.EXTRACTED, session, isbn=metadata.best_isbn, title=metadata.title)

    # --- Gate A: low-confidence extraction review ---
    if job.gate_a_enabled and not metadata.is_resolvable:
        item.advance_stage(PipelineStage.NEEDS_REVIEW, session, reason="gate_a_low_confidence")
        return

    # --- skip_ol: no OL lookup — advance through RESOLVING → RESOLVED → OL_DONE ---
    if job.skip_ol or item.skip_ol:
        item.action_taken = ActionTaken.SKIPPED_OL
        # Must traverse legal transitions: EXTRACTED → RESOLVING → RESOLVED → OL_DONE
        item.advance_stage(PipelineStage.RESOLVING, session, action="skip_ol")
        item.advance_stage(PipelineStage.RESOLVED, session, action="skip_ol")
        item.advance_stage(PipelineStage.OL_DONE, session, action="skipped_ol")
        _maybe_upload(item, job, session, s3_client, metadata)
        return

    # --- Stage: EXTRACTED → RESOLVING ---
    item.advance_stage(PipelineStage.RESOLVING, session)

    # --- Stage: RESOLVING → RESOLVED ---
    result = resolver.lookup(metadata)
    item.ol_status = result.status
    item.confidence = result.confidence
    item.olid = result.olid
    item.action_taken = result.action

    if result.candidates:
        item.review_candidates = [
            {"olid": c.olid, "title": c.title, "authors": c.authors,
             "year": c.year, "publisher": c.publisher, "score": c.score}
            for c in result.candidates
        ]

    item.advance_stage(
        PipelineStage.RESOLVED, session,
        ol_status=result.status.value if result.status else None,
        confidence=result.confidence,
        olid=result.olid,
    )

    # --- dry_run: stop here ---
    if job.dry_run:
        return

    # --- NEEDS_REVIEW: insufficient metadata or fuzzy match ---
    if result.status == OLStatus.INSUFFICIENT_METADATA or result.action == ActionTaken.NEEDS_REVIEW:
        item.advance_stage(PipelineStage.NEEDS_REVIEW, session, reason="low_confidence_or_insufficient")
        return

    # --- Gate B: OL creation review before writing ---
    if job.gate_b_enabled and result.action == ActionTaken.CREATE_FULL:
        item.advance_stage(PipelineStage.NEEDS_REVIEW, session, reason="gate_b_ol_creation_review")
        return

    # --- Stage: OL write (only if CREATE_FULL) ---
    if result.action == ActionTaken.CREATE_FULL:
        item.advance_stage(PipelineStage.OL_WRITING, session)
        new_olid = resolver.create_edition(metadata)
        item.olid = new_olid
        item.advance_stage(PipelineStage.OL_DONE, session, action="create_full", new_olid=new_olid)
    else:
        # LINK_ONLY — OLID already confirmed
        item.advance_stage(PipelineStage.OL_DONE, session, action="link_only")

    # --- Upload + Lenny write ---
    _maybe_upload(item, job, session, s3_client, metadata)


def _maybe_upload(item: ImportItem, job: ImportJob, session, s3_client, metadata: BookMetadata = None) -> None:
    """Upload EPUB to MinIO and write Item row, if this is a FULL_IMPORT job."""
    if job.mode != JobMode.FULL_IMPORT or job.dry_run:
        item.advance_stage(PipelineStage.DONE, session)
        return

    if not item.source_path or not os.path.exists(item.source_path):
        item.advance_stage(PipelineStage.DONE, session)
        return

    if item.olid is None:
        logger.warning("Item %d has no OLID — skipping upload", item.id)
        item.advance_stage(PipelineStage.DONE, session)
        return

    if s3_client is None:
        raise ValueError(f"s3_client required for FULL_IMPORT item {item.id}")

    encrypted = _determine_encrypted(job, metadata or BookMetadata())
    item.encrypted = encrypted

    # --- Stage: OL_DONE → UPLOADING ---
    item.advance_stage(PipelineStage.UPLOADING, session)

    minio_key = f"epubs/{item.olid}/{os.path.basename(item.source_path)}"
    with open(item.source_path, "rb") as f:
        s3_client.upload_fileobj(f, "bookshelf", minio_key)
    item.minio_key = minio_key

    from lenny.core.models import Item, FormatEnum
    existing = session.query(Item).filter(Item.openlibrary_edition == item.olid).first()
    if not existing:
        try:
            with session.begin_nested():
                lenny_item = Item(
                    openlibrary_edition=item.olid,
                    encrypted=encrypted,
                    formats=FormatEnum.EPUB,
                )
                session.add(lenny_item)
                session.flush()
                item.item_id = lenny_item.id
        except Exception as e:
            logger.warning("Failed to write Lenny Item row for olid=%s: %s", item.olid, e)

    item.advance_stage(PipelineStage.DONE, session)
