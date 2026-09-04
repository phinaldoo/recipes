from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, HttpUrl, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import csrf_user, current_user
from app.database import get_db
from app.i18n import Locale, normalize_locale, translate, translate_known_text
from app.imports import import_recipe_package
from app.imports.pipeline import (
    ImportSelectionError,
    import_selected_candidates,
    recompute_batch,
)
from app.imports.source_media import SourceRegionError, crop_source_region, normalize_image_source
from app.imports.url_security import UnsafeURL, validate_public_url
from app.models import ImportBatch, ImportCandidate, ImportJob, User
from app.schemas.ai import RecipeImageCandidate, RecipeSourceRegion
from app.schemas.recipe import RecipePackage
from app.services.media import create_asset
from app.services.media_quota import MediaQuotaExceeded
from app.services.storage import (
    InvalidUpload,
    StorageCapacityExceeded,
    resolve_storage_key,
    store_upload,
)
from app.upload_limits import ImportUploadRoute
from app.workers.tasks import import_batch_task, import_job_task

router = APIRouter(prefix="/imports", tags=["Importe"], route_class=ImportUploadRoute)
logger = logging.getLogger(__name__)


class URLImportPayload(BaseModel):
    urls: list[HttpUrl] = Field(min_length=1, max_length=20)


class ImportSelectionPayload(BaseModel):
    selected_candidate_ids: list[uuid.UUID] = Field(default_factory=list, max_length=400)


def _ensure_import_capacity(db: Session, user: User, requested: int) -> None:
    active = int(
        db.scalar(
            select(func.count())
            .select_from(ImportJob)
            .join(ImportBatch)
            .where(
                ImportBatch.created_by_user_id == user.id,
                ImportJob.status.in_(
                    [
                        "queued",
                        "preparing",
                        "extracting",
                        "checking_images",
                        "generating_image",
                        "validating",
                    ]
                ),
            )
        )
        or 0
    )
    if active + requested > 50:
        raise HTTPException(
            status_code=429,
            detail="Es dürfen höchstens 50 eigene Importaufträge gleichzeitig warten oder laufen.",
        )


def _candidate_dict(candidate: ImportCandidate, locale: Locale) -> dict[str, object]:
    payload = candidate.recipe_payload or {}
    return {
        "id": str(candidate.id),
        "status": candidate.status,
        "title": candidate.title,
        "description": payload.get("description"),
        "recipe_kind": payload.get("recipe_kind", "cooking"),
        "base_servings": payload.get("base_servings"),
        "serving_label": payload.get("serving_label"),
        "source_regions": candidate.source_regions_json,
        "warnings": [
            translate_known_text(locale, warning, fallback_key="import.warning.generic")
            for warning in candidate.warnings_json
        ],
        "confidence": candidate.confidence,
        "has_image": bool(candidate.image_asset_id or candidate.image_region_json),
        "result_recipe_id": (
            str(candidate.result_recipe_id) if candidate.result_recipe_id else None
        ),
        "error_message": translate_known_text(
            locale, candidate.error_message, fallback_key="error.generic"
        ),
    }


def _job_dict(job: ImportJob, locale: Locale) -> dict[str, object]:
    return {
        "id": str(job.id),
        "input_type": job.input_type,
        "status": job.status,
        "progress": job.progress,
        "current_stage": translate_known_text(
            locale, job.current_stage, fallback_key="job.processing"
        ),
        "source_url": job.source_url,
        "source_asset_id": str(job.source_asset_id) if job.source_asset_id else None,
        "result_recipe_id": str(job.result_recipe_id) if job.result_recipe_id else None,
        "error_code": job.error_code,
        "error_message": translate_known_text(
            locale, job.error_message, fallback_key="error.generic"
        ),
        "attempt_count": job.attempt_count,
        "candidates": [
            _candidate_dict(candidate, locale) for candidate in getattr(job, "candidates", [])
        ],
    }


def _check_batch_access(batch: ImportBatch, user: User) -> None:
    if batch.created_by_user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Dieser Import gehört zu einem anderen Benutzer."
        )


