from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


# ---------------------------------------------------------------------------
# Enums — all inherit str so SQLAlchemy Enum columns work without mapping
# ---------------------------------------------------------------------------

class PipelineStage(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    OL_WRITING = "ol_writing"
    OL_DONE = "ol_done"
    UPLOADING = "uploading"
    DONE = "done"
    ERROR = "error"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


# Legal forward-only transitions. Any move not in this map is rejected.
STAGE_TRANSITIONS: dict[PipelineStage, list[PipelineStage]] = {
    PipelineStage.PENDING:     [PipelineStage.EXTRACTING],
    PipelineStage.EXTRACTING:  [PipelineStage.EXTRACTED, PipelineStage.ERROR, PipelineStage.SKIPPED],
    PipelineStage.EXTRACTED:   [PipelineStage.RESOLVING, PipelineStage.NEEDS_REVIEW],
    PipelineStage.RESOLVING:   [PipelineStage.RESOLVED, PipelineStage.ERROR],
    PipelineStage.RESOLVED:    [PipelineStage.OL_WRITING, PipelineStage.OL_DONE, PipelineStage.NEEDS_REVIEW],
    PipelineStage.OL_WRITING:  [PipelineStage.OL_DONE, PipelineStage.ERROR],
    PipelineStage.OL_DONE:     [PipelineStage.UPLOADING, PipelineStage.DONE],
    PipelineStage.UPLOADING:   [PipelineStage.DONE, PipelineStage.ERROR],
    # Terminal stages — no forward transitions
    PipelineStage.DONE:        [],
    PipelineStage.ERROR:       [],
    PipelineStage.NEEDS_REVIEW: [PipelineStage.RESOLVED, PipelineStage.SKIPPED],
    PipelineStage.SKIPPED:     [],
}

# The last committed checkpoint for each active stage.
# On crash recovery, stuck items in an active stage are reset to their checkpoint.
STAGE_CHECKPOINTS: dict[PipelineStage, PipelineStage] = {
    PipelineStage.EXTRACTING:  PipelineStage.PENDING,
    PipelineStage.RESOLVING:   PipelineStage.EXTRACTED,
    PipelineStage.OL_WRITING:  PipelineStage.RESOLVED,
    PipelineStage.UPLOADING:   PipelineStage.OL_DONE,
}


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class JobMode(str, Enum):
    METADATA_SYNC = "metadata_sync"
    FULL_IMPORT = "full_import"


class Persona(str, Enum):
    PUBLISHER = "publisher"
    LIBRARY = "library"
    AUTHOR = "author"


class ResolverType(str, Enum):
    API = "api"
    DUMP = "dump"


class InputMethod(str, Enum):
    EPUB_FOLDER = "epub_folder"
    EPUB_SIDECAR = "epub_sidecar"
    CSV = "csv"
    MARC = "marc"
    OPDS = "opds"
    ONIX = "onix"
    VENDOR_API = "vendor_api"


class EncryptionPolicy(str, Enum):
    ALL_ENCRYPTED = "all_encrypted"
    ALL_OPEN = "all_open"
    MIXED_AUTO = "mixed_auto"
    MIXED_MANUAL = "mixed_manual"


class OLStatus(str, Enum):
    OL_MATCH_CLEAN = "OL_MATCH_CLEAN"
    OL_MATCH_FUZZY = "OL_MATCH_FUZZY"
    OL_WORK_ONLY = "OL_WORK_ONLY"
    OL_NOT_FOUND = "OL_NOT_FOUND"
    INSUFFICIENT_METADATA = "INSUFFICIENT_METADATA"


class ActionTaken(str, Enum):
    LINK_ONLY = "LINK_ONLY"
    CREATE_FULL = "CREATE_FULL"
    SKIPPED_OL = "SKIPPED_OL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BookMetadata:
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    publisher: Optional[str] = None
    publish_date: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    subjects: List[str] = field(default_factory=list)
    source: str = "unknown"

    @property
    def best_isbn(self) -> Optional[str]:
        return self.isbn_13 or self.isbn_10

    @property
    def primary_author(self) -> Optional[str]:
        return self.authors[0] if self.authors else None

    @property
    def is_resolvable(self) -> bool:
        has_isbn = bool(self.isbn_13 or self.isbn_10)
        has_title_and_author = bool(self.title and self.authors)
        return has_isbn or has_title_and_author


@dataclass
class OLCandidate:
    olid: int
    title: str
    authors: List[str]
    year: Optional[str]
    publisher: Optional[str]
    score: float


# Confidence thresholds — single source of truth, imported by resolver.py too
OL_AUTO_LINK_THRESHOLD: float = 0.95
OL_REVIEW_THRESHOLD: float = 0.70


@dataclass
class OLResult:
    status: OLStatus
    olid: Optional[int] = None
    confidence: float = 0.0
    candidates: List[OLCandidate] = field(default_factory=list)
    action: Optional[ActionTaken] = None

    @property
    def should_auto_link(self) -> bool:
        return self.confidence >= OL_AUTO_LINK_THRESHOLD and self.olid is not None

    @property
    def needs_review(self) -> bool:
        return OL_REVIEW_THRESHOLD <= self.confidence < OL_AUTO_LINK_THRESHOLD and self.olid is not None

