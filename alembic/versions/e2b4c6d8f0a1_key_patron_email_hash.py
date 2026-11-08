"""key patron email hash with a key derived from LENNY_SEED

Before this revision a patron was identified by sha256(normalized email), an
unkeyed hash anyone holding a DB copy and a list of addresses could reverse.
`hash_email` is now HMAC-SHA256(key, that sha256 hex), so every stored value is
rewritten in place as HMAC(key, old). The algorithm is frozen here rather than
imported, so later changes to `hash_email` can't change what this did.

Runs once: the alembic version row is the guard, and env.py runs the upgrade
in one transaction, so a failure rolls back both the rewrite and the version.

Revision ID: e2b4c6d8f0a1
Revises: b7d9e3f1a2c4
Create Date: 2026-11-08
"""
import hashlib
import hmac
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e2b4c6d8f0a1'
down_revision: Union[str, None] = 'b7d9e3f1a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every column holding a patron identity hash.
TABLES = ("loans", "oauth_authorization_codes", "oauth_access_tokens", "oauth_session_tickets")
BATCH = 1000


def rehash(conn, key: bytes) -> int:
    """Rewrite every stored patron hash as HMAC(key, old). Returns distinct identities rewritten."""
    union = " UNION ".join(f"SELECT patron_email_hash AS h FROM {t}" for t in TABLES)
    olds = [r[0] for r in conn.execute(sa.text(union))]
    pairs = [{"old": o, "new": hmac.new(key, o.encode("ascii"), hashlib.sha256).hexdigest()} for o in olds]
    for t in TABLES:
        stmt = sa.text(f"UPDATE {t} SET patron_email_hash = :new WHERE patron_email_hash = :old")
        for i in range(0, len(pairs), BATCH):
            conn.execute(stmt, pairs[i:i + BATCH])
    return len(pairs)


def upgrade() -> None:
    from lenny import configs  # raises at import if LENNY_SEED is missing
    rehash(op.get_bind(), configs.EMAIL_HASH_KEY)


def downgrade() -> None:
    raise NotImplementedError(
        "e2b4c6d8f0a1 is irreversible: a keyed hash cannot be turned back into the "
        "unkeyed one. Restore the database backup taken before the upgrade (backups/).")
