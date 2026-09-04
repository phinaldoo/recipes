from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import shutil
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.auth.dependencies import admin_user, csrf_admin
from app.auth.security import verify_password
from app.backups import InvalidBackup, preflight_backup
from app.backups.schemas import DATABASE_SCHEMA_VERSION
from app.config import get_settings
from app.database import get_db
from app.i18n import Locale, translate, translate_known_text
from app.maintenance import NON_TERMINAL_IMPORT_STATUSES
from app.models import (
    AuditLog,
    BackupRestoreJob,
    Category,
    ImportJob,
    MediaAsset,
    Recipe,
    RecipeComment,
    User,
)
from app.schemas.recipe import RestoreConfirmation
from app.upload_limits import RestoreUploadRoute
from app.workers.tasks import backup_task, restore_task

router = APIRouter(prefix="/settings", tags=["Einstellungen"], route_class=RestoreUploadRoute)
logger = logging.getLogger(__name__)


def _job_dict(job: BackupRestoreJob, locale: Locale | str | None = "de") -> dict[str, object]:
    summary = dict(job.summary_json or {})
    if isinstance(summary.get("warnings"), list):
        summary["warnings"] = [
            translate_known_text(locale, str(message), fallback_key="error.validation_short")
            for message in summary["warnings"]
        ]
    return {
        "id": str(job.id),
        "operation": job.operation,
        "status": job.status,
        "progress": job.progress,
        "current_stage": translate_known_text(
            locale, job.current_stage, fallback_key="job.processing"
        ),
        "summary": summary,
        "error_message": translate_known_text(
            locale, job.error_message, fallback_key="error.generic"
        ),
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "download_available": bool(
            job.operation == "export" and job.status == "completed" and job.archive_filename
        ),
    }


@router.get("/system-summary")
def system_summary(
    _: User = Depends(admin_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    storage_rows = db.execute(
        select(MediaAsset.kind, func.coalesce(func.sum(MediaAsset.byte_size), 0)).group_by(
            MediaAsset.kind
        )
    ).all()
    bytes_by_kind: dict[str, int] = {kind: int(byte_size) for kind, byte_size in storage_rows}
    return {
        "application_version": __version__,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "counts": {
            "users": int(db.scalar(select(func.count()).select_from(User)) or 0),
            "recipes": int(
                db.scalar(
                    select(func.count()).select_from(Recipe).where(Recipe.deleted_at.is_(None))
                )
                or 0
            ),
            "comments": int(
                db.scalar(
                    select(func.count())
                    .select_from(RecipeComment)
                    .where(RecipeComment.deleted_at.is_(None))
                )
                or 0
            ),
            "categories": int(db.scalar(select(func.count()).select_from(Category)) or 0),
            "files": int(db.scalar(select(func.count()).select_from(MediaAsset)) or 0),
        },
        "storage_bytes_by_kind": bytes_by_kind,
    }


@router.post("/backups", status_code=202)
def create_backup(
    user: User = Depends(csrf_admin), db: Session = Depends(get_db)
) -> dict[str, object]:
    running = db.scalar(
        select(BackupRestoreJob.id).where(BackupRestoreJob.status.in_(["queued", "running"]))
    )
    if running:
        raise HTTPException(
            status_code=409, detail="Es läuft bereits ein Backup oder eine Wiederherstellung."
        )
    active_import = db.scalar(
        select(ImportJob.id).where(ImportJob.status.in_(NON_TERMINAL_IMPORT_STATUSES))
    )
    if active_import:
        raise HTTPException(
            status_code=409,
            detail="Vor dem Backup müssen laufende und wartende Importe abgeschlossen oder abgebrochen werden.",
        )
    job = BackupRestoreJob(
        requested_by_user_id=user.id,
        operation="export",
        status="queued",
        current_stage=translate(user.language, "job.waiting"),
    )
    db.add(job)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="backup.requested",
            target_type="backup_restore_job",
            target_id=str(job.id),
        )
    )
    db.commit()
    try:
        backup_task.send(str(job.id))
    except Exception:
        logger.exception("Backup-Auftrag %s wird durch den Dispatcher nachgereicht", job.id)
    return {"job": _job_dict(job, user.language)}


def _job(db: Session, job_id: uuid.UUID, operation: str | None = None) -> BackupRestoreJob:
    job = db.get(BackupRestoreJob, job_id)
    if job is None or (operation and job.operation != operation):
        raise HTTPException(status_code=404, detail="Der Auftrag wurde nicht gefunden.")
    return job


@router.get("/backups/{job_id}")
def backup_status(
    job_id: uuid.UUID,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"job": _job_dict(_job(db, job_id, "export"), _.language)}


