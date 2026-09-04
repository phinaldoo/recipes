"""add structured recipe nutrition values

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recipe_nutrition",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("basis", sa.String(length=30), nullable=False),
        sa.Column("energy_kj", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("energy_kcal", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("fat_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("saturated_fat_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("carbohydrates_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("sugars_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("fiber_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("protein_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("salt_g", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "basis IN ('per_serving', 'per_100g_ml')",
            name="ck_recipe_nutrition_basis",
        ),
        sa.CheckConstraint(
            "energy_kj IS NOT NULL OR energy_kcal IS NOT NULL OR fat_g IS NOT NULL OR "
            "saturated_fat_g IS NOT NULL OR carbohydrates_g IS NOT NULL OR sugars_g IS NOT NULL "
            "OR fiber_g IS NOT NULL OR protein_g IS NOT NULL OR salt_g IS NOT NULL",
            name="ck_recipe_nutrition_has_value",
        ),
        sa.CheckConstraint(
            "(energy_kj IS NULL OR energy_kj >= 0) AND "
            "(energy_kcal IS NULL OR energy_kcal >= 0) AND "
            "(fat_g IS NULL OR fat_g >= 0) AND "
            "(saturated_fat_g IS NULL OR saturated_fat_g >= 0) AND "
            "(carbohydrates_g IS NULL OR carbohydrates_g >= 0) AND "
            "(sugars_g IS NULL OR sugars_g >= 0) AND "
            "(fiber_g IS NULL OR fiber_g >= 0) AND "
            "(protein_g IS NULL OR protein_g >= 0) AND "
            "(salt_g IS NULL OR salt_g >= 0)",
            name="ck_recipe_nutrition_nonnegative",
        ),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipe_id", "basis"),
    )
    op.create_index(
        "ix_recipe_nutrition_recipe_id",
        "recipe_nutrition",
        ["recipe_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_recipe_nutrition_recipe_id", table_name="recipe_nutrition")
    op.drop_table("recipe_nutrition")
