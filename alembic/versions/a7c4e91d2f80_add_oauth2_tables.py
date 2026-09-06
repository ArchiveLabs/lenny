"""OAuth 2.0 authorization server tables

Lenny becomes an authorization server so a consumer (Open Library, another
catalogue, a reading app) can act on a patron's behalf without either side
holding credentials it should not. See ArchiveLabs/lenny#209.

Three tables:

  oauth_clients               registered consumers, secrets stored hashed
  oauth_authorization_codes   single-use, ~60s, bound to client + redirect + PKCE
  oauth_access_tokens         issued access/refresh pairs, stored hashed

Every secret in these tables is a SHA-256 digest of a high-entropy random
string, so a database dump yields nothing replayable.

Revision ID: a7c4e91d2f80
Revises: f3a91c47b208
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c4e91d2f80'
down_revision: Union[str, None] = 'f3a91c47b208'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'oauth_clients',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('client_id', sa.String(64), nullable=False, unique=True),
        # NULL for a public (PKCE-only) client.
        sa.Column('client_secret_hash', sa.String(64), nullable=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('redirect_uris', sa.Text(), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False, server_default=''),
        sa.Column('is_confidential', sa.Boolean(), nullable=False, server_default=sa.true()),
        # Disable a client without deleting it — deleting would take the audit
        # trail with it, and is blocked by the codes referencing the row.
        sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'oauth_authorization_codes',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('code_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('client_id', sa.String(64), sa.ForeignKey('oauth_clients.client_id'),
                  nullable=False),
        sa.Column('patron_email_hash', sa.String(), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False, server_default=''),
        sa.Column('code_challenge', sa.String(128), nullable=False),
        sa.Column('code_challenge_method', sa.String(10), nullable=False,
                  server_default='S256'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
        # Set when the grant is found to have been replayed, so a token issued
        # after that discovery is born revoked regardless of interleaving.
        sa.Column('grant_revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Sweeping expired codes is a range scan over this column. `code_hash` needs
    # no explicit index: Postgres already creates one for the UNIQUE constraint.
    op.create_index('idx_oauth_codes_expires', 'oauth_authorization_codes', ['expires_at'])

    op.create_table(
        'oauth_access_tokens',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('refresh_token_hash', sa.String(64), nullable=True, unique=True),
        sa.Column('client_id', sa.String(64), sa.ForeignKey('oauth_clients.client_id'),
                  nullable=False),
        sa.Column('patron_email_hash', sa.String(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False, server_default=''),
        # Which code produced this, so reuse of a leaked code can revoke its issue.
        sa.Column('authorization_code_id', sa.BigInteger(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # token_hash and refresh_token_hash are UNIQUE, so their indexes already
    # exist. These two serve queries that would otherwise be sequential scans:
    op.create_index('idx_oauth_tokens_patron', 'oauth_access_tokens', ['patron_email_hash'])
    op.create_index('idx_oauth_tokens_expires', 'oauth_access_tokens', ['expires_at'])
    # revoke_for_code() runs on the reuse-detection path — the security-critical
    # one — and without this it scans every token ever issued.
    op.create_index('idx_oauth_tokens_code', 'oauth_access_tokens',
                    ['authorization_code_id'])


def downgrade() -> None:
    op.drop_table('oauth_access_tokens')
    op.drop_table('oauth_authorization_codes')
    op.drop_table('oauth_clients')
