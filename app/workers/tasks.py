from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dramatiq
from redis import Redis
from sqlalchemy import or_, select, update

from app.backups import export_backup, restore_backup
from app.config import get_settings
from app.database import SessionLocal
from app.imports.pipeline import ImportMaintenance, process_import_batch, process_import_job
from app.maintenance import (
    NON_TERMINAL_IMPORT_STATUSES,
    database_maintenance_exclusive_guard,
    database_maintenance_shared_guard,
)
from app.models import AuditLog, BackupRestoreJob, ImportJob
from app.services.image_generation import process_image_generation_job
from app.services.storage import recover_interrupted_restore
from app.workers.broker import broker  # noqa: F401

logger = logging.getLogger(__name__)
# Compatibility seam for focused worker tests and callers that previously
# patched this name. Restore/backup both require the exclusive variant.
database_maintenance_guard = database_maintenance_exclusive_guard
MAINTENANCE_LOCK_KEY = "maintenance:job-lock"
LOCK_TTL_SECONDS = 120
LEASE_HEARTBEAT_SECONDS = 30
RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


def _release_lock(redis: Redis, key: str, token: str) -> None:
    try:
        redis.eval(RELEASE_LOCK_SCRIPT, 1, key, token)
    except Exception:
        logger.exception("Wartungssperre %s konnte nicht entfernt werden", key)


def _lease_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=LOCK_TTL_SECONDS)


@contextmanager
def _maintenance_heartbeat(
    redis: Redis,
    identifier: uuid.UUID,
    token: str,
    keys: tuple[str, ...],
) -> Iterator[None]:
    stopped = threading.Event()

    def renew() -> None:
        while not stopped.wait(LEASE_HEARTBEAT_SECONDS):
            try:
                for key in keys:
                    if not redis.eval(RENEW_LOCK_SCRIPT, 1, key, token, str(LOCK_TTL_SECONDS)):
                        logger.error(
                            "Wartungslease %s gehört nicht mehr zu Auftrag %s", key, identifier
                        )
                with SessionLocal() as lease_db:
                    lease_db.execute(
                        update(BackupRestoreJob)
                        .where(
                            BackupRestoreJob.id == identifier,
                            BackupRestoreJob.status == "running",
                            BackupRestoreJob.lease_token == token,
                        )
                        .values(lease_expires_at=_lease_deadline())
                    )
                    lease_db.commit()
            except Exception:
                # PostgreSQL's exclusive/shared barrier remains authoritative;
                # a transient Redis failure merely makes web writes fail closed.
                logger.exception(
                    "Wartungslease für Auftrag %s konnte nicht erneuert werden", identifier
                )

    thread = threading.Thread(target=renew, name=f"maintenance-lease-{identifier}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)


def _claim_maintenance_job(identifier: uuid.UUID, token: str, stage: str) -> bool:
    now = datetime.now(UTC)
    with database_maintenance_shared_guard(), SessionLocal() as db:
        claimed = db.scalar(
            update(BackupRestoreJob)
            .where(BackupRestoreJob.id == identifier, BackupRestoreJob.status == "queued")
            .values(
                status="running",
                progress=5,
                current_stage=stage,
                started_at=now,
                finished_at=None,
                error_message=None,
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=LOCK_TTL_SECONDS),
            )
            .returning(BackupRestoreJob.id)
        )
        db.commit()
        return claimed is not None


def _mark_maintenance_failure(identifier: uuid.UUID, token: str, *, operation: str) -> None:
    with database_maintenance_shared_guard(), SessionLocal() as db:
        job = db.get(BackupRestoreJob, identifier)
        if job is None or job.status != "running" or job.lease_token != token:
            return
        job.status = "failed"
        job.current_stage = (
            "Backup fehlgeschlagen" if operation == "export" else "Wiederherstellung fehlgeschlagen"
        )
        job.error_message = (
            "Das Backup konnte nicht erstellt werden. Der Datenbestand wurde nicht verändert."
            if operation == "export"
            else "Die Wiederherstellung wurde abgebrochen oder durch die Recovery abgeschlossen."
        )
        job.finished_at = datetime.now(UTC)
        job.lease_token = None
        job.lease_expires_at = None
        db.commit()


