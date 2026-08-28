"""index items.updated_at for OPDS modified_since harvesting

The OPDS catalogue now filters on `updated_at >= :modified_since` and orders by
`(updated_at, id)` so incremental harvesters (Open Library's BookWorm cron, see
internetarchive/openlibrary#13241) can page deterministically. A composite index
on the same tuple serves both the range scan and the sort, so a polling harvester
does not force a full-table sort of `items` on every tick.

Revision ID: f3a91c47b208
Revises: d4e1f2a3b5c6
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f3a91c47b208'
down_revision: Union[str, None] = 'd4e1f2a3b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('idx_items_updated_at', 'items', ['updated_at', 'id'])


def downgrade() -> None:
    op.drop_index('idx_items_updated_at', table_name='items')
