"""Add catalog import_jobs and import_items tables.

Revision ID: 002_catalog
Revises: 001_baseline
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_catalog"
down_revision = "c6b7da6debc2"
branch_labels = None
depends_on = None


def _create_enum(name: str, *values: str) -> None:
    op.execute(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})")


def upgrade() -> None:
    # --- Enums (raw SQL — avoids SQLAlchemy auto-create unreliability) ---
    _create_enum("jobstatus",
                 "pending", "running", "awaiting_review", "paused",
                 "completed", "cancelled", "error")
    _create_enum("jobmode", "metadata_sync", "full_import")
    _create_enum("persona", "publisher", "library", "author")
    _create_enum("resolvertype", "api", "dump")
    _create_enum("inputmethod",
                 "epub_folder", "epub_sidecar", "csv", "marc",
                 "opds", "onix", "vendor_api")
    _create_enum("encryptionpolicy",
                 "all_encrypted", "all_open", "mixed_auto", "mixed_manual")
    _create_enum("pipelinestage",
                 "pending", "extracting", "extracted", "resolving",
                 "resolved", "ol_writing", "ol_done", "uploading",
                 "done", "error", "needs_review", "skipped")
    _create_enum("olstatus",
                 "OL_MATCH_CLEAN", "OL_MATCH_FUZZY", "OL_WORK_ONLY",
                 "OL_NOT_FOUND", "INSUFFICIENT_METADATA")
    _create_enum("actiontaken",
                 "LINK_ONLY", "CREATE_FULL", "SKIPPED_OL", "NEEDS_REVIEW")

    # --- import_jobs ---
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("status",           postgresql.ENUM(name="jobstatus",         create_type=False), nullable=False, server_default="pending"),
        sa.Column("mode",             postgresql.ENUM(name="jobmode",           create_type=False), nullable=False),
        sa.Column("persona",          postgresql.ENUM(name="persona",           create_type=False), nullable=False),
        sa.Column("resolver_type",    postgresql.ENUM(name="resolvertype",      create_type=False), nullable=False, server_default="api"),
        sa.Column("input_method",     postgresql.ENUM(name="inputmethod",       create_type=False), nullable=False),
        sa.Column("encryption_policy",postgresql.ENUM(name="encryptionpolicy",  create_type=False), nullable=False),
        sa.Column("dry_run",          sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("gate_a_enabled",   sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("gate_b_enabled",   sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("skip_ol",          sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("total",            sa.Integer, nullable=False, server_default="0"),
        sa.Column("processed",        sa.Integer, nullable=False, server_default="0"),
        sa.Column("linked",           sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_ol",       sa.Integer, nullable=False, server_default="0"),
        sa.Column("needs_review",     sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors",           sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped",          sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at",       sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("started_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at",     sa.DateTime(timezone=True), nullable=True),
    )

    # --- import_items ---
    op.create_table(
        "import_items",
        sa.Column("id",             sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("job_id",         sa.BigInteger, sa.ForeignKey("import_jobs.id"), nullable=False),
        sa.Column("pipeline_stage", postgresql.ENUM(name="pipelinestage", create_type=False), nullable=False, server_default="pending"),
        sa.Column("stage_updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("retry_count",    sa.Integer, nullable=False, server_default="0"),
        sa.Column("source_path",    sa.String, nullable=True),
        sa.Column("sha256",         sa.String(64), nullable=True),
        # Extracted metadata
        sa.Column("extracted_title",    sa.String, nullable=True),
        sa.Column("extracted_author",   sa.String, nullable=True),
        sa.Column("extracted_isbn",     sa.String, nullable=True),
        sa.Column("extracted_metadata", postgresql.JSONB, nullable=True),
        # OL resolution
        sa.Column("ol_status",     postgresql.ENUM(name="olstatus",    create_type=False), nullable=True),
        sa.Column("confidence",    sa.Float, nullable=True),
        sa.Column("olid",          sa.BigInteger, nullable=True),
        sa.Column("action_taken",  postgresql.ENUM(name="actiontaken", create_type=False), nullable=True),
        # Config
        sa.Column("encrypted",     sa.Boolean, nullable=True),
        sa.Column("skip_ol",       sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("review_candidates", postgresql.JSONB, nullable=True),
        # Results
        sa.Column("minio_key",     sa.String, nullable=True),
        sa.Column("item_id",       sa.BigInteger, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("action_log",    postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # Indexes — critical for worker performance
    op.create_index("idx_import_items_job_stage",     "import_items", ["job_id", "pipeline_stage"])
    op.create_index("idx_import_items_sha256",         "import_items", ["sha256"])
    op.create_index("idx_import_items_stage_updated",  "import_items", ["pipeline_stage", "stage_updated_at"])
    op.create_index("idx_import_items_olid",           "import_items", ["olid"])


def downgrade() -> None:
    op.drop_index("idx_import_items_olid",          table_name="import_items")
    op.drop_index("idx_import_items_stage_updated", table_name="import_items")
    op.drop_index("idx_import_items_sha256",        table_name="import_items")
    op.drop_index("idx_import_items_job_stage",     table_name="import_items")
    op.drop_table("import_items")
    op.drop_table("import_jobs")
    for name in ("actiontaken", "olstatus", "pipelinestage", "encryptionpolicy",
                 "inputmethod", "resolvertype", "persona", "jobmode", "jobstatus"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
