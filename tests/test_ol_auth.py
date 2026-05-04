"""Tests for Open Library / Internet Archive auth bootstrap.

Covers:
  * `ol_auth_headers()` — presence/absence of LOW header based on env state.
  * `update_env_file()` — atomic rewrite preserves unrelated lines, appends
    missing keys, and leaves 0600 perms on the resulting file.
  * `/admin/ol/status`, `/admin/ol/login`, `/admin/ol/logout` — admin gating,
    rate limiting, error translation, and happy-path persistence.
"""

import os
import stat
from unittest.mock import patch, MagicMock

import pytest

os.environ["TESTING"] = "true"


# ─── ol_auth_headers() ───────────────────────────────────────────────────

def test_ol_auth_headers_no_keys_returns_plain_headers():
    from lenny.core.openlibrary import ol_auth_headers
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", None), \
         patch.object(configs, "OL_S3_SECRET_KEY", None):
        headers = ol_auth_headers()

    assert "Authorization" not in headers
    assert headers.get("User-Agent", "").startswith("LennyImportBot")


def test_ol_auth_headers_with_keys_injects_low_auth():
    from lenny.core.openlibrary import ol_auth_headers
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", "access-xyz"), \
         patch.object(configs, "OL_S3_SECRET_KEY", "secret-abc"):
        headers = ol_auth_headers()

    assert headers["Authorization"] == "LOW access-xyz:secret-abc"


def test_ol_auth_headers_partial_keys_no_auth():
    """If only one half of the key pair is set, we must NOT send a broken LOW header."""
    from lenny.core.openlibrary import ol_auth_headers
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", "access-xyz"), \
         patch.object(configs, "OL_S3_SECRET_KEY", None):
        headers = ol_auth_headers()

    assert "Authorization" not in headers


def test_ol_auth_status_shape():
    from lenny.core.openlibrary import ol_auth_status
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", "a"), \
         patch.object(configs, "OL_S3_SECRET_KEY", "b"), \
         patch.object(configs, "OL_USERNAME", "lib@example.org"), \
         patch.object(configs, "LENDING_ENABLED", True), \
         patch.object(configs, "OL_INDEXED", False):
        status = ol_auth_status()

    assert status == {
        "logged_in": True,
        "username": "lib@example.org",
        "lending_enabled": True,
        "ol_indexed": False,
    }


# ─── update_env_file() ───────────────────────────────────────────────────

def test_update_env_file_replaces_existing_key(tmp_path):
    from lenny.core.ol_bootstrap import update_env_file

    env = tmp_path / ".env"
    env.write_text("FOO=old\nBAR=keep-me\n")

    update_env_file(str(env), {"FOO": "new"})

    body = env.read_text()
    assert "FOO=new\n" in body
    assert "BAR=keep-me\n" in body
    assert "FOO=old" not in body


def test_update_env_file_appends_missing_key(tmp_path):
    from lenny.core.ol_bootstrap import update_env_file

    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")

    update_env_file(str(env), {"NEW_KEY": "value"})

    body = env.read_text()
    assert "EXISTING=1\n" in body
    assert body.rstrip().endswith("NEW_KEY=value")


def test_update_env_file_preserves_unrelated_lines_byte_for_byte(tmp_path):
    from lenny.core.ol_bootstrap import update_env_file

    env = tmp_path / ".env"
    original = (
        "# Comment line with weird chars: $%^&*\n"
        "EMPTY=\n"
        "QUOTED=\"hello world\"\n"
        "TARGET=replace-me\n"
        "\n"
        "TRAILING=ok\n"
    )
    env.write_text(original)

    update_env_file(str(env), {"TARGET": "replaced"})

    body = env.read_text()
    assert "# Comment line with weird chars: $%^&*\n" in body
    assert "EMPTY=\n" in body
    assert 'QUOTED="hello world"\n' in body
    assert "TARGET=replaced\n" in body
    assert "TARGET=replace-me" not in body
    assert "TRAILING=ok\n" in body


def test_update_env_file_sets_0600_perms(tmp_path):
    from lenny.core.ol_bootstrap import update_env_file

    env = tmp_path / ".env"
    env.write_text("X=1\n")
    os.chmod(env, 0o644)

    update_env_file(str(env), {"X": "2"})

    mode = stat.S_IMODE(os.stat(env).st_mode)
    assert mode == 0o600


