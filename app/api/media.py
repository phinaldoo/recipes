from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import csrf_user, current_user
from app.config import get_settings
from app.database import get_db
from app.i18n import translate
from app.models import ImageGenerationJob, RecipeImage, User
from app.schemas.recipe import ImageMetadataInput
from app.services.image_generation import (
    get_active_image_generation_job,
    image_generation_available,
    image_generation_job_dict,
)
from app.services.media import (
    add_recipe_image,
    asset_is_attached,
    get_asset,
    remove_image,
    update_image,
)
from app.services.media_quota import MediaQuotaExceeded
from app.services.recipes import get_recipe
from app.services.storage import (
    InvalidUpload,
    StorageCapacityExceeded,
    resolve_storage_key,
    safe_download_name,
)
from app.upload_limits import ProtectedUploadRoute
from app.workers.tasks import image_generation_task

router = APIRouter(tags=["Medien"], route_class=ProtectedUploadRoute)
logger = logging.getLogger(__name__)


@router.post("/recipes/{recipe_id}/image-generation", status_code=202)
def start_image_generation(
    recipe_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not image_generation_available(get_settings()):
        raise HTTPException(status_code=503, detail="Die Bildgenerierung ist nicht konfiguriert.")
    recipe = get_recipe(db, recipe_id, for_update=True)

    job = get_active_image_generation_job(db, recipe.id)
    created = job is None
    if job is None:
        current_cover = recipe.cover_image
        generation_mode = "regenerate" if current_cover is not None else "create"
        job = ImageGenerationJob(
            recipe_id=recipe.id,
            requested_by_user_id=user.id,
            previous_cover_image_id=(current_cover.id if current_cover else None),
            generation_mode=generation_mode,
            status="queued",
            current_stage=translate(
                user.language,
                "recipe.image.waiting_new"
                if generation_mode == "regenerate"
                else "recipe.image.waiting",
            ),
            attempt_count=0,
        )
        db.add(job)
        db.flush()
        db.commit()
    if created:
        try:
            image_generation_task.send(str(job.id))
        except Exception:
            logger.exception("Rezeptbild-Auftrag %s wird durch den Dispatcher nachgereicht", job.id)
    return {
        "job": image_generation_job_dict(job, user.language),
        "message": translate(
            user.language,
            (
                "recipe.image.started_regenerate"
                if created and job.generation_mode == "regenerate"
                else "recipe.image.started_create"
                if created
                else "recipe.image.already_running"
            ),
        ),
    }


@router.get("/recipes/{recipe_id}/image-generation/{job_id}")
def image_generation_status(
    recipe_id: uuid.UUID,
    job_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    get_recipe(db, recipe_id)
    job = db.get(ImageGenerationJob, job_id)
    if job is None or job.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail="Der Bildauftrag wurde nicht gefunden.")
    return {"job": image_generation_job_dict(job, _.language)}


@router.post("/recipes/{recipe_id}/images", status_code=201)
async def upload_image(
    recipe_id: uuid.UUID,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    alt_text: str | None = Form(default=None),
    is_cover: bool = Form(default=False),
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    try:
        image = await add_recipe_image(
            db,
            recipe,
            user,
            file,
            caption=caption,
            alt_text=alt_text,
            is_cover=is_cover,
        )
        paths = [resolve_storage_key(image.asset.storage_key)]
        if image.thumbnail_asset:
            paths.append(resolve_storage_key(image.thumbnail_asset.storage_key))
        try:
            db.commit()
        except Exception:
            db.rollback()
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return {
            "image": {
                "id": str(image.id),
                "asset_id": str(image.media_asset_id),
                "position": image.position,
                "is_cover": image.is_cover,
                "caption": image.caption,
                "alt_text": image.alt_text,
            },
            "message": translate(user.language, "api.image.uploaded"),
        }
    except StorageCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except MediaQuotaExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except InvalidUpload as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _image(db: Session, recipe_id: uuid.UUID, image_id: uuid.UUID) -> RecipeImage:
    image = db.scalar(
        select(RecipeImage).where(RecipeImage.id == image_id, RecipeImage.recipe_id == recipe_id)
    )
    if image is None:
        raise HTTPException(status_code=404, detail="Das Bild wurde nicht gefunden.")
    return image


@router.put("/recipes/{recipe_id}/images/{image_id}")
def change_image(
    recipe_id: uuid.UUID,
    image_id: uuid.UUID,
    payload: ImageMetadataInput,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    update_image(db, recipe, _image(db, recipe_id, image_id), payload)
    db.commit()
    return {"message": translate(_.language, "api.image.saved")}


@router.delete("/recipes/{recipe_id}/images/{image_id}")
def delete_image(
    recipe_id: uuid.UUID,
    image_id: uuid.UUID,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    assets = remove_image(db, recipe, _image(db, recipe_id, image_id))
    paths = [resolve_storage_key(asset.storage_key) for asset in assets]
    db.commit()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Verwaiste Bilddatei konnte nicht entfernt werden: %s", path)
    return {"message": translate(_.language, "api.image.removed")}


@router.get("/assets/{asset_id}/view")
def view_asset(
    asset_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    asset = get_asset(db, asset_id)
    if not asset_is_attached(db, asset_id):
        raise HTTPException(status_code=404, detail="Die Datei ist keinem Rezept zugeordnet.")
    return FileResponse(
        resolve_storage_key(asset.storage_key),
        media_type=asset.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/assets/{asset_id}/download")
def download_asset(
    asset_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    asset = get_asset(db, asset_id)
    if not asset_is_attached(db, asset_id):
        raise HTTPException(status_code=404, detail="Die Datei ist keinem Rezept zugeordnet.")
    return FileResponse(
        resolve_storage_key(asset.storage_key),
        media_type="application/octet-stream",
        filename=safe_download_name(asset.original_filename),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
