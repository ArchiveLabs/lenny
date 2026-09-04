"""OTP failures must be visible.

Open Library's `/account/otp/{issue,redeem}` answer **HTTP 200 with a JSON error
body** on every failure path — `delegate.RawText` never raises, so there is no
non-2xx and nothing for Sentry to catch. Confirmed against production:

    POST /account/otp/issue?email=...&ip=...&sendmail=false
    -> HTTP 200  {"error": "missing_or_invalid_authorization"}

Lenny then discarded that response entirely and showed the patron "we sent you a
one-time password" regardless. The failure was invisible on both sides.

These tests pin the new behaviour: parse the body, log the reason, raise a typed
error carrying Open Library's own code, and never claim success we did not get.
"""

import json
import logging
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"


def _response(payload, status_code=200, text=None):
    """An httpx.Response the way Open Library actually replies: 200 + JSON body."""
    return httpx.Response(
        status_code=status_code,
        content=text if text is not None else json.dumps(payload),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "https://openlibrary.org/account/otp/issue"),
    )


@pytest.fixture
def ol_lending_enabled():
    """Bypass the lending-mode gate so tests exercise the HTTP path itself."""
    with patch("lenny.core.auth.OTP._check_lending_enabled"):
        yield


def _post_returns(response):
    """Patch httpx.Client so `with httpx.Client(...) as c: c.post(...)` yields `response`."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if isinstance(response, Exception):
        client.post.side_effect = response
    else:
        client.post.return_value = response
    return patch("lenny.core.auth.httpx.Client", return_value=client), client


# ---------------------------------------------------------------------------
# issue()
# ---------------------------------------------------------------------------

def test_issue_returns_payload_on_success(ol_lending_enabled):
    from lenny.core.auth import OTP

    patcher, _ = _post_returns(_response({"success": "issued"}))
    with patcher:
        assert OTP.issue("a@b.org", "1.2.3.4") == {"success": "issued"}


@pytest.mark.parametrize(
    "code",
    [
        "missing_or_invalid_authorization",
        "unauthorized",
        "auth_service_unavailable",
        "ratelimit",
        "missing_keys",
        "challenge_failed",
    ],
)
def test_issue_raises_on_every_ol_error_code(ol_lending_enabled, code):
    """Each of these arrives as HTTP 200. None may be mistaken for success."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": code}))
    with patcher, pytest.raises(OTPGenerationError) as exc:
        OTP.issue("a@b.org", "1.2.3.4")

    assert exc.value.code == code
    assert str(exc.value), "the patron needs a message, not an empty exception"


def test_issue_error_message_names_the_real_problem(ol_lending_enabled):
    """`unauthorized` means this library's credentials are stale — an operator
    action. It must not read as though the patron did something wrong."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": "unauthorized"}))
    with patcher, pytest.raises(OTPGenerationError) as exc:
        OTP.issue("a@b.org", "1.2.3.4")

    assert "ol-login" in str(exc.value)


def test_issue_logs_the_error_code(ol_lending_enabled, caplog):
    """The operator-facing half: the raw code has to reach the log, because
    Open Library's 200 means it will never reach Sentry."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": "ratelimit", "ratelimit": {"ttl": 60}}))
    with patcher, caplog.at_level(logging.ERROR, logger="lenny.core.auth"):
        with pytest.raises(OTPGenerationError):
            OTP.issue("alice@example.org", "1.2.3.4")

    assert "ratelimit" in caplog.text
    assert "issue" in caplog.text


def test_logs_mask_the_patron_email(ol_lending_enabled, caplog):
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": "unauthorized"}))
    with patcher, caplog.at_level(logging.ERROR, logger="lenny.core.auth"):
        with pytest.raises(OTPGenerationError):
            OTP.issue("alice@example.org", "1.2.3.4")

    assert "alice@example.org" not in caplog.text
    assert "al***@example.org" in caplog.text