def test_update_env_file_creates_file_when_missing(tmp_path):
    from lenny.core.ol_bootstrap import update_env_file

    env = tmp_path / ".env"
    assert not env.exists()

    update_env_file(str(env), {"NEW": "v"})

    assert env.read_text() == "NEW=v\n"


# ─── /admin/ol/* routes ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ol_client():
    """TestClient that bypasses DB init — the route internals touch Cache.is_throttled
    which we mock per-test, so we never actually hit PostgreSQL."""
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), \
         patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    """Short-circuit the admin gate on every /admin/ol/* test — we verify
    the gate itself in separate tests below."""
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        yield


@pytest.fixture
def cache_open():
    """Rate limiter always allows the request through."""
    with patch("lenny.routes.api.Cache.is_throttled", return_value=False):
        yield


@pytest.fixture
def reset_ol_env():
    """Snapshot + restore lenny.configs.OL_* attributes around a test.

    Routes mutate these module attributes directly (so OL calls pick up
    new keys without a restart). Tests that exercise that mutation need
    to snapshot/restore explicitly instead of using `patch.object`, which
    would revert the mutation before the test body can observe it.
    """
    from lenny import configs

    keys = ("OL_S3_ACCESS_KEY", "OL_S3_SECRET_KEY", "OL_USERNAME", "LENDING_ENABLED")
    snapshot = {k: getattr(configs, k) for k in keys}
    # Start from a clean, logged-out state.
    configs.OL_S3_ACCESS_KEY = None
    configs.OL_S3_SECRET_KEY = None
    configs.OL_USERNAME = None
    configs.LENDING_ENABLED = False
    try:
        yield
    finally:
        for k, v in snapshot.items():
            setattr(configs, k, v)


HDRS = {"X-Admin-Internal-Secret": "x", "Authorization": "Bearer t"}


def test_ol_status_rejects_missing_internal_secret(ol_client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=False):
        resp = ol_client.get("/v1/api/admin/ol/status", headers=HDRS)
    assert resp.status_code == 403


def test_ol_status_rejects_bad_token(ol_client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=False):
        resp = ol_client.get("/v1/api/admin/ol/status", headers=HDRS)
    assert resp.status_code == 401


def test_ol_status_returns_current_state(ol_client, admin_ok):
    from lenny import configs

    with patch.object(configs, "OL_S3_ACCESS_KEY", "a"), \
         patch.object(configs, "OL_S3_SECRET_KEY", "b"), \
         patch.object(configs, "OL_USERNAME", "lib@example.org"), \
         patch.object(configs, "LENDING_ENABLED", True), \
         patch.object(configs, "OL_INDEXED", False):
        resp = ol_client.get("/v1/api/admin/ol/status", headers=HDRS)

    assert resp.status_code == 200
    assert resp.json() == {
        "logged_in": True,
        "username": "lib@example.org",
        "lending_enabled": True,
        "ol_indexed": False,
    }


def test_ol_login_success_persists_and_updates_process(ol_client, admin_ok, cache_open, reset_ol_env):
    from lenny import configs

    with patch("lenny.routes.api.ol_bootstrap.acquire_keys",
               return_value=("AKEY", "SKEY", "LibScreen")) as mock_acq, \
         patch("lenny.routes.api.ol_bootstrap.update_env_file") as mock_env:
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "lib@example.org", "password": "hunter2"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["logged_in"] is True
        assert body["username"] == "lib@example.org"
        assert body["screenname"] == "LibScreen"
        assert body["lending_enabled"] is True

        mock_acq.assert_called_once_with("lib@example.org", "hunter2")
        # Verify we persisted the expected keys (and only those).
        args, _ = mock_env.call_args
        assert args[1] == {
            "OL_S3_ACCESS_KEY": "AKEY",
            "OL_S3_SECRET_KEY": "SKEY",
            "OL_USERNAME": "lib@example.org",
            "LENNY_LENDING_ENABLED": "true",
        }
        # In-process config was flipped so OL calls inside this worker use new keys
        # without waiting for a container restart.
        assert configs.OL_S3_ACCESS_KEY == "AKEY"
        assert configs.OL_S3_SECRET_KEY == "SKEY"
        assert configs.OL_USERNAME == "lib@example.org"
        assert configs.LENDING_ENABLED is True


