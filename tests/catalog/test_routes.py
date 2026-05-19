import json
import lenny.catalog.models  # noqa: F401
import lenny.core.models  # noqa: F401
from lenny.catalog.types import JobMode, Persona, InputMethod, EncryptionPolicy, JobStatus
from tests.catalog.conftest import admin_headers


def make_create_job_body(**overrides):
    body = {
        "mode": "full_import",
        "persona": "library",
        "input_method": "epub_folder",
        "encryption_policy": "all_encrypted",
        "dry_run": False,
        "gate_a_enabled": False,
        "gate_b_enabled": False,
        "skip_ol": False,
        "total": 0,
    }
    body.update(overrides)
    return body


def test_schemas_importable():
    from lenny.catalog.schemas import (
        CreateJobRequest, CreateJobItemRequest,
        JobResponse, ReviewItemResponse,
        MetadataReviewSubmit, OLCreationEdit,
        EncryptionDecision, EncryptionSubmit,
        FuzzyResolve, ManualSearchRequest,
    )
    assert CreateJobRequest is not None


def test_catalog_router_requires_admin_auth():
    from fastapi.testclient import TestClient
    from lenny.app import app
    client = TestClient(app)
    # No auth — should get 401
    r = client.get("/v1/api/catalog/jobs")
    assert r.status_code == 401


def test_create_job_returns_201(client, db_session):
    r = client.post("/v1/api/catalog/jobs", json=make_create_job_body(), headers=admin_headers())
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "pending"
    assert data["mode"] == "full_import"
    assert "id" in data


def test_list_jobs_returns_created_job(client, db_session):
    client.post("/v1/api/catalog/jobs", json=make_create_job_body(), headers=admin_headers())
    r = client.get("/v1/api/catalog/jobs", headers=admin_headers())
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_get_job_by_id(client, db_session):
    created = client.post("/v1/api/catalog/jobs", json=make_create_job_body(), headers=admin_headers()).json()
    job_id = created["id"]
    r = client.get(f"/v1/api/catalog/jobs/{job_id}", headers=admin_headers())
    assert r.status_code == 200
    assert r.json()["id"] == job_id


def test_get_job_not_found(client, db_session):
    r = client.get("/v1/api/catalog/jobs/99999", headers=admin_headers())
    assert r.status_code == 404


def test_create_job_with_items_sets_total_and_running(client, db_session):
    from lenny.catalog.models import ImportItem
    body = make_create_job_body(items=[
        {"source_path": "/tmp/a.epub", "sha256": "aaa"},
        {"source_path": "/tmp/b.epub", "sha256": "bbb"},
    ])
    r = client.post("/v1/api/catalog/jobs", json=body, headers=admin_headers())
    assert r.status_code == 201
    data = r.json()
    assert data["total"] == 2
    assert data["status"] == "running"
    assert db_session.query(ImportItem).count() == 2


