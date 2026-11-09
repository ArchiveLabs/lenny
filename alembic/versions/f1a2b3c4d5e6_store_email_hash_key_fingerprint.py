"""store a non-secret fingerprint of EMAIL_HASH_KEY beside the data

Since #268 the patron email hash is HMAC-keyed from LENNY_SEED, so a seed that
differs from the one the data was written under silently orphans every patron
(ArchiveLabs/lenny#269). This records a NON-SECRET fingerprint of the current
EMAIL_HASH_KEY so the startup guard (lenny.core.integrity.verify_email_hash_key)
can refuse to run against data written under a different seed.

The fingerprint is an HMAC of a fixed label under the key, truncated: it is not
the key and reveals nothing usable about the seed. The label and construction
are frozen here and MUST stay identical to lenny.core.integrity (kept in sync by
hand, the same way e2b4c6d8f0a1 freezes its hash rather than importing it).

A fresh install reaches this migration under its own seed and records that
seed's fingerprint; an existing install records the seed its data already
matches. An install whose seed was already lost before this migration ran will
record the wrong (current) seed — this guard prevents FUTURE silent orphaning,
it cannot recover a seed already gone.

Revision ID: f1a2b3c4d5e6
Revises: e2b4c6d8f0a1
Create Date: 2026-11-08
"""
import hashlib
import hmac
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e2b4c6d8f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of lenny.core.integrity._FINGERPRINT_LABEL / _ROW_NAME. Must match.
_FINGERPRINT_LABEL = b"lenny:email-hash-key-fingerprint:v1"
_ROW_NAME = "email_hash_key"


def upgrade() -> None:
    from lenny import configs  # raises at import if LENNY_SEED is missing

    table = op.create_table(
        'key_fingerprints',
        sa.Column('name', sa.String(64), primary_key=True),
        sa.Column('fingerprint', sa.String(64), nullable=False),
    )
    fingerprint = hmac.new(
        configs.EMAIL_HASH_KEY, _FINGERPRINT_LABEL, hashlib.sha256
    ).hexdigest()[:16]
    op.bulk_insert(table, [{"name": _ROW_NAME, "fingerprint": fingerprint}])


def downgrade() -> None:
    op.drop_table('key_fingerprints')