def test_issue_raises_on_non_json_body(ol_lending_enabled):
    """A WAF block page or HTML error must not crash with a bare JSONDecodeError."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response(None, status_code=403, text="<html>Forbidden</html>"))
    with patcher, pytest.raises(OTPGenerationError):
        OTP.issue("a@b.org", "1.2.3.4")


def test_issue_raises_on_timeout(ol_lending_enabled):
    """Open Library validates our S3 keys against xauthn on every OTP request, so
    this call can be slow. A timeout is a distinct, reportable condition."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(httpx.ReadTimeout("timed out"))
    with patcher, pytest.raises(OTPGenerationError) as exc:
        OTP.issue("a@b.org", "1.2.3.4")

    assert exc.value.code == "timeout"


def test_issue_raises_on_transport_error(ol_lending_enabled):
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(httpx.ConnectError("no route"))
    with patcher, pytest.raises(OTPGenerationError) as exc:
        OTP.issue("a@b.org", "1.2.3.4")

    assert exc.value.code == "transport"


def test_issue_read_timeout_outlasts_ol_xauthn_hop():
    """Regression guard on the 5s read timeout: Open Library's handler makes its
    own network call to xauthn before answering, and 5s was not enough."""
    from lenny.core.auth import TIMEOUT

    assert TIMEOUT.read >= 15, "read timeout must accommodate OL's xauthn round-trip"


def test_issue_still_checks_lending_mode_first():
    """The credentials gate must run before any network call."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import LendingNotConfiguredError

    patcher, client = _post_returns(_response({"success": "issued"}))
    with patch("lenny.core.auth.OTP._check_lending_enabled",
               side_effect=LendingNotConfiguredError("nope")), patcher:
        with pytest.raises(LendingNotConfiguredError):
            OTP.issue("a@b.org", "1.2.3.4")

    client.post.assert_not_called()


# ---------------------------------------------------------------------------
# redeem()
# ---------------------------------------------------------------------------

def test_redeem_true_on_success(ol_lending_enabled):
    from lenny.core.auth import OTP

    patcher, _ = _post_returns(_response({"success": "redeemed"}))
    with patcher:
        assert OTP.redeem("a@b.org", "1.2.3.4", "abc123") is True


def test_redeem_false_only_for_a_genuinely_wrong_code(ol_lending_enabled):
    from lenny.core.auth import OTP

    patcher, _ = _post_returns(_response({"error": "otp_mismatch"}))
    with patcher:
        assert OTP.redeem("a@b.org", "1.2.3.4", "wrong") is False


@pytest.mark.parametrize("code", ["unauthorized", "missing_keys", "ratelimit",
                                  "missing_or_invalid_authorization"])
def test_redeem_raises_rather_than_blaming_the_patron(ol_lending_enabled, code):
    """Collapsing these into False showed "Invalid OTP" when the real problem was
    that Lenny's own Open Library credentials had gone stale — a bug report
    nobody can act on."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": code}))
    with patcher, pytest.raises(OTPGenerationError) as exc:
        OTP.redeem("a@b.org", "1.2.3.4", "abc123")

    assert exc.value.code == code


def test_authenticate_propagates_the_real_error(ol_lending_enabled):
    """`authenticate` wraps verify -> redeem; the reason must survive the trip."""
    from lenny.core.auth import OTP
    from lenny.core.exceptions import OTPGenerationError

    patcher, _ = _post_returns(_response({"error": "unauthorized"}))
    with patcher, patch("lenny.core.auth.OTP.is_rate_limited", return_value=False):
        with pytest.raises(OTPGenerationError):
            OTP.authenticate("a@b.org", "abc123", "1.2.3.4")


def test_authenticate_returns_none_for_a_wrong_code(ol_lending_enabled):
    from lenny.core.auth import OTP

    patcher, _ = _post_returns(_response({"error": "otp_mismatch"}))
    with patcher, patch("lenny.core.auth.OTP.is_rate_limited", return_value=False):
        assert OTP.authenticate("a@b.org", "wrong", "1.2.3.4") is None


