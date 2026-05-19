from __future__ import annotations
import asyncio
import json as _json
import logging
from typing import Generator, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from lenny.core import auth
from lenny.core.db import session as _scoped_session
from lenny.core.openlibrary import ol_auth_status
from lenny.catalog.models import ImportJob, ImportItem
from lenny.catalog.types import JobStatus, PipelineStage, ResolverType, ActionTaken, EncryptionPolicy
from lenny.catalog.types import BookMetadata
from lenny.catalog.schemas import (
    CreateJobRequest, JobResponse,
    ReviewItemResponse, MetadataReviewSubmit, OLCreationEdit,
    EncryptionSubmit, FuzzyResolve, ManualCreateRequest,
)
from lenny.catalog.resolver import APIResolver
from lenny.catalog.exceptions import OLWriteError
from lenny.core.models import Item, FormatEnum

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])


def get_db() -> Generator[Session, None, None]:
    try:
        yield _scoped_session
    finally:
        _scoped_session.remove()


async def require_catalog_admin(request: Request) -> None:
    """Allow requests with a valid X-Admin-Internal-Secret header OR Bearer token."""
    internal_secret = request.headers.get("X-Admin-Internal-Secret", "")
    if auth.verify_admin_internal_secret(internal_secret):
        return
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if auth.verify_admin_token(token):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required",
    )


@router.get("/jobs", dependencies=[Depends(require_catalog_admin)], response_model=List[JobResponse])
async def list_jobs(db: Session = Depends(get_db)) -> List[JobResponse]:
    jobs = db.query(ImportJob).order_by(ImportJob.created_at.desc()).all()
    return [JobResponse.model_validate(j) for j in jobs]


