#!/usr/bin/env python
"""
Internet Archive / Open Library auth bootstrap.

This module is invoked in two ways:

1. As a CLI module inside the `lenny_api` container, by `docker/utils/ol_configure.sh`:

       printf '%s' "$password" | docker exec -i lenny_api \
           python -m lenny.core.ol_bootstrap "$email"

   It reads the password from stdin so it never appears in argv, environment,
   or `docker inspect` output. On success, it writes three newline-separated
   values to stdout (access, secret, screenname). On failure it writes a
   single `ERROR:<CODE>:<msg>` line to stderr and exits non-zero.

2. As a library, by the `/admin/ol/login` route — see `acquire_keys()`.

The module never touches the filesystem: persisting credentials is the caller's
responsibility.
"""

import os
import stat
import sys
import tempfile
from typing import Mapping, Tuple

from lenny.core.exceptions import InvalidOLCredentialsError


class OLBootstrapError(Exception):
    """Raised when IA auth fails. `code` is a stable machine-readable classifier."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def acquire_keys(email: str, password: str) -> Tuple[str, str, str]:
    """Exchange IA email + password for S3 access/secret keys.

    Returns `(access, secret, screenname)`. Raises `OLBootstrapError` with a
    stable `.code` on any failure — callers translate to HTTP status / UI.

    Never logs credentials. Never writes to disk.
    """
    if not email or "@" not in email:
        raise OLBootstrapError("BAD_EMAIL", "Email must be a valid address.")
    if not password:
        raise OLBootstrapError("BAD_PASSWORD", "Password must not be empty.")

    try:
        from internetarchive.config import get_auth_config  # type: ignore
    except ImportError as exc:
        raise OLBootstrapError(
            "MISSING_DEP",
            f"`internetarchive` package not installed in this environment: {exc}",
        ) from None

    try:
        config = get_auth_config(email, password)
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        low = msg.lower()
        if any(s in low for s in ("invalid", "incorrect", "403", "unauthorized", "401")):
            raise OLBootstrapError("INVALID_CREDENTIALS", msg) from None
        if any(s in low for s in ("connection", "timeout", "dns", "resolve", "unreachable")):
            raise OLBootstrapError("IA_UNREACHABLE", msg) from None
        raise OLBootstrapError("UNKNOWN", msg) from None

    s3 = (config or {}).get("s3") or {}
    access = s3.get("access") or ""
    secret = s3.get("secret") or ""
    if not access or not secret:
        raise OLBootstrapError(
            "NO_KEYS",
            "archive.org accepted the credentials but returned no S3 keys.",
        )

    screenname = (config or {}).get("screenname") or email
    return access, secret, screenname


def _as_user_error(err: OLBootstrapError) -> InvalidOLCredentialsError:
    """Translate a bootstrap error into the typed exception the API layer expects."""
    return InvalidOLCredentialsError(f"{err.code}: {err.message}")


def update_env_file(env_path: str, updates: Mapping[str, str]) -> None:
    """Atomically rewrite `env_path`, replacing or appending `updates`.

    Mirrors `docker/utils/ol_configure.sh`'s `env_set`: preserves unrelated
    lines byte-for-byte, writes the new file with 0600 perms before moving it
    into place, and never leaves a half-written file behind.

    Keys missing from the file are appended at the end. Values are written
    raw — callers must strip newlines themselves if needed.
    """
    if not updates:
        return

    remaining = dict(updates)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".env.", dir=os.path.dirname(os.path.abspath(env_path))
    )
    try:
        with os.fdopen(fd, "w") as out:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            try:
                with open(env_path, "r") as src:
                    for line in src:
                        stripped = line.rstrip("\n")
                        key, sep, _ = stripped.partition("=")
                        if sep and key in remaining:
                            out.write(f"{key}={remaining.pop(key)}\n")
                        else:
                            out.write(line if line.endswith("\n") else line + "\n")
            except FileNotFoundError:
                pass
            for key, value in remaining.items():
                out.write(f"{key}={value}\n")
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("ERROR:USAGE:Expected exactly one argument (email)\n")
        sys.exit(64)

    email = sys.argv[1].strip()
    # Read password from stdin — keeps it out of argv and process env.
    # rstrip only trailing CR/LF so that shell `printf '%s'` (no trailing
    # newline) and `echo` (with newline) both produce the same password.
    password = sys.stdin.read().rstrip("\r\n")

    try:
        access, secret, screenname = acquire_keys(email, password)
    except OLBootstrapError as err:
        sys.stderr.write(f"ERROR:{err.code}:{err.message}\n")
        # Distinct exit codes help the shell script branch on failure class.
        codes = {
            "BAD_EMAIL": 2,
            "BAD_PASSWORD": 2,
            "MISSING_DEP": 3,
            "INVALID_CREDENTIALS": 4,
            "IA_UNREACHABLE": 5,
            "NO_KEYS": 6,
            "UNKNOWN": 7,
        }
        sys.exit(codes.get(err.code, 1))

    sys.stdout.write(f"{access}\n{secret}\n{screenname}\n")


if __name__ == "__main__":
    main()