# ---------------------------------------------------------------------------
# Route behaviour: never promise an email that was not sent
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """A client whose template layer is a deterministic stub.

    `tests/test_direct_auth_mock.py` replaces `app.templates` on the shared app at
    import time and never restores it, so real Jinja output is only available
    when this module runs first. Install our own stub — which surfaces both the
    template name and the error placed in the context — and restore whatever was
    there on the way out, so these assertions hold in any test order.
    """
    from fastapi import Response
    from fastapi.testclient import TestClient

    def render(name, context):
        return Response(content=f"template={name}\nerror={context.get('error', '')}",
                        media_type="text/html")

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app

        original = getattr(app, "templates", None)
        stub = MagicMock()
        stub.TemplateResponse.side_effect = render
        app.templates = stub
        try:
            yield TestClient(app)
        finally:
            app.templates = original


@pytest.fixture
def borrowable_item():
    item = MagicMock()
    item.id = 1
    item.encrypted = True
    item.is_login_required = True
    with patch("lenny.routes.api.Item.exists", return_value=item), \
         patch("lenny.routes.api._require_lending"), \
         patch("lenny.routes.api.is_direct_auth_mode", return_value=True):
        yield item


def test_borrow_page_reports_the_failure_instead_of_promising_an_email(client, borrowable_item):
    """The behaviour this whole PR exists for: Open Library refused, so the patron
    must be told — not shown the "enter the code we just emailed you" screen for a
    code that was never sent."""
    from lenny.core.exceptions import OTPGenerationError

    with patch("lenny.routes.api.auth.OTP.issue",
               side_effect=OTPGenerationError("Credentials rejected; run make ol-login.",
                                              code="unauthorized")):
        resp = client.post("/v1/api/items/1/borrow?beta=true", data={"email": "a@b.org"})

    assert resp.status_code == 200
    assert "Credentials rejected" in resp.text
    # The email-entry screen, not the "enter the code we sent you" screen.
    assert "template=otp_issue.html" in resp.text
    assert "otp_redeem.html" not in resp.text


def test_borrow_page_advances_only_on_a_real_success(client, borrowable_item):
    with patch("lenny.routes.api.auth.OTP.issue", return_value={"success": "issued"}):
        resp = client.post("/v1/api/items/1/borrow?beta=true", data={"email": "a@b.org"})

    assert resp.status_code == 200
    assert "template=otp_redeem.html" in resp.text


def test_redeem_screen_distinguishes_our_fault_from_a_wrong_code(client, borrowable_item):
    from lenny.core.exceptions import OTPGenerationError

    with patch("lenny.routes.api.auth.OTP.authenticate",
               side_effect=OTPGenerationError("Open Library's login service is unavailable.",
                                              code="auth_service_unavailable")):
        resp = client.post("/v1/api/items/1/borrow?beta=true",
                           data={"email": "a@b.org", "otp": "abc123"})

    assert resp.status_code == 200
    assert "login service is unavailable" in resp.text
    assert "Invalid OTP" not in resp.text, "our outage must not be reported as the patron's typo"


def test_redeem_screen_still_says_invalid_for_a_wrong_code(client, borrowable_item):
    with patch("lenny.routes.api.auth.OTP.authenticate", return_value=None):
        resp = client.post("/v1/api/items/1/borrow?beta=true",
                           data={"email": "a@b.org", "otp": "wrong"})

    assert "Invalid OTP" in resp.text


# ---------------------------------------------------------------------------
# Proxy headers: every patron must get their own IP
# ---------------------------------------------------------------------------

def test_uvicorn_options_trust_the_fronting_proxy():
    """Without this, `request.client.host` is the nginx container's address for
    EVERY patron. That address is what Lenny sends Open Library as the OTP's
    `ip`, so all patrons on an instance shared one identity — and one
    `otp-global:ip:<...>` rate-limit bucket, which allows only 3 per 60s."""
    from lenny.configs import OPTIONS

    assert OPTIONS["proxy_headers"] is True
    assert OPTIONS["forwarded_allow_ips"]


def test_container_entrypoint_passes_forwarded_allow_ips():
    """configs.OPTIONS covers `python -m lenny.app`, but production starts uvicorn
    from the Dockerfile CMD — the two must not drift."""
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[1] / "docker" / "api" / "Dockerfile"
    cmd = dockerfile.read_text()
    assert "--proxy-headers" in cmd
    assert "--forwarded-allow-ips" in cmd
    assert "LENNY_FORWARDED_ALLOW_IPS" in cmd, "operators must be able to narrow this"
