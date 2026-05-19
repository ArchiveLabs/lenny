import datetime
from typing import Optional, Any

import sqlalchemy as sa
from sqlalchemy import Column, BigInteger, Boolean, Integer, String, Float, DateTime, Enum as SAEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from lenny.core.db import Base
from lenny.catalog.types import (
    PipelineStage, STAGE_TRANSITIONS, STAGE_CHECKPOINTS,
    JobStatus, JobMode, Persona, ResolverType,
    InputMethod, EncryptionPolicy, OLStatus, ActionTaken,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# sa.JSON works across SQLite (tests) and PostgreSQL (production).
# The migration creates the column as JSONB on PostgreSQL for indexing performance.
_JSON = sa.JSON

# SQLite does not support BigInteger autoincrement — use Integer variant for tests.
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")
# Non-PK BigInteger columns also need the sqlite variant for type-affinity consistency.
_BigInt = BigInteger().with_variant(Integer, "sqlite")

_COUNTER_COLUMNS = {"linked", "created_ol", "needs_review", "errors", "skipped"}

# PostgreSQL native enum types store the .value (lowercase), not the Python member name.
# values_callable ensures SQLAlchemy uses .value for serialization on all dialects.
def _pg_enum(enum_cls, name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, values_callable=lambda obj: [e.value for e in obj])


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id = Column(_BigIntPK, primary_key=True, autoincrement=True)
    status = Column(_pg_enum(JobStatus, "jobstatus"), nullable=False, default=JobStatus.PENDING)
    mode = Column(_pg_enum(JobMode, "jobmode"), nullable=False)
    persona = Column(_pg_enum(Persona, "persona"), nullable=False)
    resolver_type = Column(_pg_enum(ResolverType, "resolvertype"), nullable=False, default=ResolverType.API)
    input_method = Column(_pg_enum(InputMethod, "inputmethod"), nullable=False)
    encryption_policy = Column(_pg_enum(EncryptionPolicy, "encryptionpolicy"), nullable=False)
    dry_run = Column(Boolean, nullable=False, default=False)
    gate_a_enabled = Column(Boolean, nullable=False, default=False)
    gate_b_enabled = Column(Boolean, nullable=False, default=False)
    skip_ol = Column(Boolean, nullable=False, default=False)

    total = Column(Integer, nullable=False, default=0)
    processed = Column(Integer, nullable=False, default=0)
    linked = Column(Integer, nullable=False, default=0)
    created_ol = Column(Integer, nullable=False, default=0)
    needs_review = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    items = relationship("ImportItem", back_populates="job", cascade="all, delete-orphan")

    def increment(self, counter: str, session) -> None:
        """Atomically increment a job counter and the `processed` total.

        Uses an UPDATE statement (not read-modify-write) to avoid
        lost updates under concurrent workers.
        """
        if counter not in _COUNTER_COLUMNS:
            raise ValueError(f"Unknown counter: {counter!r}. Valid: {_COUNTER_COLUMNS}")
        session.execute(
            sa.update(ImportJob)
            .where(ImportJob.id == self.id)
            .values({counter: getattr(ImportJob, counter) + 1,
                     "processed": ImportJob.processed + 1})
        )
        session.commit()


class ImportItem(Base):
    __tablename__ = "import_items"
    __table_args__ = (
        sa.Index("idx_import_items_job_stage", "job_id", "pipeline_stage"),
        sa.Index("idx_import_items_sha256", "sha256"),
        sa.Index("idx_import_items_stage_updated", "pipeline_stage", "stage_updated_at"),
    )

    id = Column(_BigIntPK, primary_key=True, autoincrement=True)
    job_id = Column(_BigInt, sa.ForeignKey("import_jobs.id"), nullable=False)
    pipeline_stage = Column(
        _pg_enum(PipelineStage, "pipelinestage"),
        nullable=False,
        default=PipelineStage.PENDING,
    )
    stage_updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )
    retry_count = Column(Integer, nullable=False, default=0)
    source_path = Column(String, nullable=True)
    sha256 = Column(String(64), nullable=True)

    extracted_title = Column(String, nullable=True)
    extracted_author = Column(String, nullable=True)
    extracted_isbn = Column(String, nullable=True)
    extracted_metadata = Column(_JSON, nullable=True)

    ol_status = Column(_pg_enum(OLStatus, "olstatus"), nullable=True)
    confidence = Column(Float, nullable=True)
    olid = Column(_BigInt, nullable=True)
    action_taken = Column(_pg_enum(ActionTaken, "actiontaken"), nullable=True)

    encrypted = Column(Boolean, nullable=True)
    skip_ol = Column(Boolean, nullable=False, default=False)
    review_candidates = Column(_JSON, nullable=True)

    minio_key = Column(String, nullable=True)
    item_id = Column(_BigInt, sa.ForeignKey("items.id"), nullable=True)
    error_message = Column(String, nullable=True)
    action_log = Column(_JSON, nullable=False, default=list)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    job = relationship("ImportJob", back_populates="items")

    def advance_stage(self, new_stage: PipelineStage, session, **log_kwargs) -> None:
        allowed = STAGE_TRANSITIONS.get(self.pipeline_stage)
        if allowed is None:
            raise ValueError(f"No transitions defined for stage {self.pipeline_stage!r}")
        if new_stage not in allowed:
            raise ValueError(
                f"Invalid stage transition: {self.pipeline_stage!r} → {new_stage!r}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        # Allowlist log_kwargs keys to prevent accidental credential/object leakage into action_log
        _SAFE_LOG_KEYS = {"isbn", "title", "ol_status", "confidence", "olid", "action", "reason", "new_olid"}
        safe_kwargs = {k: str(v) for k, v in log_kwargs.items() if k in _SAFE_LOG_KEYS}
        log_entry = {"stage": new_stage.value, "ts": _utcnow().isoformat(), **safe_kwargs}
        # action_log is a list — must reassign to trigger SQLAlchemy change detection on JSON
        self.action_log = list(self.action_log or []) + [log_entry]
        self.pipeline_stage = new_stage
        self.stage_updated_at = _utcnow()
        session.add(self)
        session.commit()

    def mark_error(self, message: str, session, max_retries: int = 3) -> None:
        self.retry_count = (self.retry_count or 0) + 1
        self.error_message = message
        log_entry = {
            "stage": "error",
            "ts": _utcnow().isoformat(),
            "message": message,
            "retry_count": self.retry_count,
        }
        self.action_log = list(self.action_log or []) + [log_entry]

        if self.retry_count >= max_retries:
            self.pipeline_stage = PipelineStage.ERROR
        else:
            checkpoint = STAGE_CHECKPOINTS.get(self.pipeline_stage)
            if checkpoint:
                self.pipeline_stage = checkpoint
            else:
                self.pipeline_stage = PipelineStage.ERROR

        self.stage_updated_at = _utcnow()
        session.add(self)
        session.commit()

    @classmethod
    def reset_stale(cls, session, stale_after_seconds: int = 300) -> int:
        cutoff = _utcnow() - datetime.timedelta(seconds=stale_after_seconds)
        active_stages = list(STAGE_CHECKPOINTS.keys())
        stale = (
            session.query(cls)
            .filter(
                cls.pipeline_stage.in_(active_stages),
                cls.stage_updated_at < cutoff,
            )
            .all()
        )
        if not stale:
            return 0
        now = _utcnow()
        # Group by checkpoint so we can bulk-update stage+timestamp per transition type
        by_checkpoint: dict = {}
        for item in stale:
            checkpoint = STAGE_CHECKPOINTS[item.pipeline_stage]
            log_entry = {
                "stage": "reset_stale",
                "ts": now.isoformat(),
                "from": item.pipeline_stage.value,
                "to": checkpoint.value,
            }
            item.action_log = list(item.action_log or []) + [log_entry]
            by_checkpoint.setdefault(checkpoint, []).append(item.id)
        for checkpoint, ids in by_checkpoint.items():
            session.execute(
                sa.update(cls)
                .where(cls.id.in_(ids))
                .values(pipeline_stage=checkpoint, stage_updated_at=now)
            )
        session.commit()
        return len(stale)

    @classmethod
    def claim_pending(cls, session, job_id: int, limit: int = 1):
        """Claim pending items atomically. PostgreSQL only (uses SKIP LOCKED)."""
        return (
            session.query(cls)
            .filter(cls.job_id == job_id, cls.pipeline_stage == PipelineStage.PENDING)
            .with_for_update(skip_locked=True)
            .limit(limit)
            .all()
        )

    @classmethod
    def sha256_exists(cls, session, sha256: str) -> bool:
        return (
            session.query(cls)
            .filter(cls.sha256 == sha256, cls.pipeline_stage != PipelineStage.ERROR)
            .first()
        ) is not None