@router.post("/jobs", dependencies=[Depends(require_catalog_admin)], response_model=JobResponse, status_code=201)
async def create_job(body: CreateJobRequest, db: Session = Depends(get_db)) -> JobResponse:
    job = ImportJob(
        mode=body.mode,
        persona=body.persona,
        resolver_type=ResolverType.API,
        input_method=body.input_method,
        encryption_policy=body.encryption_policy,
        dry_run=body.dry_run,
        gate_a_enabled=body.gate_a_enabled,
        gate_b_enabled=body.gate_b_enabled,
        skip_ol=body.skip_ol,
        total=body.total,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.flush()  # assigns job.id without committing

    if body.items:
        for item_req in body.items:
            db.add(ImportItem(
                job_id=job.id,
                source_path=item_req.source_path,
                sha256=item_req.sha256,
                extracted_metadata=item_req.extracted_metadata,
                pipeline_stage=PipelineStage.PENDING,
                retry_count=0,
                action_log=[],
            ))
        job.total = len(body.items)
        job.status = JobStatus.RUNNING

    db.commit()
    db.refresh(job)

    return JobResponse.model_validate(job)


@router.get("/jobs/{job_id}/stream", dependencies=[Depends(require_catalog_admin)])
async def stream_job_progress(job_id: int, db: Session = Depends(get_db)):
    """SSE endpoint: polls import_jobs every 2 seconds and streams progress.

    Each iteration acquires a fresh session via _scoped_session so the pool
    connection is released between polls rather than held for the stream lifetime.
    The injected `db` is used only for the initial existence check.
    """
    if not db.get(ImportJob, job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    async def _event_generator():
        _TERMINAL = {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.ERROR}
        while True:
            try:
                session = _scoped_session()
                current = session.get(ImportJob, job_id)
                if not current:
                    break
                payload = JobResponse.model_validate(current).model_dump(mode="json")
                is_terminal = current.status in _TERMINAL
            finally:
                _scoped_session.remove()
            yield f"data: {_json.dumps(payload)}\n\n"
            if is_terminal:
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(require_catalog_admin)], response_model=JobResponse)
async def get_job(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobResponse.model_validate(job)


@router.post("/jobs/{job_id}/pause", dependencies=[Depends(require_catalog_admin)], response_model=JobResponse)
async def pause_job(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
        raise HTTPException(status_code=409, detail=f"Cannot pause job with status {job.status}")
    job.status = JobStatus.PAUSED
    db.commit()
    db.refresh(job)
    return JobResponse.model_validate(job)


@router.post("/jobs/{job_id}/resume", dependencies=[Depends(require_catalog_admin)], response_model=JobResponse)
async def resume_job(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status != JobStatus.PAUSED:
        raise HTTPException(status_code=409, detail=f"Cannot resume job with status {job.status}")
    job.status = JobStatus.RUNNING
    db.commit()
    db.refresh(job)
    return JobResponse.model_validate(job)


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_catalog_admin)], response_model=JobResponse)
async def cancel_job(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
        raise HTTPException(status_code=409, detail=f"Job is already {job.status}")
    job.status = JobStatus.CANCELLED
    db.commit()
    db.refresh(job)
    return JobResponse.model_validate(job)


# ---------------------------------------------------------------------------
# Review queue endpoints (Gates A, B, C + Fuzzy)
# These are mounted under /catalog/review/* via the router prefix.
# ---------------------------------------------------------------------------

@router.get("/review/metadata", dependencies=[Depends(require_catalog_admin)], response_model=List[ReviewItemResponse])
async def list_metadata_review(job_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(ImportItem).filter(ImportItem.pipeline_stage == PipelineStage.NEEDS_REVIEW)
    if job_id:
        q = q.filter(ImportItem.job_id == job_id)
    return [ReviewItemResponse.model_validate(i) for i in q.all()]


@router.post("/review/metadata/{item_id}", dependencies=[Depends(require_catalog_admin)], response_model=ReviewItemResponse)
async def submit_metadata_review(item_id: int, body: MetadataReviewSubmit, db: Session = Depends(get_db)):
    item = db.get(ImportItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    if body.title is not None:
        item.extracted_title = body.title
    if body.authors is not None:
        item.extracted_author = body.authors[0] if body.authors else None
    if body.isbn_13 is not None:
        item.extracted_isbn = body.isbn_13
    meta = dict(item.extracted_metadata or {})
    if body.title is not None:
        meta["title"] = body.title
    if body.authors is not None:
        meta["authors"] = body.authors
    if body.isbn_13 is not None:
        meta["isbn_13"] = body.isbn_13
    if body.isbn_10 is not None:
        meta["isbn_10"] = body.isbn_10
    if body.publisher is not None:
        meta["publisher"] = body.publisher
    item.extracted_metadata = meta
    # FSM CORRECTION: NEEDS_REVIEW → RESOLVED (NEEDS_REVIEW → EXTRACTED is not a valid transition)
    item.advance_stage(PipelineStage.RESOLVED, db, action="gate_a_review_submitted")
    return ReviewItemResponse.model_validate(item)


# --- Gate B: OL creation review ---

@router.get("/review/ol-creation", dependencies=[Depends(require_catalog_admin)], response_model=List[ReviewItemResponse])
async def list_ol_creation_review(job_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = (db.query(ImportItem)
         .filter(ImportItem.pipeline_stage == PipelineStage.NEEDS_REVIEW,
                 ImportItem.action_taken == ActionTaken.CREATE_FULL))
    if job_id:
        q = q.filter(ImportItem.job_id == job_id)
    return [ReviewItemResponse.model_validate(i) for i in q.all()]


@router.post("/review/ol-creation/{item_id}/approve", dependencies=[Depends(require_catalog_admin)], response_model=ReviewItemResponse)
async def approve_ol_creation(item_id: int, db: Session = Depends(get_db)):
    item = db.get(ImportItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    # FSM CORRECTION: NEEDS_REVIEW → RESOLVED (not OL_WRITING)
    item.advance_stage(PipelineStage.RESOLVED, db, action="gate_b_approved")
    return ReviewItemResponse.model_validate(item)


@router.post("/review/ol-creation/{item_id}/edit", dependencies=[Depends(require_catalog_admin)], response_model=ReviewItemResponse)
async def edit_ol_creation(item_id: int, body: OLCreationEdit, db: Session = Depends(get_db)):
    item = db.get(ImportItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    meta = dict(item.extracted_metadata or {})
    if body.title is not None:
        item.extracted_title = body.title
        meta["title"] = body.title
    if body.authors is not None:
        meta["authors"] = body.authors
    if body.publisher is not None:
        meta["publisher"] = body.publisher
    if body.publish_date is not None:
        meta["publish_date"] = body.publish_date
    item.extracted_metadata = meta
    # FSM CORRECTION: NEEDS_REVIEW → RESOLVED (not OL_WRITING)
    item.advance_stage(PipelineStage.RESOLVED, db, action="gate_b_edited_and_approved")
    return ReviewItemResponse.model_validate(item)


# --- Gate C: Encryption review (MIXED_MANUAL policy) ---

@router.get("/review/encryption", dependencies=[Depends(require_catalog_admin)], response_model=List[ReviewItemResponse])
async def list_encryption_review(job_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = (db.query(ImportItem)
         .join(ImportJob, ImportItem.job_id == ImportJob.id)
         .filter(
             ImportItem.pipeline_stage == PipelineStage.NEEDS_REVIEW,
             ImportJob.encryption_policy == EncryptionPolicy.MIXED_MANUAL,
         ))
    if job_id:
        q = q.filter(ImportItem.job_id == job_id)
    return [ReviewItemResponse.model_validate(i) for i in q.all()]


@router.post("/review/encryption/submit", dependencies=[Depends(require_catalog_admin)])
async def submit_encryption_decisions(body: EncryptionSubmit, db: Session = Depends(get_db)):
    results = []
    for decision in body.decisions:
        item = db.get(ImportItem, decision.item_id)
        if not item:
            continue
        item.encrypted = decision.encrypted
        # Advance to RESOLVED — the worker re-dispatch mechanism is a TODO for Phase 2
        item.advance_stage(PipelineStage.RESOLVED, db, action="gate_c_encryption_decided")
        results.append(ReviewItemResponse.model_validate(item))
    return results


# --- Fuzzy match resolution ---

@router.get("/review/fuzzy", dependencies=[Depends(require_catalog_admin)], response_model=List[ReviewItemResponse])
async def list_fuzzy_review(job_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = (db.query(ImportItem)
         .filter(ImportItem.pipeline_stage == PipelineStage.NEEDS_REVIEW,
                 ImportItem.action_taken == ActionTaken.NEEDS_REVIEW))
    if job_id:
        q = q.filter(ImportItem.job_id == job_id)
    return [ReviewItemResponse.model_validate(i) for i in q.all()]


@router.post("/review/fuzzy/{item_id}/resolve", dependencies=[Depends(require_catalog_admin)], response_model=ReviewItemResponse)
async def resolve_fuzzy(item_id: int, body: FuzzyResolve, db: Session = Depends(get_db)):
    item = db.get(ImportItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    item.olid = body.olid
    item.advance_stage(PipelineStage.RESOLVED, db, action="fuzzy_manually_resolved", olid=body.olid)
    return ReviewItemResponse.model_validate(item)


@router.post("/review/fuzzy/{item_id}/skip", dependencies=[Depends(require_catalog_admin)], response_model=ReviewItemResponse)
async def skip_fuzzy(item_id: int, db: Session = Depends(get_db)):
    item = db.get(ImportItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    item.advance_stage(PipelineStage.SKIPPED, db, action="fuzzy_skipped")
    return ReviewItemResponse.model_validate(item)


# ---------------------------------------------------------------------------
# Manual single-book flow
# ---------------------------------------------------------------------------

@router.get("/manual/search", dependencies=[Depends(require_catalog_admin)])
async def manual_search(
    title: Optional[str] = None,
    author: Optional[str] = None,
    isbn: Optional[str] = None,
):
    from lenny.configs import GOOGLE_BOOKS_API_KEY
    meta = BookMetadata(
        title=title,
        authors=[author] if author else [],
        isbn_13=isbn if isbn and isbn.startswith("978") else None,
        isbn_10=isbn if isbn and not isbn.startswith("978") else None,
    )
    resolver = APIResolver(google_books_api_key=GOOGLE_BOOKS_API_KEY)
    result = resolver.lookup(meta)
    return {
        "status": result.status,
        "olid": result.olid,
        "confidence": result.confidence,
        "action": result.action,
        "candidates": [
            {
                "olid": c.olid,
                "title": c.title,
                "authors": c.authors,
                "year": c.year,
                "publisher": c.publisher,
                "score": c.score,
            }
            for c in result.candidates
        ],
    }


@router.post("/manual/link", dependencies=[Depends(require_catalog_admin)], status_code=201)
async def manual_link(body: FuzzyResolve, db: Session = Depends(get_db)):
    """Link an existing OLID directly to Lenny (no OL write needed)."""
    olid = body.olid
    existing = db.query(Item).filter(Item.openlibrary_edition == olid).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"OLID {olid} already exists in Lenny")
    lenny_item = Item(openlibrary_edition=olid, encrypted=False, formats=FormatEnum.EPUB)
    db.add(lenny_item)
    db.commit()
    db.refresh(lenny_item)
    return {"id": lenny_item.id, "olid": olid, "encrypted": False}


@router.post("/manual/create", dependencies=[Depends(require_catalog_admin)], status_code=201)
async def manual_create(body: ManualCreateRequest, db: Session = Depends(get_db)):
    """Create a new OL record for a book and optionally link it to Lenny."""
    from lenny.configs import GOOGLE_BOOKS_API_KEY
    if not ol_auth_status()["logged_in"]:
        raise HTTPException(status_code=503, detail="OL not authenticated. Run `make ol-login` first.")
    meta = BookMetadata(
        title=body.title,
        authors=body.authors,
        isbn_13=body.isbn_13,
        isbn_10=body.isbn_10,
        publisher=body.publisher,
        publish_date=body.publish_date,
        language=body.language,
    )
    resolver = APIResolver(google_books_api_key=GOOGLE_BOOKS_API_KEY)
    try:
        olid = resolver.create_edition(meta)
    except OLWriteError as e:
        raise HTTPException(status_code=502, detail=f"OL write failed: {e}")
    except Exception:
        logger.exception("Unexpected error in manual_create")
        raise HTTPException(status_code=500, detail="Unexpected error creating OL record")
    return {"olid": olid}


# ---------------------------------------------------------------------------
# OL credentials
# ---------------------------------------------------------------------------

@router.get("/ol/status", dependencies=[Depends(require_catalog_admin)])
async def ol_status():
    return ol_auth_status()
