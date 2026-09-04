"""classify recipes as cooking or baking

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column(
            "recipe_kind",
            sa.String(length=20),
            nullable=False,
            server_default="cooking",
        ),
    )
    op.execute(
        """
        WITH RECURSIVE baking_categories AS (
            SELECT id
            FROM categories
            WHERE parent_id IS NULL AND normalized_name = 'backen'
            UNION
            SELECT child.id
            FROM categories AS child
            JOIN baking_categories AS parent ON child.parent_id = parent.id
        )
        UPDATE recipes AS recipe
        SET recipe_kind = 'baking'
        WHERE EXISTS (
            SELECT 1
            FROM recipe_categories AS link
            JOIN baking_categories AS category ON category.id = link.category_id
            WHERE link.recipe_id = recipe.id
        )
        """
    )
    op.execute(
        """
        UPDATE recipe_versions AS version
        SET snapshot = (
            version.snapshot::jsonb
            || jsonb_build_object('recipe_kind', recipe.recipe_kind)
        )::json
        FROM recipes AS recipe
        WHERE recipe.id = version.recipe_id
          AND NOT (version.snapshot::jsonb ? 'recipe_kind')
        """
    )
    op.create_check_constraint(
        "ck_recipes_recipe_kind",
        "recipes",
        "recipe_kind IN ('cooking', 'baking')",
    )
    op.create_index(
        "ix_recipes_kind_status_updated",
        "recipes",
        ["recipe_kind", "status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_recipes_kind_status_updated", table_name="recipes")
    op.drop_constraint("ck_recipes_recipe_kind", "recipes", type_="check")
    op.drop_column("recipes", "recipe_kind")
