from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    ImportBatch,
    ImportCandidate,
    ImportJob,
    Recipe,
    RecipeComment,
    RecipeImage,
    RecipeOriginalAsset,
    User,
)
from app.schemas.recipe import CategoryPathInput, RecipeInput, RecipePackage
from app.services.media import create_asset, create_thumbnail_asset
from app.services.media_quota import enforce_recipe_quota
from app.services.recipes import create_recipe, refresh_search_document
from app.services.storage import resolve_storage_key, store_bytes


def import_recipe_package(
    db: Session, package: RecipePackage, user: User, *, batch: ImportBatch | None = None
) -> Recipe:
    data = package.recipe.model_dump(
        exclude={
            "comments",
            "images",
            "original_assets",
            "created_at",
            "updated_at",
            "created_by_name",
            "updated_by_name",
        }
    )
    data["categories"] = [
        CategoryPathInput(id=None, path=value.path, origin=value.origin)
        for value in package.recipe.categories
    ]
    data["status"] = "active"
    payload = RecipeInput.model_validate(data)
    written: list[Path] = []
    try:
        recipe = create_recipe(db, payload, user)
        for position, encoded in enumerate(package.recipe.images):
            binary = encoded.decoded(max_bytes=50 * 1024 * 1024)
            stored = store_bytes(
                binary,
                filename=encoded.filename,
                kind=encoded.kind,
                expected_sha256=encoded.sha256,
            )
            written.append(resolve_storage_key(stored.storage_key))
            asset = create_asset(db, stored, user, encoded.kind)
            thumbnail = create_thumbnail_asset(db, asset, user)
            if thumbnail:
                written.append(resolve_storage_key(thumbnail.storage_key))
            enforce_recipe_quota(db, recipe.id, [asset, thumbnail])
            recipe.images.append(
                RecipeImage(
                    media_asset_id=asset.id,
                    thumbnail_asset_id=thumbnail.id if thumbnail else None,
                    position=position,
                    is_cover=encoded.is_cover,
                    caption=encoded.caption,
                    alt_text=encoded.alt_text,
                    generation_metadata=encoded.generation_metadata,
                )
            )
            db.flush()
        if recipe.images and not any(image.is_cover for image in recipe.images):
            recipe.images[0].is_cover = True
        if sum(1 for image in recipe.images if image.is_cover) > 1:
            first = next(image for image in recipe.images if image.is_cover)
            for image in recipe.images:
                image.is_cover = image is first

        for position, encoded in enumerate(package.recipe.original_assets):
            binary = encoded.decoded(max_bytes=50 * 1024 * 1024)
            stored = store_bytes(
                binary,
                filename=encoded.filename,
                kind=encoded.kind,
                expected_sha256=encoded.sha256,
            )
            written.append(resolve_storage_key(stored.storage_key))
            asset = create_asset(db, stored, user, encoded.kind)
            enforce_recipe_quota(db, recipe.id, [asset])
            recipe.original_assets.append(
                RecipeOriginalAsset(media_asset_id=asset.id, position=position)
            )
            db.flush()

        for exported in package.recipe.comments:
            comment = RecipeComment(
                recipe_id=recipe.id,
                author_user_id=None,
                author_name_snapshot=exported.author_name,
                text=exported.text,
                created_at=exported.created_at.astimezone(UTC),
                updated_at=(exported.updated_at or exported.created_at).astimezone(UTC),
            )
            recipe.comments.append(comment)
        if package.recipe.created_at is not None:
            recipe.created_at = package.recipe.created_at.astimezone(UTC)
        if package.recipe.updated_at is not None:
            recipe.updated_at = package.recipe.updated_at.astimezone(UTC)
        recipe.created_by_name_snapshot = package.recipe.created_by_name or user.visible_name
        recipe.updated_by_name_snapshot = package.recipe.updated_by_name or user.visible_name
        db.flush()
        refresh_search_document(db, recipe)
        if batch is not None:
            batch.jobs = [
                ImportJob(
                    input_type="recipe_json",
                    status="completed",
                    progress=100,
                    current_stage="Import abgeschlossen",
                    result_recipe_id=recipe.id,
                    finished_at=datetime.now(UTC),
                    candidates=[
                        ImportCandidate(
                            position=0,
                            status="imported",
                            title=recipe.title,
                            confidence="high",
                            recipe_payload=payload.model_dump(mode="json"),
                            result_recipe_id=recipe.id,
                            finished_at=datetime.now(UTC),
                        )
                    ],
                )
            ]
            db.add(batch)
        db.commit()
        return recipe
    except Exception:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
