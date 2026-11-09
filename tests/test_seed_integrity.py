"""ArchiveLabs/lenny#269 — the startup guard must refuse to run against data
written under a different LENNY_SEED.

Since #268 the patron email hash is HMAC-keyed from the seed, so a seed that
differs from the one the data was written under matches no patron. An absent
seed already fails loudly at import; a *changed* seed was silent. These tests
pin the four behaviours of the guard: table absent -> skip, row absent -> write,
match -> proceed, mismatch -> refuse.

`test_changed_seed_refuses_to_start` is the red-first test: against the
silent-mismatch state it fails (no SeedMismatchError), and the fix makes it
pass. See the PR for the recorded red run.
"""

import os

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "seed-integrity-test-seed-32-chars")

from lenny import configs  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.integrity import (  # noqa: E402
    KeyFingerprint,
    SeedMismatchError,
    email_hash_key_fingerprint,
    verify_email_hash_key,
)

KEY_A = b"A" * 32
KEY_B = b"B" * 32


@pytest.fixture
def fingerprints_table():
    """A clean key_fingerprints table bound to the in-memory test DB."""
    KeyFingerprint.__table__.create(engine, checkfirst=True)
    db.query(KeyFingerprint).delete()
    db.commit()
    yield
    db.query(KeyFingerprint).delete()
    db.commit()
    db.remove()


@pytest.fixture
def live_key(monkeypatch):
    """Set the live EMAIL_HASH_KEY and return a setter to change it mid-test."""
    def _set(key: bytes):
        monkeypatch.setattr(configs, "EMAIL_HASH_KEY", key)
    _set(KEY_A)
    return _set


class TestFingerprintIsNotTheKey:

    def test_fingerprint_does_not_reveal_the_key(self):
        fp = email_hash_key_fingerprint(KEY_A)
        assert KEY_A.hex() not in fp
        assert KEY_A.decode() not in fp
        # One-way and short: 16 hex chars, not the 64-char full key hex.
        assert len(fp) == 16

    def test_fingerprint_is_deterministic_and_key_specific(self):
        assert email_hash_key_fingerprint(KEY_A) == email_hash_key_fingerprint(KEY_A)
        assert email_hash_key_fingerprint(KEY_A) != email_hash_key_fingerprint(KEY_B)


class TestGuardBehaviour:

    def test_absent_row_is_written_then_matches(self, fingerprints_table, live_key):
        """Fresh install: no stored fingerprint -> write it, proceed. A second
        call under the same key then matches."""
        assert db.get(KeyFingerprint, "email_hash_key") is None
        verify_email_hash_key()  # writes
        row = db.get(KeyFingerprint, "email_hash_key")
        assert row is not None
        assert row.fingerprint == email_hash_key_fingerprint(KEY_A)
        verify_email_hash_key()  # matches, no raise

    def test_matching_seed_proceeds(self, fingerprints_table, live_key):
        verify_email_hash_key()
        verify_email_hash_key()  # must not raise

    def test_changed_seed_refuses_to_start(self, fingerprints_table, live_key):
        """RED-FIRST: write a fingerprint under seed A, switch the live key to
        seed B, and require a refusal. Against the silent-mismatch state this
        does not raise and the test fails; the fix makes it raise."""
        verify_email_hash_key()  # stores fingerprint for KEY_A
        live_key(KEY_B)          # operator now runs with a different seed
        with pytest.raises(SeedMismatchError) as excinfo:
            verify_email_hash_key()
        message = str(excinfo.value)
        # The message must tell the operator what to do, and must NOT leak the
        # seed or the key.
        assert "backup" in message.lower()
        assert "LENNY_SEED" in message
        assert KEY_A.decode() not in message
        assert KEY_B.decode() not in message
        assert email_hash_key_fingerprint(KEY_B) not in message

    def test_missing_table_is_tolerated(self, live_key):
        """A database whose migrations have not created the table yet cannot be
        checked and must not crash."""
        KeyFingerprint.__table__.drop(engine, checkfirst=True)
        verify_email_hash_key()  # must not raise
