"""add the subscription access mode, subscriptions and subscription_reads

Revision ID: b93783fb98de
Revises: e2b4c6d8f0a1
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b93783fb98de'
down_revision: Union[str, None] = 'e2b4c6d8f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('items', sa.Column('subscription', sa.Boolean(), nullable=False, server_default=sa.text('false')))

    op.create_table(
        'subscriptions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('member_uuid', sa.String(64), nullable=True),
        sa.Column('patron_email_hash', sa.String(), nullable=False),
        sa.Column('tier_ids', sa.String(), nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('provider', 'patron_email_hash', name='uq_subscriptions_provider_patron'),
    )
    op.create_index('idx_subscriptions_patron_expires', 'subscriptions', ['patron_email_hash', 'expires_at'])
    op.create_index('idx_subscriptions_provider_member', 'subscriptions', ['provider', 'member_uuid'])

    op.create_table(
        'subscription_reads',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('item_id', sa.BigInteger(), sa.ForeignKey('items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('patron_email_hash', sa.String(), nullable=False),
        sa.Column('device_class', sa.String(16), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('opens', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('item_id', 'patron_email_hash', 'device_class', 'day', name='uq_subscription_reads'),
    )
    op.create_index('idx_subscription_reads_day', 'subscription_reads', ['day'])


def downgrade() -> None:
    op.drop_table('subscription_reads')
    op.drop_table('subscriptions')
    op.drop_column('items', 'subscription')
