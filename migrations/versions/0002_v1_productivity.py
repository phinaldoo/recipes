"""Version-1-Produktoberflächen: Tags, Synonyme, Freigaben und Planung.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name"),
    )
    op.create_index("ix_tags_normalized_name", "tags", ["normalized_name"], unique=True)

    op.create_table(
        "search_synonyms",
        sa.Column("term", sa.String(length=100), nullable=False),
        sa.Column("normalized_term", sa.String(length=100), nullable=False),
        sa.Column("synonym", sa.String(length=100), nullable=False),
        sa.Column("normalized_synonym", sa.String(length=100), nullable=False),
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
        sa.CheckConstraint(
            "normalized_term <> normalized_synonym", name="ck_search_synonyms_distinct"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_term", "normalized_synonym"),
    )
    op.create_index(
        "ix_search_synonyms_normalized_term", "search_synonyms", ["normalized_term"], unique=False
    )
    op.create_index(
        "ix_search_synonyms_normalized_synonym",
        "search_synonyms",
        ["normalized_synonym"],
        unique=False,
    )

    op.create_table(
        "recipe_tags",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recipe_id", "tag_id"),
    )

    op.create_table(
        "recipe_shares",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_recipe_shares_recipe_id", "recipe_shares", ["recipe_id"], unique=False)
    op.create_index(
        "ix_recipe_shares_created_by_user_id", "recipe_shares", ["created_by_user_id"], unique=False
    )
    op.create_index("ix_recipe_shares_token_hash", "recipe_shares", ["token_hash"], unique=True)
    op.create_index("ix_recipe_shares_expires_at", "recipe_shares", ["expires_at"], unique=False)
    op.create_index("ix_recipe_shares_revoked_at", "recipe_shares", ["revoked_at"], unique=False)

    op.add_column("shopping_list_items", sa.Column("unit", sa.String(length=80), nullable=True))
    op.add_column(
        "shopping_list_items",
        sa.Column("amount_min", sa.Numeric(precision=16, scale=4), nullable=True),
    )
    op.add_column(
        "shopping_list_items",
        sa.Column("amount_max", sa.Numeric(precision=16, scale=4), nullable=True),
    )
    op.add_column(
        "shopping_list_items", sa.Column("normalized_key", sa.String(length=700), nullable=True)
    )
    op.add_column(
        "shopping_list_items",
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "shopping_list_items",
        sa.Column(
            "source_recipe_ids", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False
        ),
    )
    op.create_index(
        "ix_shopping_list_items_normalized_key",
        "shopping_list_items",
        ["normalized_key"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_recipe_versions_recipe_version", "recipe_versions", ["recipe_id", "version_number"]
    )

    # Existing rows predate the weighted index. Give titles the highest relevance
    # immediately; subsequent edits rebuild the complete A/B/C/D vector including
    # categories, ingredients, sources, steps, notes, comments and tags.
    op.execute(
        """
        UPDATE recipes
        SET search_vector =
            setweight(to_tsvector('german', unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('german', unaccent(coalesce(search_document, ''))), 'D')
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_recipe_versions_recipe_version", "recipe_versions", type_="unique")
    op.drop_index("ix_shopping_list_items_normalized_key", table_name="shopping_list_items")
    op.drop_column("shopping_list_items", "source_recipe_ids")
    op.drop_column("shopping_list_items", "is_manual")
    op.drop_column("shopping_list_items", "normalized_key")
    op.drop_column("shopping_list_items", "amount_max")
    op.drop_column("shopping_list_items", "amount_min")
    op.drop_column("shopping_list_items", "unit")
    op.drop_table("recipe_shares")
    op.drop_table("recipe_tags")
    op.drop_table("search_synonyms")
    op.drop_table("tags")
