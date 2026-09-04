"""add durable recipe image generation jobs

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_generation_jobs",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_image_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_stage", sa.String(length=200), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_image_generation_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_image_id"],
            ["recipe_images.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_image_id"),
    )
    op.create_index(
        "ix_image_generation_jobs_recipe_id",
        "image_generation_jobs",
        ["recipe_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_generation_jobs_requested_by_user_id",
        "image_generation_jobs",
        ["requested_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_generation_jobs_lease_token",
        "image_generation_jobs",
        ["lease_token"],
        unique=False,
    )
    op.create_index(
        "ix_image_generation_jobs_lease_expires_at",
        "image_generation_jobs",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_image_generation_jobs_active_recipe",
        "image_generation_jobs",
        ["recipe_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_image_generation_jobs_active_recipe",
        table_name="image_generation_jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index(
        "ix_image_generation_jobs_lease_expires_at",
        table_name="image_generation_jobs",
    )
    op.drop_index("ix_image_generation_jobs_lease_token", table_name="image_generation_jobs")
    op.drop_index(
        "ix_image_generation_jobs_requested_by_user_id",
        table_name="image_generation_jobs",
    )
    op.drop_index("ix_image_generation_jobs_recipe_id", table_name="image_generation_jobs")
    op.drop_table("image_generation_jobs")
