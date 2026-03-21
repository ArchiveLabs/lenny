"""Baseline schema: items and loans tables.

Revision ID: 001_baseline
Revises: None
Create Date: 2026-03-08

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

# revision identifiers, used by Alembic.
revision = "001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # If 'items' table already exists, this is a pre-Alembic database.
    # Schema is already correct — skip creation, Alembic stamps the revision.
    if bind.dialect.has_table(bind, "items"):
        return

    # Create enum via raw SQL — avoids SQLAlchemy's unreliable auto-creation
    op.execute("CREATE TYPE formatenum AS ENUM ('EPUB', 'PDF', 'EPUB_PDF')")

    # PgEnum with create_type=False references the existing type without recreating it
    formatenum = PgEnum("EPUB", "PDF", "EPUB_PDF", name="formatenum", create_type=False)

    # -- items table --
    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("openlibrary_edition", sa.BigInteger, nullable=False),
        sa.Column(
            "encrypted",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("formats", formatenum, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )

    op.create_index("idx_items_openlibrary_edition", "items", ["openlibrary_edition"])

    # -- loans table --
    op.create_table(
        "loans",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "item_id",
            sa.BigInteger,
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column("patron_email_hash", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "idx_loans_item_patron_returned",
        "loans",
        ["item_id", "patron_email_hash", "returned_at"],
    )
    op.create_index("idx_loans_item_returned", "loans", ["item_id", "returned_at"])


def downgrade() -> None:
    op.drop_index("idx_loans_item_returned", table_name="loans")
    op.drop_index("idx_loans_item_patron_returned", table_name="loans")
    op.drop_table("loans")
    op.drop_index("idx_items_openlibrary_edition", table_name="items")
    op.drop_table("items")
    op.execute("DROP TYPE IF EXISTS formatenum")
