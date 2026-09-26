"""POST /v1/api/upload is admin-gated, and nothing about the caller's network
position can substitute for that.

The endpoint used to authorize on `request.client.host` alone, which granted
write access to the library three different ways:

  1. any private address — the docker gateway and every LAN peer, so in a
     default compose install "anyone who can reach port 8080" was an uploader,
     with no header needed at all;
  2. any caller willing to send `X-Forwarded-For: 127.0.0.1` — uvicorn runs
     with `--proxy-headers --forwarded-allow-ips=172.16.0.0/12`, and when the
     real peer is itself inside the trusted range the value the caller supplied
     is what `request.client.host` becomes;
  3. any host whose name merely *ends with* "localhost" or "openlibrary.press"
     — `evil-openlibrary.press`, `notopenlibrary.press`, `mylocalhost`. The
     forward-confirmed rDNS in front of that check does not help: an attacker
     owns both the PTR and the A record of a domain they own, so a lookalike
     confirms and then passes the suffix test.

Each test below goes red against the pre-fix code.
"""

import os
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

SECRET = "internal-secret-for-tests"

UPLOAD = "/v1/api/upload"


def _payload():
    return (
        {"openlibrary_edition": "930000001", "encrypted": "false"},
        {"file": ("book.epub", BytesIO(b"PK\x03\x04fake-epub"), "application/epub+zip")},
    )


@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def admin_secrets():
    """Give the process a real internal secret and a real signing salt, so the
    tests exercise the actual verifiers rather than patched stand-ins."""
    from lenny.core import auth

    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), \
         patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        yield auth


@pytest.fixture
def admin_headers(admin_secrets):
    return {
        "X-Admin-Internal-Secret": SECRET,
        "Authorization": f"Bearer {admin_secrets.issue_admin_token()}",
    }


@pytest.fixture
def added():
    """LennyAPI.add stubbed out: these tests are about who may reach it."""
    with patch("lenny.routes.api.LennyAPI.add", return_value=MagicMock()) as add:
        yield add


def _client(app, host="172.17.0.1"):
    """A TestClient whose requests arrive from `host`.

    Starlette's TestClient hardcodes `scope["client"]` to ("testclient", 50000),
    which no IP check would ever have accepted — so testing this endpoint
    through a stock TestClient would have hidden the bug rather than caught it.
    One ASGI wrapper sets the peer address the app actually sees.
    """
    from fastapi.testclient import TestClient

    async def peer(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (host, 40000)}
        await app(scope, receive, send)

    return TestClient(peer)


# ─── 1. no credential, any peer address ──────────────────────────────────────

@pytest.mark.parametrize("peer", [
    "127.0.0.1",    # loopback
    "::1",          # loopback, v6
    "172.17.0.1",   # docker bridge gateway — what a host-machine POST looks like
    "192.168.1.50", # a laptop on the same LAN as the server
    "10.0.0.7",     # a private range that is not docker's
])
def test_upload_rejects_uncredentialed_caller_from_any_address(
    app_module, admin_secrets, added, peer
):
    data, files = _payload()
    r = _client(app_module, peer).post(UPLOAD, data=data, files=files)
    assert r.status_code == 403, f"{peer} was allowed to upload without credentials"
    added.assert_not_called()


