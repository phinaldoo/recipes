from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('member', 'admin')", name="ck_users_role"),
        CheckConstraint(
            "language IS NULL OR language IN ('de', 'en', 'zh-CN', 'hi', 'es')",
            name="ck_users_language",
        ),
        Index("ix_users_email_lower", sql_text("lower(email)"), unique=True),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    language: Mapped[str | None] = mapped_column(String(10))

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    notes: Mapped[list[UserNote]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def visible_name(self) -> str:
        return self.display_name or self.email


class UserSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(96), nullable=False)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class UserNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_notes"
    __table_args__ = (
        CheckConstraint(
            "title IS NOT NULL OR url IS NOT NULL OR content IS NOT NULL",
            name="ck_user_notes_has_content",
        ),
        Index("ix_user_notes_user_updated", "user_id", "updated_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(2048))
    content: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="notes")


class Recipe(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recipes"
    __table_args__ = (
        CheckConstraint("base_servings > 0", name="ck_recipes_base_servings_positive"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_recipes_status"),
        CheckConstraint(
            "recipe_kind IN ('cooking', 'baking')",
            name="ck_recipes_recipe_kind",
        ),
        Index("ix_recipes_status_updated", "status", "updated_at"),
        Index("ix_recipes_kind_status_updated", "recipe_kind", "status", "updated_at"),
        Index(
            "ix_recipes_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index("ix_recipes_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_recipes_search_document_trgm",
            "search_document",
            postgresql_using="gin",
            postgresql_ops={"search_document": "gin_trgm_ops"},
        ),
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by_name_snapshot: Mapped[str | None] = mapped_column(String(320))
    updated_by_name_snapshot: Mapped[str | None] = mapped_column(String(320))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str] = mapped_column(String(360), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    recipe_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="cooking", server_default="cooking"
    )
    base_servings: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=4)
    serving_label: Mapped[str] = mapped_column(String(80), nullable=False, default="Personen")
    prep_time_minutes: Mapped[int | None] = mapped_column(Integer)
    cook_time_minutes: Mapped[int | None] = mapped_column(Integer)
    rest_time_minutes: Mapped[int | None] = mapped_column(Integer)
    total_time_minutes: Mapped[int | None] = mapped_column(Integer)
    total_time_is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    search_document: Mapped[str] = mapped_column(Text, nullable=False, default="")
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_user_id])
    source: Mapped[RecipeSource | None] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", uselist=False
    )
    nutrition: Mapped[list[RecipeNutrition]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeNutrition.basis",
    )
    ingredient_groups: Mapped[list[IngredientGroup]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="IngredientGroup.position",
    )
    instruction_steps: Mapped[list[InstructionStep]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="InstructionStep.position",
    )
    category_links: Mapped[list[RecipeCategory]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    images: Mapped[list[RecipeImage]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", order_by="RecipeImage.position"
    )
    original_assets: Mapped[list[RecipeOriginalAsset]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeOriginalAsset.position",
    )
    comments: Mapped[list[RecipeComment]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeComment.created_at",
    )
    versions: Mapped[list[RecipeVersion]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeVersion.version_number",
    )
    tag_links: Mapped[list[RecipeTag]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    shares: Mapped[list[RecipeShare]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )

    @property
    def categories(self) -> list[Category]:
        return [link.category for link in self.category_links]

    @property
    def expanded_categories(self) -> list[Category]:
        """Return assigned categories and their ancestors once, parent first."""

        result: list[Category] = []
        seen: set[uuid.UUID] = set()
        for category in self.categories:
            lineage: list[Category] = []
            lineage_ids: set[uuid.UUID] = set()
            node: Category | None = category
            while node is not None and node.id not in lineage_ids:
                lineage_ids.add(node.id)
                lineage.append(node)
                node = node.parent
            for item in reversed(lineage):
                if item.id in seen:
                    continue
                seen.add(item.id)
                result.append(item)
        return result

    @property
    def cover_image(self) -> RecipeImage | None:
        return next(
            (image for image in self.images if image.is_cover),
            self.images[0] if self.images else None,
        )

    @property
    def tags(self) -> list[Tag]:
        return [link.tag for link in self.tag_links]

    @property
    def nutrition_per_serving(self) -> RecipeNutrition | None:
        return next((value for value in self.nutrition if value.basis == "per_serving"), None)

    @property
    def nutrition_per_100g_ml(self) -> RecipeNutrition | None:
        return next((value for value in self.nutrition if value.basis == "per_100g_ml"), None)


class RecipeSource(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "recipe_sources"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), unique=True
    )
    title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text)
    recipe: Mapped[Recipe] = relationship(back_populates="source")