def test_pause_running_job(client, db_session):
    body = make_create_job_body(items=[{"source_path": "/tmp/a.epub", "sha256": "aaa"}])
    job_id = client.post("/v1/api/catalog/jobs", json=body, headers=admin_headers()).json()["id"]
    r = client.post(f"/v1/api/catalog/jobs/{job_id}/pause", headers=admin_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "paused"


def test_resume_paused_job(client, db_session):
    body = make_create_job_body(items=[{"source_path": "/tmp/a.epub", "sha256": "aaa"}])
    job_id = client.post("/v1/api/catalog/jobs", json=body, headers=admin_headers()).json()["id"]
    client.post(f"/v1/api/catalog/jobs/{job_id}/pause", headers=admin_headers())
    r = client.post(f"/v1/api/catalog/jobs/{job_id}/resume", headers=admin_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_cancel_job(client, db_session):
    body = make_create_job_body(items=[{"source_path": "/tmp/a.epub", "sha256": "aaa"}])
    job_id = client.post("/v1/api/catalog/jobs", json=body, headers=admin_headers()).json()["id"]
    r = client.delete(f"/v1/api/catalog/jobs/{job_id}", headers=admin_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_pause_nonexistent_job_returns_404(client, db_session):
    r = client.post("/v1/api/catalog/jobs/99999/pause", headers=admin_headers())
    assert r.status_code == 404


def _make_job(db_session):
    from lenny.catalog.models import ImportJob
    from lenny.catalog.types import JobStatus, JobMode, Persona, ResolverType, InputMethod, EncryptionPolicy
    job = ImportJob(
        mode=JobMode.FULL_IMPORT, persona=Persona.LIBRARY,
        resolver_type=ResolverType.API,
        input_method=InputMethod.EPUB_FOLDER,
        encryption_policy=EncryptionPolicy.ALL_ENCRYPTED,
        dry_run=False, gate_a_enabled=True, gate_b_enabled=True,
        skip_ol=False, total=1, status=JobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _make_needs_review_item(db_session, job_id, **kwargs):
    from lenny.catalog.models import ImportItem
    from lenny.catalog.types import PipelineStage
    defaults = dict(
        job_id=job_id, pipeline_stage=PipelineStage.NEEDS_REVIEW,
        source_path="/tmp/test.epub", sha256="abc123",
        retry_count=0, action_log=[],
    )
    defaults.update(kwargs)
    item = ImportItem(**defaults)
    db_session.add(item)
    db_session.commit()
    return item


def test_gate_a_metadata_review_lists_items(client, db_session):
    job = _make_job(db_session)
    _make_needs_review_item(db_session, job.id, extracted_title=None)
    r = client.get(f"/v1/api/catalog/review/metadata?job_id={job.id}", headers=admin_headers())
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1


def test_gate_a_metadata_submit_corrects_item(client, db_session):
    from lenny.catalog.types import PipelineStage
    job = _make_job(db_session)
    item = _make_needs_review_item(db_session, job.id)
    body = {"title": "Fixed Title", "authors": ["Fixed Author"], "isbn_13": "9781234567890"}
    r = client.post(f"/v1/api/catalog/review/metadata/{item.id}", json=body, headers=admin_headers())
    assert r.status_code == 200
    from lenny.catalog.models import ImportItem
    db_session.refresh(item)
    assert item.extracted_title == "Fixed Title"
    # FSM CORRECTION: NEEDS_REVIEW → RESOLVED (not EXTRACTED, which is not an allowed transition)
    assert item.pipeline_stage == PipelineStage.RESOLVED


def test_gate_b_ol_creation_review_lists_items(client, db_session):
    from lenny.catalog.types import ActionTaken
    job = _make_job(db_session)
    _make_needs_review_item(db_session, job.id, action_taken=ActionTaken.CREATE_FULL)
    r = client.get(f"/v1/api/catalog/review/ol-creation?job_id={job.id}", headers=admin_headers())
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_gate_b_ol_creation_approve(client, db_session):
    from lenny.catalog.types import ActionTaken, PipelineStage
    job = _make_job(db_session)
    item = _make_needs_review_item(db_session, job.id, action_taken=ActionTaken.CREATE_FULL,
                                   pipeline_stage=PipelineStage.NEEDS_REVIEW)
    r = client.post(f"/v1/api/catalog/review/ol-creation/{item.id}/approve", headers=admin_headers())
    assert r.status_code == 200
    db_session.refresh(item)
    # CORRECTED: Gate B approve advances to RESOLVED (not OL_WRITING)
    assert item.pipeline_stage == PipelineStage.RESOLVED


def test_gate_c_encryption_review_lists_items(client, db_session):
    from lenny.catalog.models import ImportJob
    from lenny.catalog.types import JobStatus, JobMode, Persona, ResolverType, InputMethod, EncryptionPolicy
    # Gate C only returns items from jobs with MIXED_MANUAL encryption policy
    job = ImportJob(
        mode=JobMode.FULL_IMPORT, persona=Persona.LIBRARY,
        resolver_type=ResolverType.API,
        input_method=InputMethod.EPUB_FOLDER,
        encryption_policy=EncryptionPolicy.MIXED_MANUAL,
        dry_run=False, gate_a_enabled=True, gate_b_enabled=True,
        skip_ol=False, total=1, status=JobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()
    _make_needs_review_item(db_session, job.id)
    r = client.get(f"/v1/api/catalog/review/encryption?job_id={job.id}", headers=admin_headers())
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_gate_c_encryption_submit(client, db_session):
    from lenny.catalog.types import PipelineStage
    job = _make_job(db_session)
    item = _make_needs_review_item(db_session, job.id)
    body = {"decisions": [{"item_id": item.id, "encrypted": True}]}
    r = client.post("/v1/api/catalog/review/encryption/submit", json=body, headers=admin_headers())
    assert r.status_code == 200
    db_session.refresh(item)
    assert item.encrypted is True
    # FSM: NEEDS_REVIEW only allows → RESOLVED or SKIPPED; advances to RESOLVED so the
    # worker proceeds to OL_DONE → UPLOADING via the normal pipeline.
    assert item.pipeline_stage == PipelineStage.RESOLVED


def test_fuzzy_review_lists_items(client, db_session):
    from lenny.catalog.types import ActionTaken, OLStatus
    job = _make_job(db_session)
    _make_needs_review_item(db_session, job.id,
                             action_taken=ActionTaken.NEEDS_REVIEW,
                             ol_status=OLStatus.OL_MATCH_FUZZY,
                             review_candidates=[{"olid": 123, "score": 0.85}])
    r = client.get(f"/v1/api/catalog/review/fuzzy?job_id={job.id}", headers=admin_headers())
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_fuzzy_resolve_sets_olid_and_advances(client, db_session):
    from lenny.catalog.types import ActionTaken, OLStatus, PipelineStage
    job = _make_job(db_session)
    item = _make_needs_review_item(db_session, job.id,
                                    action_taken=ActionTaken.NEEDS_REVIEW,
                                    ol_status=OLStatus.OL_MATCH_FUZZY)
    r = client.post(f"/v1/api/catalog/review/fuzzy/{item.id}/resolve",
                    json={"olid": 99999}, headers=admin_headers())
    assert r.status_code == 200
    db_session.refresh(item)
    assert item.olid == 99999
    assert item.pipeline_stage == PipelineStage.RESOLVED


def test_fuzzy_skip_advances_to_skipped(client, db_session):
    from lenny.catalog.types import PipelineStage, ActionTaken
    job = _make_job(db_session)
    item = _make_needs_review_item(db_session, job.id, action_taken=ActionTaken.NEEDS_REVIEW)
    r = client.post(f"/v1/api/catalog/review/fuzzy/{item.id}/skip", headers=admin_headers())
    assert r.status_code == 200
    db_session.refresh(item)
    assert item.pipeline_stage == PipelineStage.SKIPPED


def test_manual_search_returns_candidates(client, db_session):
    from unittest.mock import patch, MagicMock
    from lenny.catalog.types import OLStatus, ActionTaken
    from lenny.catalog.resolver import OLResult
    mock_result = OLResult(
        status=OLStatus.OL_MATCH_CLEAN,
        olid=12345,
        confidence=0.97,
        action=ActionTaken.LINK_ONLY,
        candidates=[],
    )
    with patch("lenny.catalog.routes.APIResolver") as MockResolver:
        instance = MockResolver.return_value
        instance.lookup.return_value = mock_result
        r = client.get("/v1/api/catalog/manual/search?title=Dune&author=Frank+Herbert",
                       headers=admin_headers())
    assert r.status_code == 200
    data = r.json()
    assert data["olid"] == 12345
    assert data["confidence"] == 0.97


def test_manual_link_creates_lenny_item(client, db_session):
    """manual_link creates a Lenny Item row and returns 201 with the olid."""
    from lenny.core.models import Item
    r = client.post(
        "/v1/api/catalog/manual/link",
        json={"olid": 12345},
        headers=admin_headers(),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["olid"] == 12345
    assert db_session.query(Item).filter(Item.openlibrary_edition == 12345).count() == 1


def test_manual_link_rejects_duplicate_olid(client, db_session):
    """manual_link returns 409 when the OLID already exists in Lenny."""
    client.post("/v1/api/catalog/manual/link", json={"olid": 99999}, headers=admin_headers())
    r = client.post("/v1/api/catalog/manual/link", json={"olid": 99999}, headers=admin_headers())
    assert r.status_code == 409


def test_ol_status_returns_logged_in_state(client, db_session):
    import lenny.configs as cfg
    original_access, original_secret = cfg.OL_S3_ACCESS_KEY, cfg.OL_S3_SECRET_KEY
    cfg.OL_S3_ACCESS_KEY = "myaccesskey"
    cfg.OL_S3_SECRET_KEY = "mysecretkey"
    try:
        r = client.get("/v1/api/catalog/ol/status", headers=admin_headers())
    finally:
        cfg.OL_S3_ACCESS_KEY = original_access
        cfg.OL_S3_SECRET_KEY = original_secret
    assert r.status_code == 200
    data = r.json()
    assert data["logged_in"] is True


def test_ol_status_returns_logged_out_when_no_creds(client, db_session):
    import lenny.configs as cfg
    original_access, original_secret = cfg.OL_S3_ACCESS_KEY, cfg.OL_S3_SECRET_KEY
    cfg.OL_S3_ACCESS_KEY = None
    cfg.OL_S3_SECRET_KEY = None
    try:
        r = client.get("/v1/api/catalog/ol/status", headers=admin_headers())
    finally:
        cfg.OL_S3_ACCESS_KEY = original_access
        cfg.OL_S3_SECRET_KEY = original_secret
    assert r.status_code == 200
    assert r.json()["logged_in"] is False


def test_sse_stream_returns_job_progress(client, db_session):
    """SSE endpoint returns at least one progress event and closes on terminal state."""
    from lenny.catalog.models import ImportJob
    from lenny.catalog.types import JobStatus, JobMode, Persona, ResolverType, InputMethod, EncryptionPolicy
    # Use COMPLETED so the generator terminates immediately after one event (no 2-second sleep).
    job = ImportJob(
        mode=JobMode.FULL_IMPORT,
        persona=Persona.LIBRARY,
        resolver_type=ResolverType.API,
        input_method=InputMethod.EPUB_FOLDER,
        encryption_policy=EncryptionPolicy.ALL_ENCRYPTED,
        dry_run=False, gate_a_enabled=False, gate_b_enabled=False, skip_ol=False,
        total=10, processed=10, linked=8, created_ol=2, needs_review=0, errors=0, skipped=0,
        status=JobStatus.COMPLETED,
    )
    db_session.add(job)
    db_session.commit()

    # Use stream=True to consume the SSE response
    with client.stream("GET", f"/v1/api/catalog/jobs/{job.id}/stream", headers=admin_headers()) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Read first event
        for line in resp.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                assert payload["id"] == job.id
                assert payload["processed"] == 10
                assert payload["status"] == "completed"
                break
