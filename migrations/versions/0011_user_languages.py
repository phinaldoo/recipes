"""add per-user and import target languages

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("language", sa.String(length=10), nullable=True))
    op.create_check_constraint(
        "ck_users_language",
        "users",
        "language IS NULL OR language IN ('de', 'en', 'zh-CN', 'hi', 'es')",
    )
    op.add_column(
        "import_batches",
        sa.Column(
            "target_language",
            sa.String(length=10),
            nullable=False,
            server_default="de",
        ),
    )
    op.create_check_constraint(
        "ck_import_batches_target_language",
        "import_batches",
        "target_language IN ('de', 'en', 'zh-CN', 'hi', 'es')",
    )
    op.alter_column("import_batches", "target_language", server_default=None)
    op.create_index(
        "ix_recipes_search_document_trgm",
        "recipes",
        ["search_document"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"search_document": "gin_trgm_ops"},
    )
    op.execute(
        "UPDATE recipes SET search_vector = "
        "to_tsvector('simple', unaccent(coalesce(search_document, '')))"
    )


def downgrade() -> None:
    op.drop_index("ix_recipes_search_document_trgm", table_name="recipes")
    op.execute(
        "UPDATE recipes SET search_vector = "
        "to_tsvector('german', unaccent(coalesce(search_document, '')))"
    )
    op.drop_constraint(
        "ck_import_batches_target_language",
        "import_batches",
        type_="check",
    )
    op.drop_column("import_batches", "target_language")
    op.drop_constraint("ck_users_language", "users", type_="check")
    op.drop_column("users", "language")