class RecipeNutrition(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "recipe_nutrition"
    __table_args__ = (
        CheckConstraint(
            "basis IN ('per_serving', 'per_100g_ml')",
            name="ck_recipe_nutrition_basis",
        ),
        CheckConstraint(
            "energy_kj IS NOT NULL OR energy_kcal IS NOT NULL OR fat_g IS NOT NULL OR "
            "saturated_fat_g IS NOT NULL OR carbohydrates_g IS NOT NULL OR sugars_g IS NOT NULL "
            "OR fiber_g IS NOT NULL OR protein_g IS NOT NULL OR salt_g IS NOT NULL",
            name="ck_recipe_nutrition_has_value",
        ),
        CheckConstraint(
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
        UniqueConstraint("recipe_id", "basis"),
    )

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    basis: Mapped[str] = mapped_column(String(30), nullable=False)
    energy_kj: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    fat_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    saturated_fat_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    carbohydrates_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    sugars_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    fiber_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    protein_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    salt_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    note: Mapped[str | None] = mapped_column(String(1000))
    recipe: Mapped[Recipe] = relationship(back_populates="nutrition")


class IngredientGroup(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ingredient_groups"
    __table_args__ = (UniqueConstraint("recipe_id", "position"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe: Mapped[Recipe] = relationship(back_populates="ingredient_groups")
    ingredients: Mapped[list[Ingredient]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="Ingredient.position"
    )


class Ingredient(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ingredients"
    __table_args__ = (
        CheckConstraint("amount_min IS NULL OR amount_min >= 0", name="ck_ingredients_amount_min"),
        CheckConstraint(
            "amount_max IS NULL OR amount_min IS NULL OR amount_max >= amount_min",
            name="ck_ingredients_amount_range",
        ),
        UniqueConstraint("ingredient_group_id", "position"),
    )

    ingredient_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredient_groups.id", ondelete="CASCADE"), index=True
    )
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    unit: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000))
    is_scalable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    group: Mapped[IngredientGroup] = relationship(back_populates="ingredients")


class InstructionStep(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "instruction_steps"
    __table_args__ = (UniqueConstraint("recipe_id", "position"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    recipe: Mapped[Recipe] = relationship(back_populates="instruction_steps")


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("parent_id", "normalized_name", name="uq_categories_parent_name"),
        CheckConstraint("origin IN ('manual', 'ai_import')", name="ck_categories_origin"),
        Index(
            "ix_categories_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(240), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")

    parent: Mapped[Category | None] = relationship(
        remote_side="Category.id", back_populates="children", foreign_keys=[parent_id]
    )
    children: Mapped[list[Category]] = relationship(
        back_populates="parent", order_by="Category.position", passive_deletes=True
    )
    recipe_links: Mapped[list[RecipeCategory]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )

    @property
    def path(self) -> str:
        parts = [self.name]
        node = self.parent
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return " › ".join(reversed(parts))


class RecipeCategory(Base):
    __tablename__ = "recipe_categories"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    recipe: Mapped[Recipe] = relationship(back_populates="category_links")
    category: Mapped[Category] = relationship(back_populates="recipe_links")


class MediaAsset(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('recipe_image', 'original_upload', 'url_snapshot_pdf', 'generated_image', 'image_thumbnail')",
            name="ck_media_assets_kind",
        ),
    )

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(200), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecipeImage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "recipe_images"
    __table_args__ = (UniqueConstraint("recipe_id", "position"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), unique=True
    )
    thumbnail_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), unique=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_cover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    caption: Mapped[str | None] = mapped_column(String(1000))
    alt_text: Mapped[str | None] = mapped_column(String(1000))
    generation_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    recipe: Mapped[Recipe] = relationship(back_populates="images")
    asset: Mapped[MediaAsset] = relationship(foreign_keys=[media_asset_id])
    thumbnail_asset: Mapped[MediaAsset | None] = relationship(foreign_keys=[thumbnail_asset_id])


class ImageGenerationJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "image_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_image_generation_jobs_status",
        ),
        CheckConstraint(
            "generation_mode IN ('create', 'regenerate')",
            name="ck_image_generation_jobs_mode",
        ),
        Index(
            "uq_image_generation_jobs_active_recipe",
            "recipe_id",
            unique=True,
            postgresql_where=sql_text("status IN ('queued', 'running')"),
        ),
    )

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    previous_cover_image_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipe_images.id", ondelete="SET NULL"),
        index=True,
    )
    result_image_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipe_images.id", ondelete="SET NULL"),
        unique=True,
    )
    generation_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="create")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    current_stage: Mapped[str] = mapped_column(
        String(200), nullable=False, default="Wartet auf Bildgenerierung"
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipe: Mapped[Recipe] = relationship()
    requested_by: Mapped[User | None] = relationship(foreign_keys=[requested_by_user_id])
    previous_cover_image: Mapped[RecipeImage | None] = relationship(
        foreign_keys=[previous_cover_image_id]
    )
    result_image: Mapped[RecipeImage | None] = relationship(foreign_keys=[result_image_id])


class RecipeOriginalAsset(Base):
    __tablename__ = "recipe_original_assets"
    __table_args__ = (UniqueConstraint("recipe_id", "position"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe: Mapped[Recipe] = relationship(back_populates="original_assets")
    asset: Mapped[MediaAsset] = relationship()


class RecipeComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recipe_comments"
    __table_args__ = (Index("ix_comments_recipe_created", "recipe_id", "created_at"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    author_name_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipe: Mapped[Recipe] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship()


class RecipeVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "recipe_versions"
    __table_args__ = (UniqueConstraint("recipe_id", "version_number"),)

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    recipe: Mapped[Recipe] = relationship(back_populates="versions")
    changed_by: Mapped[User | None] = relationship(foreign_keys=[changed_by_user_id])


class Tag(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    recipe_links: Mapped[list[RecipeTag]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class RecipeTag(Base):
    __tablename__ = "recipe_tags"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    recipe: Mapped[Recipe] = relationship(back_populates="tag_links")
    tag: Mapped[Tag] = relationship(back_populates="recipe_links")


class SearchSynonym(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "search_synonyms"
    __table_args__ = (
        UniqueConstraint("normalized_term", "normalized_synonym"),
        CheckConstraint(
            "normalized_term <> normalized_synonym", name="ck_search_synonyms_distinct"
        ),
    )

    term: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_term: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    synonym: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_synonym: Mapped[str] = mapped_column(String(100), nullable=False, index=True)


class RecipeShare(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "recipe_shares"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipe: Mapped[Recipe] = relationship(back_populates="shares")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])


class ImportBatch(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "target_language IN ('de', 'en', 'zh-CN', 'hi', 'es')",
            name="ck_import_batches_target_language",
        ),
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    target_language: Mapped[str] = mapped_column(String(10), nullable=False, default="de")
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jobs: Mapped[list[ImportJob]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportJob.created_at"
    )


class ImportJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(
            "input_type IN ('image', 'pdf', 'url', 'recipe_json')", name="ck_import_jobs_input_type"
        ),
        CheckConstraint(
            "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
            "'generating_image', 'validating', 'review', 'completed', 'failed', 'cancelled')",
            name="ck_import_jobs_status",
        ),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    input_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="SET NULL")
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    result_recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="SET NULL")
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_stage: Mapped[str] = mapped_column(String(200), nullable=False, default="Wartet")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    suggestions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    batch: Mapped[ImportBatch] = relationship(back_populates="jobs")
    source_asset: Mapped[MediaAsset | None] = relationship(foreign_keys=[source_asset_id])
    candidates: Mapped[list[ImportCandidate]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ImportCandidate.position",
    )


class ImportCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "import_candidates"
    __table_args__ = (
        UniqueConstraint("job_id", "position"),
        CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'imported', 'discarded')",
            name="ck_import_candidates_status",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_import_candidates_confidence",
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processing")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    recipe_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source_regions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    image_region_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        unique=True,
    )
    thumbnail_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        unique=True,
    )
    image_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="SET NULL"), unique=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[ImportJob] = relationship(back_populates="candidates")
    image_asset: Mapped[MediaAsset | None] = relationship(foreign_keys=[image_asset_id])
    thumbnail_asset: Mapped[MediaAsset | None] = relationship(foreign_keys=[thumbnail_asset_id])
    result_recipe: Mapped[Recipe | None] = relationship(foreign_keys=[result_recipe_id])


class BackupRestoreJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "backup_restore_jobs"
    __table_args__ = (
        CheckConstraint("operation IN ('export', 'restore')", name="ck_backup_jobs_operation"),
    )

    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    archive_filename: Mapped[str | None] = mapped_column(String(500))
    archive_sha256: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_stage: Mapped[str] = mapped_column(String(200), nullable=False, default="Wartet")
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    recipe: Mapped[Recipe] = relationship()
