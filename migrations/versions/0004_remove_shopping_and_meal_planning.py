"""remove shopping list and meal planning

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("meal_plan_entries")
    op.drop_table("shopping_list_items")


def downgrade() -> None:
    op.create_table(
        "shopping_list_items",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.String(length=100), nullable=True),
        sa.Column("checked", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=80), nullable=True),
        sa.Column("amount_min", sa.Numeric(precision=16, scale=4), nullable=True),
        sa.Column("amount_max", sa.Numeric(precision=16, scale=4), nullable=True),
        sa.Column("normalized_key", sa.String(length=700), nullable=True),
        sa.Column(
            "is_manual",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "source_recipe_ids",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shopping_list_items_user_id",
        "shopping_list_items",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_shopping_list_items_normalized_key",
        "shopping_list_items",
        ["normalized_key"],
        unique=False,
    )

    op.create_table(
        "meal_plan_entries",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planned_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("servings", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meal_plan_entries_planned_for",
        "meal_plan_entries",
        ["planned_for"],
        unique=False,
    )
    op.create_index(
        "ix_meal_plan_entries_recipe_id",
        "meal_plan_entries",
        ["recipe_id"],
        unique=False,
    )
    op.create_index(
        "ix_meal_plan_entries_user_id",
        "meal_plan_entries",
        ["user_id"],
        unique=False,
    )
