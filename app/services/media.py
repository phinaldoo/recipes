from __future__ import annotations

import uuid
from io import BytesIO

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MediaAsset, Recipe, RecipeImage, RecipeOriginalAsset, User
from app.schemas.recipe import ImageMetadataInput
from app.services.media_quota import enforce_new_asset_quota, enforce_recipe_quota
from app.services.storage import StoredFile, resolve_storage_key, store_bytes, store_upload


class RecipeImageAlreadyExists(ValueError):
    pass


class RecipeCoverChanged(ValueError):
    pass


def create_asset(db: Session, stored: StoredFile, user: User, kind: str) -> MediaAsset:
    enforce_new_asset_quota(db, user_id=user.id, byte_size=stored.byte_size)
    asset = MediaAsset(
        uploaded_by_user_id=user.id,
        kind=kind,
        storage_key=stored.storage_key,
        original_filename=stored.original_filename,
        mime_type=stored.mime_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        width=stored.width,
        height=stored.height,
        page_count=stored.page_count,
    )
    db.add(asset)
    db.flush()
    return asset


def create_thumbnail_asset(db: Session, asset: MediaAsset, user: User) -> MediaAsset | None:
    if not asset.mime_type.startswith("image/"):
        return None
    with Image.open(resolve_storage_key(asset.storage_key)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((640, 480), Image.Resampling.LANCZOS)
        flattened = Image.new("RGB", image.size, "white")
        flattened.paste(image, mask=image.getchannel("A"))
        buffer = BytesIO()
        flattened.save(buffer, format="JPEG", quality=84, optimize=True, progressive=True)
    stored = store_bytes(
        buffer.getvalue(),
        filename=f"vorschau-{asset.original_filename}.jpg",
        kind="image_thumbnail",
    )
    try:
        return create_asset(db, stored, user, "image_thumbnail")
    except Exception:
        resolve_storage_key(stored.storage_key).unlink(missing_ok=True)
        raise


async def add_recipe_image(
    db: Session,
    recipe: Recipe,
    user: User,
    upload: UploadFile,
    *,
    caption: str | None = None,
    alt_text: str | None = None,
    is_cover: bool = False,
) -> RecipeImage:
    stored = await store_upload(
        upload,
        allowed={"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"},
    )
    written = [resolve_storage_key(stored.storage_key)]
    try:
        asset = create_asset(db, stored, user, "recipe_image")
        thumbnail = create_thumbnail_asset(db, asset, user)
        if thumbnail:
            written.append(resolve_storage_key(thumbnail.storage_key))
        enforce_recipe_quota(db, recipe.id, [asset, thumbnail])
        current_max = db.scalar(
            select(func.max(RecipeImage.position)).where(RecipeImage.recipe_id == recipe.id)
        )
        position = (int(current_max) if current_max is not None else -1) + 1
        if is_cover or not recipe.images:
            for existing in recipe.images:
                existing.is_cover = False
            is_cover = True
        image = RecipeImage(
            recipe_id=recipe.id,
            asset=asset,
            thumbnail_asset=thumbnail,
            position=position,
            is_cover=is_cover,
            caption=(caption or "").strip() or None,
            alt_text=(alt_text or "").strip() or None,
        )
        db.add(image)
        db.flush()
        return image
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def add_generated_recipe_image(
    db: Session,
    recipe: Recipe,
    user: User,
    data: bytes,
    *,
    filename: str,
    alt_text: str,
    generation_metadata: dict[str, object],
    previous_cover_image_id: uuid.UUID | None = None,
) -> RecipeImage:
    current_cover = recipe.cover_image
    if previous_cover_image_id is None and recipe.images:
        raise RecipeImageAlreadyExists("Für dieses Rezept ist bereits ein Bild vorhanden")
    if previous_cover_image_id is not None and (
        current_cover is None or current_cover.id != previous_cover_image_id
    ):
        raise RecipeCoverChanged("Das Titelbild wurde inzwischen geändert")
    stored = store_bytes(data, filename=filename, kind="generated_image")
    written = [resolve_storage_key(stored.storage_key)]
    try:
        asset = create_asset(db, stored, user, "generated_image")
        thumbnail = create_thumbnail_asset(db, asset, user)
        if thumbnail:
            written.append(resolve_storage_key(thumbnail.storage_key))
        enforce_recipe_quota(db, recipe.id, [asset, thumbnail])
        position = 0
        if previous_cover_image_id is not None:
            position = max((existing.position for existing in recipe.images), default=-1) + 1
            for existing in recipe.images:
                existing.is_cover = False
            # Flush the demotion first so PostgreSQL's single-cover index cannot
            # observe both the previous and generated cover in one statement batch.
            db.flush()
        image = RecipeImage(
            recipe_id=recipe.id,
            asset=asset,
            thumbnail_asset=thumbnail,
            position=position,
            is_cover=True,
            alt_text=alt_text.strip()[:1000],
            generation_metadata=generation_metadata,
        )
        db.add(image)
        db.flush()
        return image
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


async def add_original_asset(
    db: Session, recipe: Recipe, user: User, upload: UploadFile
) -> RecipeOriginalAsset:
    stored = await store_upload(upload)
    path = resolve_storage_key(stored.storage_key)
    try:
        asset = create_asset(db, stored, user, "original_upload")
        enforce_recipe_quota(db, recipe.id, [asset])
        current_max = db.scalar(
            select(func.max(RecipeOriginalAsset.position)).where(
                RecipeOriginalAsset.recipe_id == recipe.id
            )
        )
        position = (int(current_max) if current_max is not None else -1) + 1
        link = RecipeOriginalAsset(recipe_id=recipe.id, media_asset_id=asset.id, position=position)
        db.add(link)
        db.flush()
        return link
    except Exception:
        path.unlink(missing_ok=True)
        raise


def update_image(
    db: Session, recipe: Recipe, image: RecipeImage, payload: ImageMetadataInput
) -> RecipeImage:
    if image.recipe_id != recipe.id:
        raise HTTPException(status_code=404, detail="Das Bild wurde nicht gefunden.")
    if payload.is_cover:
        for other in recipe.images:
            if other.id != image.id and other.is_cover:
                other.is_cover = False
        db.flush()
        image.is_cover = True
    elif payload.is_cover is False and image.is_cover:
        image.is_cover = False
        replacement = next((item for item in recipe.images if item.id != image.id), None)
        if replacement:
            replacement.is_cover = True
    if payload.caption is not None:
        image.caption = payload.caption.strip() or None
    if payload.alt_text is not None:
        image.alt_text = payload.alt_text.strip() or None
    if payload.position is not None and payload.position != image.position:
        new_position = min(payload.position, max(len(recipe.images) - 1, 0))
        ordered = [item for item in recipe.images if item.id != image.id]
        ordered.insert(new_position, image)
        for position, item in enumerate(ordered):
            item.position = position + 10_000
        db.flush()
        for position, item in enumerate(ordered):
            item.position = position
    db.flush()
    return image


def remove_image(db: Session, recipe: Recipe, image: RecipeImage) -> list[MediaAsset]:
    if image.recipe_id != recipe.id:
        raise HTTPException(status_code=404, detail="Das Bild wurde nicht gefunden.")
    was_cover = image.is_cover
    removed_assets = [image.asset]
    if image.thumbnail_asset:
        removed_assets.append(image.thumbnail_asset)
    db.delete(image)
    db.flush()
    remaining = list(
        db.scalars(
            select(RecipeImage)
            .where(RecipeImage.recipe_id == recipe.id)
            .order_by(RecipeImage.position)
        )
    )
    for item in remaining:
        item.position += 10_000
    db.flush()
    for position, item in enumerate(remaining):
        item.position = position
    if was_cover and remaining:
        for item in remaining:
            item.is_cover = False
        db.flush()
        remaining[0].is_cover = True
    for asset in removed_assets:
        db.delete(asset)
    db.flush()
    return removed_assets


def get_asset(db: Session, asset_id: uuid.UUID) -> MediaAsset:
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Die Datei wurde nicht gefunden.")
    return asset


def asset_is_attached(db: Session, asset_id: uuid.UUID) -> bool:
    image = db.scalar(
        select(RecipeImage.id).where(
            (RecipeImage.media_asset_id == asset_id) | (RecipeImage.thumbnail_asset_id == asset_id)
        )
    )
    original = db.scalar(
        select(RecipeOriginalAsset.media_asset_id).where(
            RecipeOriginalAsset.media_asset_id == asset_id
        )
    )
    return bool(image or original)
