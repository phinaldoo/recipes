"""Initial production schema.

Revision ID: 0001
Revises:

This migration is intentionally self-contained. Importing application metadata
here would make a fresh installation change whenever a model changes, defeating
Alembic's guarantee that a revision is immutable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "categories",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=240), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
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
        sa.CheckConstraint("origin IN ('manual', 'ai_import')", name="ck_categories_origin"),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_id", "normalized_name", name="uq_categories_parent_name"),
    )
    op.create_index(
        "ix_categories_name_trgm",
        "categories",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"], unique=False)
    op.execute(
        "CREATE UNIQUE INDEX uq_categories_root_or_parent_name "
        "ON categories "
        "(COALESCE(parent_id, "
        "'00000000-0000-0000-0000-000000000000'::uuid), normalized_name)"
    )

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("role IN ('member', 'admin')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=True),
        sa.Column("target_id", sa.String(length=100), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index(
        "ix_audit_logs_actor_user_id",
        "audit_logs",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)

    op.create_table(
        "backup_restore_jobs",
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("archive_filename", sa.String(length=500), nullable=True),
        sa.Column("archive_sha256", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("current_stage", sa.String(length=200), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("operation IN ('export', 'restore')", name="ck_backup_jobs_operation"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backup_restore_jobs_requested_by_user_id",
        "backup_restore_jobs",
        ["requested_by_user_id"],
        unique=False,
    )

    op.create_table(
        "import_batches",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False),
        sa.Column("completed_jobs", sa.Integer(), nullable=False),
        sa.Column("failed_jobs", sa.Integer(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_import_batches_created_by_user_id",
        "import_batches",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "media_assets",
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('recipe_image', 'original_upload', 'url_snapshot_pdf', "
            "'generated_image', 'image_thumbnail')",
            name="ck_media_assets_kind",
        ),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_media_assets_sha256", "media_assets", ["sha256"], unique=False)
    op.create_index(
        "ix_media_assets_uploaded_by_user_id",
        "media_assets",
        ["uploaded_by_user_id"],
        unique=False,
    )

    op.create_table(
        "recipes",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name_snapshot", sa.String(length=320), nullable=True),
        sa.Column("updated_by_name_snapshot", sa.String(length=320), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("slug", sa.String(length=360), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_servings", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("serving_label", sa.String(length=80), nullable=False),
        sa.Column("prep_time_minutes", sa.Integer(), nullable=True),
        sa.Column("cook_time_minutes", sa.Integer(), nullable=True),
        sa.Column("rest_time_minutes", sa.Integer(), nullable=True),
        sa.Column("total_time_minutes", sa.Integer(), nullable=True),
        sa.Column("total_time_is_manual", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("search_document", sa.Text(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
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
        sa.CheckConstraint("base_servings > 0", name="ck_recipes_base_servings_positive"),
        sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name="ck_recipes_status"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recipes_created_by_user_id",
        "recipes",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index("ix_recipes_deleted_at", "recipes", ["deleted_at"], unique=False)
    op.create_index(
        "ix_recipes_search_vector",
        "recipes",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index("ix_recipes_slug", "recipes", ["slug"], unique=True)
    op.create_index(
        "ix_recipes_status_updated",
        "recipes",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_recipes_title_trgm",
        "recipes",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_recipes_updated_by_user_id",
        "recipes",
        ["updated_by_user_id"],
        unique=False,
    )

    op.create_table(
        "user_sessions",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=96), nullable=False),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_sessions_expires_at",
        "user_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_sessions_token_hash",
        "user_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)

    op.create_table(
        "favorites",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "recipe_id"),
    )

    op.create_table(
        "import_jobs",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("result_recipe_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("current_stage", sa.String(length=200), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("suggestions_json", sa.JSON(), nullable=True),
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
            "input_type IN ('image', 'pdf', 'url', 'recipe_json')",
            name="ck_import_jobs_input_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
            "'generating_image', 'validating', 'review', 'completed', 'failed', "
            "'cancelled')",
            name="ck_import_jobs_status",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["result_recipe_id"], ["recipes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_asset_id"], ["media_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_jobs_batch_id", "import_jobs", ["batch_id"], unique=False)
    op.create_index(
        "ix_import_jobs_lease_expires_at",
        "import_jobs",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index("ix_import_jobs_lease_token", "import_jobs", ["lease_token"], unique=False)

    op.create_table(
        "ingredient_groups",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipe_id", "position"),
    )
    op.create_index(
        "ix_ingredient_groups_recipe_id",
        "ingredient_groups",
        ["recipe_id"],
        unique=False,
    )

    op.create_table(
        "instruction_steps",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipe_id", "position"),
    )
    op.create_index(
        "ix_instruction_steps_recipe_id",
        "instruction_steps",
        ["recipe_id"],
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

    op.create_table(
        "recipe_categories",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recipe_id", "category_id"),
    )

    op.create_table(
        "recipe_comments",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_name_snapshot", sa.String(length=320), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_comments_recipe_created",
        "recipe_comments",
        ["recipe_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_recipe_comments_author_user_id",
        "recipe_comments",
        ["author_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_recipe_comments_recipe_id",
        "recipe_comments",
        ["recipe_id"],
        unique=False,
    )

    op.create_table(
        "recipe_images",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thumbnail_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_cover", sa.Boolean(), nullable=False),
        sa.Column("caption", sa.String(length=1000), nullable=True),
        sa.Column("alt_text", sa.String(length=1000), nullable=True),
        sa.Column("generation_metadata", sa.JSON(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["media_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thumbnail_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_asset_id"),
        sa.UniqueConstraint("recipe_id", "position"),
        sa.UniqueConstraint("thumbnail_asset_id"),
    )
    op.create_index(
        "ix_recipe_images_recipe_id",
        "recipe_images",
        ["recipe_id"],
        unique=False,
    )
    op.create_index(
        "uq_recipe_images_single_cover",
        "recipe_images",
        ["recipe_id"],
        unique=True,
        postgresql_where=sa.text("is_cover"),
    )

    op.create_table(
        "recipe_original_assets",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["media_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recipe_id", "media_asset_id"),
        sa.UniqueConstraint("recipe_id", "position"),
    )

    op.create_table(
        "recipe_sources",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipe_id"),
    )

    op.create_table(
        "recipe_versions",
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("change_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["changed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recipe_versions_recipe_id",
        "recipe_versions",
        ["recipe_id"],
        unique=False,
    )

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

    op.create_table(
        "ingredients",
        sa.Column("ingredient_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_min", sa.Numeric(precision=16, scale=4), nullable=True),
        sa.Column("amount_max", sa.Numeric(precision=16, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=80), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("is_scalable", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "amount_min IS NULL OR amount_min >= 0",
            name="ck_ingredients_amount_min",
        ),
        sa.CheckConstraint(
            "amount_max IS NULL OR amount_min IS NULL OR amount_max >= amount_min",
            name="ck_ingredients_amount_range",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_group_id"], ["ingredient_groups.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingredient_group_id", "position"),
    )
    op.create_index(
        "ix_ingredients_ingredient_group_id",
        "ingredients",
        ["ingredient_group_id"],
        unique=False,
    )

    op.execute(
        "CREATE FUNCTION reject_category_cycles() RETURNS trigger AS $$ "
        "DECLARE current_id uuid; BEGIN "
        "IF NEW.parent_id IS NULL THEN RETURN NEW; END IF; "
        "IF NEW.parent_id = NEW.id THEN RAISE EXCEPTION 'category cycle'; END IF; "
        "current_id := NEW.parent_id; "
        "WHILE current_id IS NOT NULL LOOP "
        "IF current_id = NEW.id THEN RAISE EXCEPTION 'category cycle'; END IF; "
        "SELECT parent_id INTO current_id FROM categories WHERE id = current_id; "
        "END LOOP; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER categories_no_cycles "
        "BEFORE INSERT OR UPDATE OF parent_id ON categories "
        "FOR EACH ROW EXECUTE FUNCTION reject_category_cycles()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS categories_no_cycles ON categories")
    op.execute("DROP FUNCTION IF EXISTS reject_category_cycles()")

    op.drop_table("ingredients")
    op.drop_table("shopping_list_items")
    op.drop_table("recipe_versions")
    op.drop_table("recipe_sources")
    op.drop_table("recipe_original_assets")
    op.drop_table("recipe_images")
    op.drop_table("recipe_comments")
    op.drop_table("recipe_categories")
    op.drop_table("meal_plan_entries")
    op.drop_table("instruction_steps")
    op.drop_table("ingredient_groups")
    op.drop_table("import_jobs")
    op.drop_table("favorites")
    op.drop_table("user_sessions")
    op.drop_table("recipes")
    op.drop_table("media_assets")
    op.drop_table("import_batches")
    op.drop_table("backup_restore_jobs")
    op.drop_table("audit_logs")
    op.drop_table("users")
    op.drop_table("categories")
    op.drop_table("app_settings")
