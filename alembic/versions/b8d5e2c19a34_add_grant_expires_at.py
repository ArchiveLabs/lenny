"""Cap how long one consent can last

`AccessToken.issue` sets `refresh_expires_at = now() + 90d` on every rotation,
so a consumer that keeps refreshing holds a grant forever from a single "Allow"
click — while the consent screen tells the patron their access expires.

`grant_expires_at` is the absolute ceiling, anchored to the authorization code's
`created_at` and carried forward unchanged across every rotation. It lives on
the token rather than being re-read from the code because `sweep_expired`
eventually deletes the code, and a grant must not become immortal again when it
does.

NULL on existing rows, which means "no ceiling recorded". Those are the tokens
issued before this migration; they keep their current behaviour until the patron
re-authorizes, rather than being retroactively cut off mid-loan.

Revision ID: b8d5e2c19a34
Revises: a7c4e91d2f80
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8d5e2c19a34'
down_revision: Union[str, None] = 'a7c4e91d2f80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'oauth_access_tokens',
        sa.Column('grant_expires_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('oauth_access_tokens', 'grant_expires_at')
