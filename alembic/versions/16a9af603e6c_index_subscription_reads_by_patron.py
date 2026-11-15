"""index subscription_reads by patron and day, for new vs returning readers

Revision ID: 16a9af603e6c
Revises: b93783fb98de
Create Date: 2026-11-15
"""
from typing import Sequence, Union

from alembic import op

revision: str = '16a9af603e6c'
down_revision: Union[str, None] = 'b93783fb98de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('idx_subscription_reads_patron_day', 'subscription_reads', ['patron_email_hash', 'day'])


def downgrade() -> None:
    op.drop_index('idx_subscription_reads_patron_day', table_name='subscription_reads')
