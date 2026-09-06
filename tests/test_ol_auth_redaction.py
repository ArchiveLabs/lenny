"""The OL S3 credential must survive the redaction hook and reach the wire.

`_redact_auth_header` exists to keep `Authorization: LOW <access>:<secret>` out
of httpx logs and exception reprs. It was registered as a **request** hook —
but httpx calls those with the live outgoing Request *before* transmission, so
it was not redacting a log line, it was redacting the real credential.

Open Library received `Authorization: LOW [REDACTED]`, found no `:` to split on,
and answered `{"error": "missing_or_invalid_authorization"}` at HTTP 200. Every
OTP issue, every OTP redeem, and every authenticated OL search was affected.
It went unnoticed because OL only began *checking* the header when
`_require_s3_auth` shipped; before that a blanked credential was harmless.

Reproduced against production 2026-08-28: the identical request sent with a
plain `httpx.Client` succeeded, and with `event_hooks=_REDACT_HOOKS` failed.
"""

import os
from unittest.mock import patch

import httpx
import pytest

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"

ACCESS, SECRET = "testaccess", "testsecret"


@pytest.fixture
def ol_keys():
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", ACCESS), \
         patch.object(configs, "OL_S3_SECRET_KEY", SECRET):
        yield


def test_hook_is_registered_on_response_not_request():
    """A request hook mutates the outgoing credential; a response hook cannot."""
    from lenny.core.openlibrary import _REDACT_HOOKS

    assert "request" not in _REDACT_HOOKS, (
        "a request hook runs before transmission and would blank the real credential"
    )
    assert "response" in _REDACT_HOOKS


def test_credential_reaches_the_wire_intact(ol_keys):
    """The end-to-end guard: capture what the transport actually receives."""
    from lenny.core.openlibrary import _REDACT_HOOKS, ol_auth_headers

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"success": "issued"})

    with httpx.Client(transport=httpx.MockTransport(handler),
                      event_hooks=_REDACT_HOOKS) as client:
        client.post("https://openlibrary.org/account/otp/issue",
                    headers=ol_auth_headers())

    assert seen["auth"] == f"LOW {ACCESS}:{SECRET}"
    assert "REDACTED" not in seen["auth"]


def test_open_library_would_accept_what_we_send(ol_keys):
    """Run the transmitted header through Open Library's own parsing rules
    (openlibrary/plugins/upstream/account.py::_parse_low_auth_header), so this
    test fails for the same reason production did rather than merely asserting
    on a string we chose."""
    from lenny.core.openlibrary import _REDACT_HOOKS, ol_auth_headers

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"success": "issued"})

    with httpx.Client(transport=httpx.MockTransport(handler),
                      event_hooks=_REDACT_HOOKS) as client:
        client.post("https://openlibrary.org/account/otp/issue",
                    headers=ol_auth_headers())

    header = seen["auth"]
    assert header.startswith("LOW ")
    keys = header.split("LOW ", 1)[1]
    assert ":" in keys, "OL raises here -> missing_or_invalid_authorization"
    access, secret = (part.strip() for part in keys.split(":", 1))
    assert access == ACCESS and secret == SECRET


def test_credential_is_redacted_after_the_exchange(ol_keys):
    """The protection the hook was written for still holds: once the response is
    in hand, nothing logging or repr-ing the request can leak the secret."""
    from lenny.core.openlibrary import _REDACT_HOOKS, ol_auth_headers

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": "issued"})

    with httpx.Client(transport=httpx.MockTransport(handler),
                      event_hooks=_REDACT_HOOKS) as client:
        response = client.post("https://openlibrary.org/account/otp/issue",
                               headers=ol_auth_headers())

    assert response.request.headers["Authorization"] == "LOW [REDACTED]"
    assert SECRET not in str(response.request.headers)


def test_hook_ignores_requests_with_no_low_credential():
    from lenny.core.openlibrary import _redact_auth_header

    request = httpx.Request("GET", "https://openlibrary.org/search.json")
    response = httpx.Response(200, request=request)
    _redact_auth_header(response)
    assert "Authorization" not in response.request.headers

    request = httpx.Request("GET", "https://openlibrary.org/search.json",
                            headers={"Authorization": "Bearer abc"})
    response = httpx.Response(200, request=request)
    _redact_auth_header(response)
    assert response.request.headers["Authorization"] == "Bearer abc"


def test_otp_issue_sends_the_real_credential(ol_keys):
    """OTP.issue is the call that was failing in production. Pin it directly."""
    from lenny.core import auth

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"success": "issued"})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("http2", None)
        kwargs.pop("verify", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch("lenny.core.auth.OTP._check_lending_enabled"), \
         patch("lenny.core.auth.httpx.Client", fake_client):
        auth.OTP.issue("patron@example.org", "1.2.3.4")

    assert seen["auth"] == f"LOW {ACCESS}:{SECRET}"
