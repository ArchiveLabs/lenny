"""`make update` empties the old /v1/api default of NEXT_PUBLIC_API_URL.

Installs configured in April 2026 carry NEXT_PUBLIC_API_URL=/v1/api. The admin app
treats it like an empty value, but it is folded into the admin image hash, so those
installs could never pull the image CI publishes. Any other value is left alone.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STEP = REPO / "docker" / "utils" / "update" / "020_env_sync.sh"


def _install(tmp_path, value):
    (tmp_path / "docker").mkdir()
    shutil.copy(REPO / "docker" / "configure.sh", tmp_path / "docker" / "configure.sh")
    for name in ("auth.env", "ol.env", "reader.env", "loan.env"):  # present: no bootstrap
        (tmp_path / name).write_text("")
    env = tmp_path / ".env"
    env.write_text(f"LENNY_PORT=8080\nNEXT_PUBLIC_API_URL={value}\nLENNY_PROXY=https://lenny.example.org\n")
    env.chmod(0o600)
    return env


def _sync(tmp_path):
    out = subprocess.run(["bash", str(STEP)], env={**os.environ, "LENNY_ROOT": str(tmp_path)},
                         check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return out.stdout


def _value(env):
    return [line for line in env.read_text().splitlines() if line.startswith("NEXT_PUBLIC_API_URL=")]


@pytest.mark.parametrize("old", ["/v1/api", "/v1/api/", '"/v1/api"', "'/v1/api'"])
def test_the_old_default_is_emptied_with_a_backup(tmp_path, old):
    env = _install(tmp_path, old)
    log = _sync(tmp_path)
    assert _value(env) == ["NEXT_PUBLIC_API_URL="]
    assert "LENNY_PROXY=https://lenny.example.org" in env.read_text()         # nothing else touched
    assert stat.S_IMODE(env.stat().st_mode) == 0o600                          # permissions kept
    (backup,) = (tmp_path / "backups").glob(".env.*.admin-api-url.bak")
    assert f"NEXT_PUBLIC_API_URL={old}" in backup.read_text()                   # the original value is kept
    assert "old default /v1/api" in log
    assert "old default" not in _sync(tmp_path)                                # idempotent


@pytest.mark.parametrize("kept", ["", "https://lenny.example.org", "https://lenny.example.org/v1/api",
                                  "/v1/api/other", "http://localhost:8080"])
def test_any_other_value_is_left_alone(tmp_path, kept):
    env = _install(tmp_path, kept)
    log = _sync(tmp_path)
    assert _value(env) == [f"NEXT_PUBLIC_API_URL={kept}"]
    assert "old default" not in log