@router.get("/backups/{job_id}/download")
def backup_download(
    job_id: uuid.UUID,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    job = _job(db, job_id, "export")
    if job.status != "completed" or not job.archive_filename:
        raise HTTPException(
            status_code=409, detail="Das Backup steht noch nicht zum Download bereit."
        )
    if job.finished_at is None:
        raise HTTPException(status_code=409, detail="Das Backup ist noch nicht abgeschlossen.")
    if datetime.now(UTC) - job.finished_at > timedelta(
        hours=get_settings().backup_download_retention_hours
    ):
        path = (get_settings().backup_temp_root / job.archive_filename).resolve()
        if get_settings().backup_temp_root.resolve() in path.parents:
            path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=410, detail="Der Download ist abgelaufen. Erstelle ein neues Backup."
        )
    path = (get_settings().backup_temp_root / job.archive_filename).resolve()
    if get_settings().backup_temp_root.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Die Backup-Datei ist nicht mehr vorhanden.")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=job.archive_filename,
        headers={"Cache-Control": "private, no-store"},
    )


def _preflight_token(job: BackupRestoreJob) -> str:
    message = f"{job.id}:{job.archive_sha256}".encode()
    digest = hmac.new(get_settings().app_secret_key.encode(), message, hashlib.sha256).hexdigest()
    return f"{job.id}.{digest}"


@router.post("/restores/preflight", status_code=201)
async def restore_preflight(
    file: UploadFile = File(...),
    user: User = Depends(csrf_admin),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    settings = get_settings()
    filename = f"restore-upload-{secrets.token_urlsafe(24)}.zip"
    path = settings.backup_temp_root / filename
    size = 0
    digest = hashlib.sha256()
    try:
        with path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_backup_upload_bytes:
                    raise InvalidBackup("Das Backup überschreitet die erlaubte Größe")
                output.write(chunk)
                digest.update(chunk)
        result = preflight_backup(path)
        free_bytes = shutil.disk_usage(settings.storage_root).free
        if free_bytes < result.required_disk_bytes:
            raise InvalidBackup(
                "Für Sicherheitsbackup und Wiederherstellung ist nicht genügend Speicherplatz frei"
            )
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, InvalidBackup):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    job = BackupRestoreJob(
        requested_by_user_id=user.id,
        operation="restore",
        status="preflight_complete",
        archive_filename=filename,
        archive_sha256=digest.hexdigest(),
        progress=0,
        current_stage=translate(user.language, "settings.preflight_passed"),
        summary_json=result.model_dump(mode="json"),
    )
    db.add(job)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="restore.preflight_completed",
            target_type="backup_restore_job",
            target_id=str(job.id),
            details={"archive_sha256": job.archive_sha256},
        )
    )
    db.commit()
    return {
        "job": _job_dict(job, user.language),
        "preflight_token": _preflight_token(job),
    }


@router.post("/restores", status_code=202)
def start_restore(
    payload: RestoreConfirmation,
    user: User = Depends(csrf_admin),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if payload.confirmation != translate(user.language, "settings.confirmation_word"):
        raise HTTPException(
            status_code=422,
            detail=translate(user.language, "settings.confirmation_error"),
        )
    try:
        raw_id, _ = payload.preflight_token.split(".", 1)
        job_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=422, detail="Die Vorabprüfung ist ungültig oder abgelaufen."
        ) from exc
    job = _job(db, job_id, "restore")
    if not hmac.compare_digest(payload.preflight_token, _preflight_token(job)):
        raise HTTPException(
            status_code=422, detail="Die Vorabprüfung ist ungültig oder abgelaufen."
        )
    if job.requested_by_user_id != user.id:
        raise HTTPException(
            status_code=403, detail="Die Vorabprüfung gehört zu einem anderen Administrator."
        )
    if job.status != "preflight_complete":
        raise HTTPException(status_code=409, detail="Dieser Restore wurde bereits gestartet.")
    running = db.scalar(
        select(BackupRestoreJob.id).where(
            BackupRestoreJob.id != job.id,
            BackupRestoreJob.status.in_(["queued", "running"]),
        )
    )
    if running:
        raise HTTPException(
            status_code=409, detail="Es läuft bereits ein Backup oder eine Wiederherstellung."
        )
    active_import = db.scalar(
        select(ImportJob.id).where(ImportJob.status.in_(NON_TERMINAL_IMPORT_STATUSES))
    )
    if active_import:
        raise HTTPException(
            status_code=409,
            detail="Vor der Wiederherstellung müssen laufende und wartende Importe abgeschlossen oder abgebrochen werden.",
        )
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Das Passwort ist nicht korrekt.")
    path = (get_settings().backup_temp_root / (job.archive_filename or "")).resolve()
    if get_settings().backup_temp_root.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Die Restore-Datei ist nicht mehr vorhanden.")
    job.status = "queued"
    job.current_stage = translate(user.language, "job.maintenance_wait")
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="restore.requested",
            target_type="backup_restore_job",
            target_id=str(job.id),
            details={"archive_sha256": job.archive_sha256},
        )
    )
    db.commit()
    try:
        restore_task.send(str(job.id), str(path))
    except Exception:
        logger.exception("Restore-Auftrag %s wird durch den Dispatcher nachgereicht", job.id)
    return {"job": _job_dict(job, user.language)}


@router.get("/restores/{job_id}")
def restore_status(
    job_id: uuid.UUID,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"job": _job_dict(_job(db, job_id, "restore"), _.language)}