def requeue_stale_maintenance_jobs() -> list[uuid.UUID]:
    """Make crashed maintenance deliveries dispatchable after their lease expires."""
    now = datetime.now(UTC)
    legacy_cutoff = now - timedelta(minutes=5)
    with database_maintenance_shared_guard(), SessionLocal() as db:
        identifiers = list(
            db.scalars(
                update(BackupRestoreJob)
                .where(
                    BackupRestoreJob.status == "running",
                    or_(
                        BackupRestoreJob.lease_expires_at < now,
                        (
                            BackupRestoreJob.lease_expires_at.is_(None)
                            & (BackupRestoreJob.started_at < legacy_cutoff)
                        ),
                    ),
                )
                .values(
                    status="queued",
                    progress=0,
                    current_stage="Wird nach Worker-Unterbrechung fortgesetzt",
                    lease_token=None,
                    lease_expires_at=None,
                )
                .returning(BackupRestoreJob.id)
            )
        )
        db.commit()
        return identifiers


def recover_pending_restore() -> bool:
    """Let a healthy app process finish a journal left by a dead worker."""
    settings = get_settings()
    if not (settings.storage_root / ".restore-journal.json").exists():
        return False
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    token = uuid.uuid4().hex
    lock_acquired = False
    restore_flag_set = False
    resolved = False
    try:
        lock_acquired = bool(redis.set(MAINTENANCE_LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS))
        if not lock_acquired:
            return False
        restore_flag_set = bool(
            redis.set("maintenance:restore", token, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not restore_flag_set:
            return False
        with database_maintenance_guard():
            resolved = recover_interrupted_restore(settings)
        return resolved
    finally:
        # If authority could not be established, retain the write-pause until
        # its short TTL expires; the dispatcher retries recovery on a later run.
        if restore_flag_set and resolved:
            _release_lock(redis, "maintenance:restore", token)
        if lock_acquired:
            _release_lock(redis, MAINTENANCE_LOCK_KEY, token)


def _active_import_exists(db: object) -> bool:
    return bool(
        db.scalar(  # type: ignore[attr-defined]
            select(ImportJob.id).where(ImportJob.status.in_(NON_TERMINAL_IMPORT_STATUSES)).limit(1)
        )
    )


@dramatiq.actor(max_retries=20, min_backoff=30_000, max_backoff=60_000, queue_name="imports")
def import_batch_task(batch_id: str) -> None:
    try:
        process_import_batch(uuid.UUID(batch_id))
    except ImportMaintenance as exc:
        raise dramatiq.Retry(message=str(exc), delay=60_000) from exc


@dramatiq.actor(max_retries=20, min_backoff=30_000, max_backoff=60_000, queue_name="imports")
def import_job_task(job_id: str) -> None:
    try:
        process_import_job(uuid.UUID(job_id))
    except ImportMaintenance as exc:
        raise dramatiq.Retry(message=str(exc), delay=60_000) from exc


@dramatiq.actor(max_retries=0, queue_name="images")
def image_generation_task(job_id: str) -> None:
    process_image_generation_job(uuid.UUID(job_id))


@dramatiq.actor(max_retries=0, queue_name="maintenance")
def backup_task(job_id: str) -> None:
    identifier = uuid.UUID(job_id)
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    lock_token = uuid.uuid4().hex
    lock_acquired = False
    backup_flag_set = False
    archive: Path | None = None
    if not _claim_maintenance_job(identifier, lock_token, "Datenbestand wird gesichert"):
        return
    try:
        lock_acquired = bool(
            redis.set(MAINTENANCE_LOCK_KEY, lock_token, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not lock_acquired:
            raise RuntimeError("Es läuft bereits ein anderer Wartungsauftrag")
        backup_flag_set = bool(
            redis.set("maintenance:backup", lock_token, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not backup_flag_set:
            raise RuntimeError("Der Backup-Wartungsmodus konnte nicht aktiviert werden")
        with (
            _maintenance_heartbeat(
                redis, identifier, lock_token, (MAINTENANCE_LOCK_KEY, "maintenance:backup")
            ),
            database_maintenance_guard(),
            SessionLocal() as db,
        ):
            # The visible flag is set before this call. The exclusive database
            # lock now drains in-flight requests/imports and prevents a new one
            # from entering the snapshot interval.
            journal_path = settings.storage_root / ".restore-journal.json"
            if journal_path.exists() and not recover_interrupted_restore(settings):
                raise RuntimeError("Ein früherer Restore konnte nicht sicher recovered werden")
            if _active_import_exists(db):
                raise RuntimeError("Ein Import ist noch nicht terminal")
            # ``export_backup`` starts a REPEATABLE READ, READ ONLY
            # transaction. PostgreSQL requires SET TRANSACTION to be its first
            # statement, so end the preceding read-only import-state check.
            db.rollback()
            archive, manifest, digest = export_backup(db)
            db.rollback()
            job = db.get(BackupRestoreJob, identifier)
            if job is None or job.status != "running" or job.lease_token != lock_token:
                raise RuntimeError("Die Wartungslease wurde während des Backups verloren")
            job.status = "completed"
            job.progress = 100
            job.current_stage = "Backup wurde vollständig geprüft"
            job.archive_filename = archive.name
            job.archive_sha256 = digest
            job.summary_json = manifest.model_dump(mode="json")
            job.finished_at = datetime.now(UTC)
            job.lease_token = None
            job.lease_expires_at = None
            db.add(
                AuditLog(
                    actor_user_id=job.requested_by_user_id,
                    action="backup.completed",
                    target_type="backup_restore_job",
                    target_id=str(job.id),
                    details={"archive_sha256": digest},
                )
            )
            db.commit()
    except Exception:
        logger.exception("Backup-Auftrag %s ist fehlgeschlagen", job_id)
        if archive is not None:
            archive.unlink(missing_ok=True)
        _mark_maintenance_failure(identifier, lock_token, operation="export")
    finally:
        if backup_flag_set:
            _release_lock(redis, "maintenance:backup", lock_token)
        if lock_acquired:
            _release_lock(redis, MAINTENANCE_LOCK_KEY, lock_token)


@dramatiq.actor(max_retries=0, queue_name="maintenance")
def restore_task(job_id: str, archive_path: str) -> None:
    identifier = uuid.UUID(job_id)
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    lock_token = uuid.uuid4().hex
    lock_acquired = False
    restore_flag_set = False
    settings = get_settings()
    archive = Path(archive_path)
    claimed = _claim_maintenance_job(identifier, lock_token, "Sicherheitsbackup wird erstellt")
    if not claimed:
        return
    try:
        lock_acquired = bool(
            redis.set(MAINTENANCE_LOCK_KEY, lock_token, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not lock_acquired:
            raise RuntimeError("Es läuft bereits ein anderer Wartungsauftrag")
        restore_flag_set = bool(
            redis.set("maintenance:restore", lock_token, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not restore_flag_set:
            raise RuntimeError("Der Wartungsmodus konnte nicht aktiviert werden")
        with (
            _maintenance_heartbeat(
                redis, identifier, lock_token, (MAINTENANCE_LOCK_KEY, "maintenance:restore")
            ),
            database_maintenance_guard(),
            SessionLocal() as db,
        ):
            journal_path = settings.storage_root / ".restore-journal.json"
            if journal_path.exists() and not recover_interrupted_restore(settings):
                raise RuntimeError("Ein früherer Restore konnte nicht sicher recovered werden")
            if _active_import_exists(db):
                raise RuntimeError("Ein Import ist noch nicht terminal")
            # The safety backup inside restore_backup uses the same strict
            # snapshot transaction and must likewise start on a clean Session.
            db.rollback()
            restore_backup(db, archive, restore_id=job_id)
            # Completion job and audit are committed atomically with the
            # restored corpus by restore_backup().
    except Exception:
        logger.exception("Restore-Auftrag %s ist fehlgeschlagen", job_id)
        _mark_maintenance_failure(identifier, lock_token, operation="restore")
    finally:
        if restore_flag_set:
            _release_lock(redis, "maintenance:restore", lock_token)
        if lock_acquired:
            _release_lock(redis, MAINTENANCE_LOCK_KEY, lock_token)
        # Preserve the upload while an unresolved journal exists; it is useful
        # for operator recovery and cleanup retention still bounds its lifetime.
        if not (settings.storage_root / ".restore-journal.json").exists():
            archive.unlink(missing_ok=True)
