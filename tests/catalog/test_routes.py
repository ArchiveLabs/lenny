import os
import json
import pytest
from fastapi.testclient import TestClient


def test_schemas_importable():
    from lenny.catalog.schemas import (
        CreateJobRequest, CreateJobItemRequest,
        JobResponse, ReviewItemResponse,
        MetadataReviewSubmit, OLCreationEdit,
        EncryptionDecision, EncryptionSubmit,
        FuzzyResolve, ManualSearchRequest,
        OLConnectRequest,
    )
    assert CreateJobRequest is not None
