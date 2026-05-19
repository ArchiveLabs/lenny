import pytest
from lenny.catalog.types import (
    BookMetadata, OLResult, OLCandidate,
    PipelineStage, OLStatus, ActionTaken,
    JobMode, JobStatus, Persona, EncryptionPolicy, InputMethod,
)


def test_book_metadata_is_resolvable_with_isbn():
    m = BookMetadata(title="Dune", authors=["Frank Herbert"], isbn_13="9780441013593")
    assert m.is_resolvable is True


def test_book_metadata_is_resolvable_with_title_and_author():
    m = BookMetadata(title="Dune", authors=["Frank Herbert"])
    assert m.is_resolvable is True


def test_book_metadata_not_resolvable_without_title_or_isbn():
    m = BookMetadata(authors=["Frank Herbert"])
    assert m.is_resolvable is False


def test_book_metadata_not_resolvable_empty():
    m = BookMetadata()
    assert m.is_resolvable is False


def test_book_metadata_best_isbn_prefers_13():
    m = BookMetadata(isbn_13="9780441013593", isbn_10="0441013591")
    assert m.best_isbn == "9780441013593"


def test_book_metadata_best_isbn_falls_back_to_10():
    m = BookMetadata(isbn_10="0441013591")
    assert m.best_isbn == "0441013591"


def test_book_metadata_best_isbn_none_when_absent():
    m = BookMetadata(title="No ISBN Book")
    assert m.best_isbn is None


def test_book_metadata_primary_author_returns_first():
    m = BookMetadata(authors=["Frank Herbert", "Brian Herbert"])
    assert m.primary_author == "Frank Herbert"


def test_book_metadata_primary_author_none_when_empty():
    m = BookMetadata()
    assert m.primary_author is None


def test_ol_result_auto_link_confidence():
    r = OLResult(status=OLStatus.OL_MATCH_CLEAN, olid=12345, confidence=0.97)
    assert r.should_auto_link is True


def test_ol_result_review_queue_confidence():
    r = OLResult(status=OLStatus.OL_MATCH_FUZZY, olid=12345, confidence=0.82)
    assert r.should_auto_link is False
    assert r.needs_review is True


def test_ol_result_create_needed():
    r = OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0, action=ActionTaken.CREATE_FULL)
    assert r.should_auto_link is False
    assert r.needs_review is False


def test_pipeline_stage_ordering():
    assert PipelineStage.PENDING != PipelineStage.EXTRACTED
    assert PipelineStage.OL_DONE != PipelineStage.DONE


def test_enums_are_string_subclass():
    assert isinstance(PipelineStage.PENDING, str)
    assert isinstance(JobStatus.RUNNING, str)
    assert isinstance(OLStatus.OL_MATCH_CLEAN, str)
    assert isinstance(InputMethod.CSV, str)
    assert isinstance(EncryptionPolicy.ALL_ENCRYPTED, str)