@router.post("/json", status_code=201)
async def import_json(
    file: UploadFile = File(...),
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    content = await file.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Das Rezeptpaket ist zu groß.")
    try:
        package = RecipePackage.model_validate_json(content)
        recipe = import_recipe_package(db, package, user)
        return {
            "recipe_id": str(recipe.id),
            "redirect": f"/rezepte/{recipe.id}",
            "message": translate(user.language, "api.import.package"),
        }
    except StorageCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except MediaQuotaExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="Das Rezeptpaket ist ungültig oder verwendet eine nicht unterstützte Version.",
        ) from exc


@router.post("/files", status_code=202)
async def import_files(
    files: list[UploadFile] = File(...),
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not files or len(files) > 20:
        raise HTTPException(status_code=422, detail="Bitte wähle zwischen 1 und 20 Dateien aus.")
    _ensure_import_capacity(db, user, len(files))
    batch = ImportBatch(
        created_by_user_id=user.id,
        status="queued",
        total_jobs=len(files),
        target_language=user.language or "de",
    )
    db.add(batch)
    db.flush()
    written = []
    try:
        for upload in files:
            stored = await store_upload(
                upload,
                allowed={
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "image/heic",
                    "application/pdf",
                },
            )
            written.append(resolve_storage_key(stored.storage_key))
            asset = create_asset(db, stored, user, "original_upload")
            input_type = "pdf" if stored.mime_type == "application/pdf" else "image"
            db.add(
                ImportJob(
                    batch_id=batch.id,
                    input_type=input_type,
                    source_asset_id=asset.id,
                    status="queued",
                    current_stage=translate(user.language, "job.waiting"),
                )
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        if isinstance(exc, StorageCapacityExceeded):
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        if isinstance(exc, MediaQuotaExceeded):
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        if isinstance(exc, InvalidUpload):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    try:
        import_batch_task.send(str(batch.id))
    except Exception:
        logger.exception("Import-Batch %s wird durch den Dispatcher nachgereicht", batch.id)
    return {"batch_id": str(batch.id), "redirect": f"/importieren/{batch.id}"}


@router.post("/urls", status_code=202)
def import_urls(
    payload: URLImportPayload,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _ensure_import_capacity(db, user, len(payload.urls))
    validated_urls: list[str] = []
    for ordinal, url in enumerate(payload.urls, start=1):
        try:
            validated_urls.append(validate_public_url(str(url)))
        except UnsafeURL as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Webadresse {ordinal}: {exc}",
            ) from exc

    batch = ImportBatch(
        created_by_user_id=user.id,
        status="queued",
        total_jobs=len(validated_urls),
        target_language=user.language or "de",
    )
    db.add(batch)
    db.flush()
    for validated_url in validated_urls:
        db.add(
            ImportJob(
                batch_id=batch.id,
                input_type="url",
                source_url=validated_url,
                status="queued",
                current_stage=translate(user.language, "job.waiting"),
            )
        )
    db.commit()
    try:
        import_batch_task.send(str(batch.id))
    except Exception:
        logger.exception("Import-Batch %s wird durch den Dispatcher nachgereicht", batch.id)
    return {"batch_id": str(batch.id), "redirect": f"/importieren/{batch.id}"}


@router.get("/batches/{batch_id}")
def batch_status(
    batch_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    batch = db.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.jobs).selectinload(ImportJob.candidates))
        .where(ImportBatch.id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Der Import wurde nicht gefunden.")
    _check_batch_access(batch, user)
    locale = normalize_locale(user.language) or "de"
    return {
        "id": str(batch.id),
        "status": batch.status,
        "total_jobs": batch.total_jobs,
        "completed_jobs": batch.completed_jobs,
        "failed_jobs": batch.failed_jobs,
        "jobs": [_job_dict(job, locale) for job in batch.jobs],
    }


@router.get("/jobs/{job_id}")
def job_status(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Der Importauftrag wurde nicht gefunden.")
    _check_batch_access(job.batch, user)
    locale = normalize_locale(user.language) or "de"
    return _job_dict(job, locale)


@router.get("/candidates/{candidate_id}/image")
def view_candidate_image(
    candidate_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    candidate = db.get(ImportCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Das erkannte Rezept wurde nicht gefunden.")
    _check_batch_access(candidate.job.batch, user)
    asset = candidate.thumbnail_asset or candidate.image_asset
    if asset is not None:
        return FileResponse(
            resolve_storage_key(asset.storage_key),
            media_type=asset.mime_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    if candidate.image_region_json is None or candidate.job.source_asset is None:
        raise HTTPException(status_code=404, detail="Für dieses Rezept wurde kein Bild erkannt.")
    try:
        image_candidate = RecipeImageCandidate.model_validate(candidate.image_region_json)
        source_asset = candidate.job.source_asset
        source_content = resolve_storage_key(source_asset.storage_key).read_bytes()
        source_mime = source_asset.mime_type
        if source_mime != "application/pdf":
            source_content = normalize_image_source(source_content)
            source_mime = "image/png"
        region = RecipeSourceRegion(
            page=image_candidate.page,
            bounding_box=image_candidate.bounding_box,
        )
        image = crop_source_region(source_content, source_mime, region, max_dimension=1200)
    except (OSError, SourceRegionError, ValidationError) as exc:
        raise HTTPException(
            status_code=404,
            detail="Der erkannte Bildausschnitt ist nicht mehr verfügbar.",
        ) from exc
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/batches/{batch_id}/confirm")
def confirm_candidates(
    batch_id: uuid.UUID,
    payload: ImportSelectionPayload,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    batch = db.scalar(
        select(ImportBatch)
        .options(
            selectinload(ImportBatch.jobs)
            .selectinload(ImportJob.candidates)
            .selectinload(ImportCandidate.image_asset),
            selectinload(ImportBatch.jobs)
            .selectinload(ImportJob.candidates)
            .selectinload(ImportCandidate.thumbnail_asset),
            selectinload(ImportBatch.jobs).selectinload(ImportJob.source_asset),
        )
        .where(ImportBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Der Import wurde nicht gefunden.")
    _check_batch_access(batch, user)
    try:
        recipes, cleanup_paths = import_selected_candidates(
            db,
            batch=batch,
            selected_ids=set(payload.selected_candidate_ids),
            user=user,
        )
        db.commit()
    except ImportSelectionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MediaQuotaExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    for path in cleanup_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Verworfenes temporäres Importbild konnte nicht entfernt werden: %s", path
            )
    recipe_ids = [str(recipe.id) for recipe in recipes]
    return {
        "recipe_ids": recipe_ids,
        "redirect": f"/rezepte/{recipe_ids[0]}" if len(recipe_ids) == 1 else "/rezepte",
        "message": (
            translate(user.language, "api.import.empty")
            if not recipe_ids
            else translate(user.language, "api.import.one")
            if len(recipe_ids) == 1
            else translate(user.language, "api.import.other", count=len(recipe_ids))
        ),
    }


@router.get("/jobs/{job_id}/source")
def view_job_source(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    job = db.get(ImportJob, job_id)
    if job is None or job.source_asset is None:
        raise HTTPException(status_code=404, detail="Die Originaldatei wurde nicht gefunden.")
    _check_batch_access(job.batch, user)
    return FileResponse(
        resolve_storage_key(job.source_asset.storage_key),
        media_type=job.source_asset.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(
    job_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Der Importauftrag wurde nicht gefunden.")
    _check_batch_access(job.batch, user)
    if job.status != "failed":
        raise HTTPException(
            status_code=409, detail="Nur fehlgeschlagene Aufträge können wiederholt werden."
        )
    if job.input_type != "url" and job.source_asset_id is None:
        raise HTTPException(
            status_code=410,
            detail="Die aufbewahrte Originaldatei ist abgelaufen und kann nicht erneut verarbeitet werden.",
        )
    job.status = "queued"
    job.progress = 0
    job.current_stage = translate(user.language, "job.waiting")
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    job.lease_token = None
    job.lease_expires_at = None
    recompute_batch(db, job.batch_id)
    db.commit()
    try:
        import_job_task.send(str(job.id))
    except Exception:
        logger.exception("Importauftrag %s wird durch den Dispatcher nachgereicht", job.id)
    return {"message": translate(user.language, "api.import.retry")}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Der Importauftrag wurde nicht gefunden.")
    _check_batch_access(job.batch, user)
    if job.status != "queued":
        raise HTTPException(
            status_code=409, detail="Nur wartende Aufträge können abgebrochen werden."
        )
    job.status = "cancelled"
    job.progress = 0
    job.current_stage = translate(user.language, "job.cancelled")
    job.finished_at = datetime.now(UTC)
    recompute_batch(db, job.batch_id)
    db.commit()
    return {"message": translate(user.language, "api.import.cancelled")}
