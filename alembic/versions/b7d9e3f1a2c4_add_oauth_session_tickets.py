"""Add oauth_session_tickets

A single-use, ~60s browser handoff. A consumer (Open Library) exchanges its
bearer token for a ticket on the back channel, then redirects the patron here
with it; Lenny burns the ticket and sets the `session` cookie, so the patron can
read without the access token ever travelling through the browser.

The row stores only the SHA-256 of the ticket and the patron hash the token
already proved — no token, no address — so a dump of this table yields nothing
replayable, exactly like the codes and tokens it sits beside. See
ArchiveLabs/lenny#254.

Revision ID: b7d9e3f1a2c4
Revises: e8c1a4d7b3f6
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7d9e3f1a2c4'
down_revision: Union[str, None] = 'e8c1a4d7b3f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'oauth_session_tickets',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('ticket_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('client_id', sa.String(64), sa.ForeignKey('oauth_clients.client_id'),
                  nullable=False),
        sa.Column('patron_email_hash', sa.String(), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        # NULL until the ticket is redeemed; the atomic claim
        # (UPDATE ... WHERE redeemed_at IS NULL) is what makes redemption
        # single-use under a concurrent double-submit.
        sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Sweeping expired tickets is a range scan over this column. `ticket_hash`
    # needs no explicit index: Postgres already creates one for UNIQUE.
    op.create_index('idx_oauth_session_tickets_expires', 'oauth_session_tickets',
                    ['expires_at'])


def downgrade() -> None:
    op.drop_table('oauth_session_tickets')
