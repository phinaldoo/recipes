from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Load, Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    Category,
    IngredientGroup,
    MediaAsset,
    Recipe,
    RecipeCategory,
    RecipeImage,
    RecipeShare,
    RecipeTag,
    User,
)


def share_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_token_shape(token: str) -> None:
    if not 32 <= len(token) <= 128:
        raise HTTPException(status_code=404, detail="Dieser Freigabelink ist ungültig.")


def _active_share_conditions(token: str, now: datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        RecipeShare.token_hash == share_token_hash(token),
        RecipeShare.revoked_at.is_(None),
        or_(RecipeShare.expires_at.is_(None), RecipeShare.expires_at > now),
    )


def _get_recipe_for_share_creation(db: Session, recipe_id: uuid.UUID) -> Recipe:
    recipe = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.deleted_at.is_(None)).with_for_update()
    )
    if recipe is None:
        raise HTTPException(status_code=404, detail="Das Rezept wurde nicht gefunden.")
    return recipe


def create_share(
    db: Session, user: User, recipe_id: uuid.UUID, expires_in_days: int | None
) -> tuple[RecipeShare, str]:
    recipe = _get_recipe_for_share_creation(db, recipe_id)
    if recipe.status != "active":
        raise HTTPException(status_code=409, detail="Nur aktive Rezepte können geteilt werden.")

    token = secrets.token_urlsafe(32)
    share = RecipeShare(
        recipe_id=recipe.id,
        created_by_user_id=user.id,
        token_hash=share_token_hash(token),
        token_prefix=token[:8],
        expires_at=(datetime.now(UTC) + timedelta(days=expires_in_days))
        if expires_in_days is not None
        else None,
    )
    db.add(share)
    db.flush()
    return share, token


def list_shares(db: Session, recipe_id: uuid.UUID) -> list[RecipeShare]:
    recipe_exists = db.scalar(
        select(Recipe.id).where(Recipe.id == recipe_id, Recipe.deleted_at.is_(None))
    )
    if recipe_exists is None:
        raise HTTPException(status_code=404, detail="Das Rezept wurde nicht gefunden.")
    return list(
        db.scalars(
            select(RecipeShare)
            .where(RecipeShare.recipe_id == recipe_id)
            .order_by(RecipeShare.created_at.desc())
        )
    )


def revoke_share(db: Session, recipe_id: uuid.UUID, share_id: uuid.UUID) -> RecipeShare:
    share = db.get(RecipeShare, share_id)
    if share is None or share.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail="Die Freigabe wurde nicht gefunden.")
    if share.revoked_at is None:
        share.revoked_at = datetime.now(UTC)
    db.flush()
    return share


PUBLIC_RECIPE_LOAD_OPTIONS = (
    # Deny accidental access to any other Recipe relationship (especially users,
    # comments and originals), while allowing Category.path to continue lazily
    # beyond the eagerly loaded common depth for an unusually deep taxonomy.
    Load(Recipe).raiseload("*"),
    selectinload(Recipe.source),
    selectinload(Recipe.nutrition),
    selectinload(Recipe.ingredient_groups).selectinload(IngredientGroup.ingredients),
    selectinload(Recipe.instruction_steps),
    selectinload(Recipe.category_links)
    .selectinload(RecipeCategory.category)
    .selectinload(Category.parent, recursion_depth=19),
    selectinload(Recipe.images),
    selectinload(Recipe.tag_links).selectinload(RecipeTag.tag),
)


def resolve_share(db: Session, token: str) -> tuple[RecipeShare, Recipe]:
    _validate_token_shape(token)
    now = datetime.now(UTC)
    share = db.scalar(select(RecipeShare).where(*_active_share_conditions(token, now)))
    if share is None:
        raise HTTPException(
            status_code=404, detail="Dieser Freigabelink ist ungültig oder abgelaufen."
        )

    recipe = db.scalar(
        select(Recipe)
        .where(
            Recipe.id == share.recipe_id,
            Recipe.deleted_at.is_(None),
            Recipe.status == "active",
        )
        .options(*PUBLIC_RECIPE_LOAD_OPTIONS)
    )
    if recipe is None:
        raise HTTPException(
            status_code=404, detail="Dieser Freigabelink ist ungültig oder abgelaufen."
        )
    share.last_accessed_at = now
    db.flush()
    return share, recipe


def resolve_share_image(
    db: Session, token: str, image_id: uuid.UUID
) -> tuple[RecipeShare, MediaAsset]:
    """Resolve a public image without loading the recipe's private/content graph."""

    _validate_token_shape(token)
    now = datetime.now(UTC)
    row = db.execute(
        select(RecipeShare, MediaAsset)
        .join(Recipe, Recipe.id == RecipeShare.recipe_id)
        .join(RecipeImage, RecipeImage.recipe_id == Recipe.id)
        .join(MediaAsset, MediaAsset.id == RecipeImage.media_asset_id)
        .where(
            *_active_share_conditions(token, now),
            Recipe.deleted_at.is_(None),
            Recipe.status == "active",
            RecipeImage.id == image_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Das Bild wurde nicht gefunden.")
    share, asset = row
    share.last_accessed_at = now
    db.flush()
    return share, asset
