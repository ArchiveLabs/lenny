"""`LennyClient.upload` must not dump the whole HTTP response body into the log.

Logging an entire response body is the clear-text-logging anti-pattern
(CWE-532, the family CodeQL's py/clear-text-logging-sensitive-data belongs to):
the admin `/upload` response is a status message today, but a log line that
echoes an *arbitrary* response body leaks whatever that body may later carry
(an error page, an echoed header, a stack trace). The line exists to record the
outcome, so it logs the status code, not the bytes.

Scanner note: CodeQL did not flag this site; it was found by enumerating every
logging sink in lenny/ for this class, not by trusting the four it reported.
"""

import logging
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

os.environ.setdefault("TESTING", "true")

SENTINEL = "RESPONSE-BODY-MUST-NOT-BE-LOGGED"


def test_upload_logs_status_not_the_response_body(caplog):
    from lenny.core.client import LennyClient

    def fake_post(url, **kwargs):
        return MagicMock(
            status_code=200,
            content=SENTINEL.encode(),
            raise_for_status=MagicMock(),
        )

    fake_client = MagicMock()
    fake_client.__enter__.return_value.post.side_effect = fake_post

    with caplog.at_level(logging.INFO, logger="lenny.core.client"), \
            patch("lenny.core.client.auth.internal_admin_headers", return_value={}), \
            patch("lenny.core.client.httpx.Client", return_value=fake_client):
        assert LennyClient.upload(930000002, BytesIO(b"PK\x03\x04")) is True

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in logged, "upload dumped the raw response body into the log"
    assert "200" in logged, "upload should still record the response status"
