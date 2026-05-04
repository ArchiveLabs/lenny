from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel

from lenny.catalog.types import (
    JobStatus, JobMode, Persona, ResolverType,
    InputMethod, EncryptionPolicy, PipelineStage,
    OLStatus, ActionTaken,
)


class CreateJobItemRequest(BaseModel):
    source_path: Optional[str] = None
    sha256: Optional[str] = None
    extracted_metadata: Optional[dict] = None


class CreateJobRequest(BaseModel):
    mode: JobMode
    persona: Persona
    input_method: InputMethod
    encryption_policy: EncryptionPolicy = EncryptionPolicy.ALL_ENCRYPTED
    dry_run: bool = False
    gate_a_enabled: bool = False
    gate_b_enabled: bool = False
    skip_ol: bool = False
    total: int = 0
    items: Optional[List[CreateJobItemRequest]] = None


class JobResponse(BaseModel):
    id: int
    status: JobStatus
    mode: JobMode
    persona: Persona
    input_method: InputMethod
    encryption_policy: EncryptionPolicy
    dry_run: bool
    gate_a_enabled: bool
    gate_b_enabled: bool
    skip_ol: bool
    total: int
    processed: int
    linked: int
    created_ol: int
    needs_review: int
    errors: int
    skipped: int
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ReviewItemResponse(BaseModel):
    id: int
    job_id: int
    pipeline_stage: PipelineStage
    source_path: Optional[str] = None
    extracted_title: Optional[str] = None
    extracted_author: Optional[str] = None
    extracted_isbn: Optional[str] = None
    extracted_metadata: Optional[dict] = None
    ol_status: Optional[OLStatus] = None
    confidence: Optional[float] = None
    olid: Optional[int] = None
    action_taken: Optional[ActionTaken] = None
    review_candidates: Optional[list] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class MetadataReviewSubmit(BaseModel):
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    publisher: Optional[str] = None


class OLCreationEdit(BaseModel):
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    publisher: Optional[str] = None
    publish_date: Optional[str] = None


class EncryptionDecision(BaseModel):
    item_id: int
    encrypted: bool


class EncryptionSubmit(BaseModel):
    decisions: List[EncryptionDecision]


class FuzzyResolve(BaseModel):
    olid: int


class ManualSearchRequest(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None


class OLConnectRequest(BaseModel):
    access_key: str
    secret_key: str