def test_ol_login_invalid_credentials_returns_401(ol_client, admin_ok, cache_open, reset_ol_env):
    from lenny.core.ol_bootstrap import OLBootstrapError

    with patch("lenny.routes.api.ol_bootstrap.acquire_keys",
               side_effect=OLBootstrapError("INVALID_CREDENTIALS", "nope")):
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "lib@example.org", "password": "wrong"},
        )

    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_credentials"


def test_ol_login_ia_unreachable_returns_502(ol_client, admin_ok, cache_open, reset_ol_env):
    from lenny.core.ol_bootstrap import OLBootstrapError

    with patch("lenny.routes.api.ol_bootstrap.acquire_keys",
               side_effect=OLBootstrapError("IA_UNREACHABLE", "timeout")):
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "lib@example.org", "password": "hunter2"},
        )

    assert resp.status_code == 502
    assert resp.json()["error"] == "ia_unreachable"


def test_ol_login_already_logged_in_requires_replace(ol_client, admin_ok, cache_open, reset_ol_env):
    from lenny import configs

    configs.OL_S3_ACCESS_KEY = "existing-access"
    configs.OL_USERNAME = "prev@example.org"

    with patch("lenny.routes.api.ol_bootstrap.acquire_keys") as mock_acq:
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "new@example.org", "password": "hunter2"},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "already_logged_in"
    assert body["username"] == "prev@example.org"
    # We must not have even attempted IA auth.
    mock_acq.assert_not_called()


def test_ol_login_replace_true_overwrites(ol_client, admin_ok, cache_open, reset_ol_env):
    from lenny import configs

    configs.OL_S3_ACCESS_KEY = "old"
    configs.OL_S3_SECRET_KEY = "old"
    configs.OL_USERNAME = "prev@example.org"

    with patch("lenny.routes.api.ol_bootstrap.acquire_keys",
               return_value=("NEW_A", "NEW_S", "NewScreen")), \
         patch("lenny.routes.api.ol_bootstrap.update_env_file"):
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "new@example.org", "password": "hunter2", "replace": True},
        )

    assert resp.status_code == 200
    assert resp.json()["username"] == "new@example.org"


def test_ol_login_rate_limited_returns_429(ol_client, admin_ok):
    with patch("lenny.routes.api.Cache.is_throttled", return_value=True), \
         patch("lenny.routes.api.ol_bootstrap.acquire_keys") as mock_acq:
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "lib@example.org", "password": "hunter2"},
        )

    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    mock_acq.assert_not_called()


def test_ol_login_requires_admin(ol_client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=False):
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "lib@example.org", "password": "hunter2"},
        )
    assert resp.status_code == 403


def test_ol_login_rejects_bad_email_payload(ol_client, admin_ok, cache_open):
    with patch("lenny.routes.api.ol_bootstrap.acquire_keys") as mock_acq:
        resp = ol_client.post(
            "/v1/api/admin/ol/login",
            headers=HDRS,
            json={"email": "not-an-email", "password": "hunter2"},
        )
    # Pydantic validation blocks the request before we try IA.
    assert resp.status_code == 422
    mock_acq.assert_not_called()


def test_ol_logout_clears_credentials(ol_client, admin_ok, reset_ol_env):
    from lenny import configs

    configs.OL_S3_ACCESS_KEY = "a"
    configs.OL_S3_SECRET_KEY = "b"
    configs.OL_USERNAME = "lib@example.org"

    with patch("lenny.routes.api.ol_bootstrap.update_env_file") as mock_env:
        resp = ol_client.post("/v1/api/admin/ol/logout", headers=HDRS)

        assert resp.status_code == 200
        body = resp.json()
        assert body["logged_in"] is False
        assert body["previous_username"] == "lib@example.org"

        args, _ = mock_env.call_args
        assert args[1] == {
            "OL_S3_ACCESS_KEY": "",
            "OL_S3_SECRET_KEY": "",
            "OL_USERNAME": "",
            "LENNY_LENDING_ENABLED": "false",
        }
        assert configs.OL_S3_ACCESS_KEY is None
        assert configs.OL_USERNAME is None
        assert configs.LENDING_ENABLED is False


def test_ol_logout_requires_admin(ol_client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=False):
        resp = ol_client.post("/v1/api/admin/ol/logout", headers=HDRS)
    assert resp.status_code == 403