def test_upload_rejects_spoofed_forwarded_for(app_module, admin_secrets, added):
    """A client-supplied header must never be the thing that authorizes a write.

    In production uvicorn's ProxyHeadersMiddleware turns this header into
    `request.client.host` before the app sees it; here it stays inert. The
    assertion is the same either way: no header a caller controls can produce a
    successful upload.
    """
    data, files = _payload()
    r = _client(app_module).post(
        UPLOAD, data=data, files=files,
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert r.status_code == 403
    added.assert_not_called()


@pytest.mark.parametrize("hostname", [
    "evil-openlibrary.press",
    "notopenlibrary.press",
    "mylocalhost",
    "openlibrary.press",
    "localhost",
])
def test_upload_rejects_caller_by_reverse_dns(app_module, admin_secrets, added, hostname):
    """rDNS is not a credential — not even for the real allow-listed names.

    socket is patched at its own module (not at `lenny.core.api`) so this test
    is meaningful against the pre-fix code, which resolved the peer, and against
    the fixed code, which never looks the peer up at all.
    """
    with patch("socket.gethostbyaddr", return_value=(hostname, [], ["8.8.8.8"])), \
         patch("socket.gethostbyname", return_value="8.8.8.8"):
        data, files = _payload()
        r = _client(app_module, "8.8.8.8").post(UPLOAD, data=data, files=files)
    assert r.status_code == 403, f"rDNS name {hostname!r} was accepted as authorization"
    added.assert_not_called()


# ─── 2. the credentialed path still works ────────────────────────────────────

def test_upload_succeeds_with_admin_credentials(app_module, admin_headers, added):
    data, files = _payload()
    r = _client(app_module, "8.8.8.8").post(
        UPLOAD, data=data, files=files, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert "uploaded successfully" in r.text
    assert added.call_count == 1
    kwargs = added.call_args.kwargs
    assert kwargs["openlibrary_edition"] == 930000001
    assert kwargs["encrypt"] is False


def test_upload_needs_both_halves_of_the_credential(app_module, admin_secrets, added):
    token = admin_secrets.issue_admin_token()
    data, files = _payload()
    r = _client(app_module).post(
        UPLOAD, data=data, files=files,
        headers={"X-Admin-Internal-Secret": SECRET},  # no bearer token
    )
    assert r.status_code == 401

    data, files = _payload()
    r = _client(app_module).post(
        UPLOAD, data=data, files=files,
        headers={"Authorization": f"Bearer {token}"},  # no internal secret
    )
    assert r.status_code == 403
    added.assert_not_called()


def test_upload_rejects_forged_bearer_token(app_module, admin_headers, added):
    data, files = _payload()
    r = _client(app_module).post(
        UPLOAD, data=data, files=files,
        headers={**admin_headers, "Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401
    added.assert_not_called()


def test_upload_rejects_everything_when_internal_secret_is_unset(app_module, added):
    """An unconfigured deployment must fail closed, including against an empty
    header matching an empty secret."""
    from lenny.core import auth

    with patch.object(auth, "ADMIN_INTERNAL_SECRET", None), \
         patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        token = auth.issue_admin_token()
        for secret in ("", "anything", SECRET):
            data, files = _payload()
            r = _client(app_module).post(
                UPLOAD, data=data, files=files,
                headers={
                    "X-Admin-Internal-Secret": secret,
                    "Authorization": f"Bearer {token}",
                },
            )
            assert r.status_code == 403
    added.assert_not_called()


# ─── 3. the in-container importers are credentialed, not exempted ────────────

def test_lenny_client_presents_real_admin_credentials(admin_secrets):
    """`LennyClient.upload` (Standard Ebooks / BRIET importers) posts to the
    API's own /upload. It must authenticate like anything else — and the
    credentials it sends must be ones the server's own verifiers accept."""
    from lenny.core import auth
    from lenny.core.client import LennyClient

    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs.get("headers") or {})
        return MagicMock(status_code=200, content=b"ok", raise_for_status=MagicMock())

    fake_client = MagicMock()
    fake_client.__enter__.return_value.post.side_effect = fake_post

    with patch("lenny.core.client.httpx.Client", return_value=fake_client):
        assert LennyClient.upload(930000002, BytesIO(b"PK\x03\x04")) is True

    assert auth.verify_admin_internal_secret(
        captured.get("X-Admin-Internal-Secret", "")
    ), "internal secret missing or not one the server accepts"
    bearer = captured.get("Authorization", "").removeprefix("Bearer ").strip()
    assert auth.verify_admin_token(bearer), "admin token missing or not verifiable"
    # The existing User-Agent must survive the addition.
    assert captured.get("User-Agent") == LennyClient.HTTP_HEADERS["User-Agent"]


# ─── 4. the core layer no longer has an IP authorization knob ────────────────

def test_core_add_takes_no_uploader_ip():
    """`LennyAPI.add` must not carry an authorization parameter at all —
    authorization belongs to the route, and a vestigial `uploader_ip` invites
    the IP check back in."""
    import inspect

    from lenny.core.api import LennyAPI

    params = inspect.signature(LennyAPI.add).parameters
    assert "uploader_ip" not in params
    assert not hasattr(LennyAPI, "is_allowed_uploader")
