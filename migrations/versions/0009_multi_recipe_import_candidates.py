"""stage multiple detected recipes before selective import

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_import_jobs_status", "import_jobs", type_="check")
    op.create_check_constraint(
        "ck_import_jobs_status",
        "import_jobs",
        "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
        "'generating_image', 'validating', 'review', 'completed', 'failed', 'cancelled')",
    )
    op.create_table(
        "import_candidates",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("recipe_payload", sa.JSON(), nullable=True),
        sa.Column(
            "source_regions_json",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("image_region_json", sa.JSON(), nullable=True),
        sa.Column(
            "warnings_json",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.String(length=20),
            server_default="medium",
            nullable=False,
        ),
        sa.Column("image_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thumbnail_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("image_metadata_json", sa.JSON(), nullable=True),
        sa.Column("result_recipe_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'imported', 'discarded')",
            name="ck_import_candidates_status",
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_import_candidates_confidence",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["image_asset_id"], ["media_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["thumbnail_asset_id"], ["media_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_recipe_id"], ["recipes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "position"),
        sa.UniqueConstraint("image_asset_id"),
        sa.UniqueConstraint("thumbnail_asset_id"),
        sa.UniqueConstraint("result_recipe_id"),
    )
    op.create_index("ix_import_candidates_job_id", "import_candidates", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_import_candidates_job_id", table_name="import_candidates")
    op.drop_table("import_candidates")
    op.drop_constraint("ck_import_jobs_status", "import_jobs", type_="check")
    op.create_check_constraint(
        "ck_import_jobs_status",
        "import_jobs",
        "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
        "'generating_image', 'validating', 'completed', 'failed', 'cancelled')",
    )
