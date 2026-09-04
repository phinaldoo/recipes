"""support regenerating an existing recipe cover

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_generation_jobs",
        sa.Column(
            "generation_mode",
            sa.String(length=30),
            server_default="create",
            nullable=False,
        ),
    )
    op.add_column(
        "image_generation_jobs",
        sa.Column(
            "previous_cover_image_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_image_generation_jobs_previous_cover_image_id",
        "image_generation_jobs",
        "recipe_images",
        ["previous_cover_image_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_image_generation_jobs_previous_cover_image_id",
        "image_generation_jobs",
        ["previous_cover_image_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_image_generation_jobs_mode",
        "image_generation_jobs",
        "generation_mode IN ('create', 'regenerate')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_image_generation_jobs_mode",
        "image_generation_jobs",
        type_="check",
    )
    op.drop_index(
        "ix_image_generation_jobs_previous_cover_image_id",
        table_name="image_generation_jobs",
    )
    op.drop_constraint(
        "fk_image_generation_jobs_previous_cover_image_id",
        "image_generation_jobs",
        type_="foreignkey",
    )
    op.drop_column("image_generation_jobs", "previous_cover_image_id")
    op.drop_column("image_generation_jobs", "generation_mode")
