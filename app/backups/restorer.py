from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Table, delete, select, text
from sqlalchemy.orm import Session

from app.backups.errors import InvalidBackup
from app.backups.exporter import BACKUP_MODELS, export_backup, table_name
from app.backups.preflight import preflight_backup, safe_archive_name
from app.config import Settings, get_settings
from app.models import (
    AppSetting,
    AuditLog,
    BackupRestoreJob,
    ImageGenerationJob,
    User,
    UserSession,
)
from app.services.storage import (
    active_storage_root,
    atomic_write_json,
    recover_interrupted_restore,
    swap_active_generation,
)

INSERT_ORDER = BACKUP_MODELS
DELETE_ORDER = tuple(reversed(BACKUP_MODELS))
logger = logging.getLogger(__name__)


def decode_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("$type") == "uuid":
        return uuid.UUID(value["value"])
    if isinstance(value, dict) and value.get("$type") == "datetime":
        return datetime.fromisoformat(value["value"])
    if isinstance(value, dict) and value.get("$type") == "decimal":
        return Decimal(value["value"])
    if isinstance(value, dict):
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def _extract_media(
    archive_path: Path, generation: Path, expected_checksums: dict[str, str]
) -> None:
    generation.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        media_entries = [
            entry
            for entry in archive.infolist()
            if safe_archive_name(entry.filename).startswith("media/")
            and not entry.filename.endswith("/")
        ]
        if len(media_entries) != len(expected_checksums) or {
            entry.filename for entry in media_entries
        } != set(expected_checksums):
            raise InvalidBackup(
                "Das Archiv wurde nach der Vorabprüfung verändert oder enthält andere Mediendateien"
            )
        for entry in media_entries:
            name = safe_archive_name(entry.filename)
            relative = PurePathWithoutTraversal(name.removeprefix("media/"))
            target = generation / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            file_digest = hashlib.sha256()
            with archive.open(entry) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    file_digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if file_digest.hexdigest() != expected_checksums[name]:
                raise InvalidBackup(f"Die Prüfsumme von {name} stimmt nach dem Entpacken nicht")


def PurePathWithoutTraversal(value: str) -> Path:
    safe = safe_archive_name(value)
    return Path(*safe.split("/"))


def _parent_first_categories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = {row["id"]: row for row in rows}
    ordered: list[dict[str, Any]] = []
    emitted: set[uuid.UUID] = set()
    while pending:
        ready = [
            row
            for row in pending.values()
            if row.get("parent_id") is None or row["parent_id"] in emitted
        ]
        if not ready:
            raise InvalidBackup("Der Kategoriebaum enthält einen Zyklus oder fehlende Eltern")
        ready.sort(key=lambda row: (row.get("position", 0), str(row.get("name", "")).casefold()))
        for row in ready:
            ordered.append(row)
            emitted.add(row["id"])
            pending.pop(row["id"])
    return ordered


def restore_backup(
    db: Session,
    archive_path: Path,
    *,
    restore_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    preflight = preflight_backup(archive_path, settings)
    restore_id = restore_id or str(uuid.uuid4())

    # Always make a verified safety backup before touching current state.
    safety_path = settings.backup_temp_root / f"sicherheitsbackup-vor-restore-{restore_id}.zip"
    export_backup(db, destination=safety_path, settings=settings)
    db.rollback()  # end the read-only repeatable-read transaction used for the safety backup
    if shutil.disk_usage(settings.storage_root).free < preflight.required_disk_bytes:
        raise InvalidBackup(
            "Nach dem Sicherheitsbackup ist nicht genügend Speicherplatz für den Restore frei"
        )

    generation = settings.storage_root / "generations" / f"restore-{restore_id}"
    old_generation = active_storage_root(settings)
    journal_path = settings.storage_root / ".restore-journal.json"
    swapped = False
    try:
        _extract_media(archive_path, generation, preflight.media_checksums)
        tables = preflight.normalized_tables

        journal = {
            "restore_id": restore_id,
            "old_target": str(old_generation),
            "new_target": str(generation),
            "state": "prepared",
        }
        atomic_write_json(journal_path, journal)

        for transient_model in (UserSession, BackupRestoreJob, ImageGenerationJob):
            db.execute(delete(transient_model))
        for backup_model in DELETE_ORDER:
            db.execute(delete(backup_model))
        db.flush()

        for backup_model in INSERT_ORDER:
            model_table = cast(Table, backup_model.__table__)
            model_name = table_name(backup_model)
            rows = tables.get(model_name, [])
            if not isinstance(rows, list):
                raise InvalidBackup(f"Ungültige Tabelle: {model_name}")
            decoded = [decode_value(row) for row in rows]
            if model_name == "categories":
                decoded = _parent_first_categories(decoded)
            if decoded:
                db.execute(model_table.insert(), decoded)

        marker = AppSetting(key="last_restore", value={"restore_id": restore_id})
        db.merge(marker)
        restored_admin = db.scalar(
            select(User)
            .where(User.role == "admin", User.is_active.is_(True))
            .order_by(User.created_at, User.id)
        )
        if restored_admin is None:
            raise InvalidBackup("Der wiederhergestellte Bestand enthält keinen Administrator")
        db.add(
            BackupRestoreJob(
                id=uuid.UUID(restore_id),
                requested_by_user_id=restored_admin.id,
                operation="restore",
                status="completed",
                progress=100,
                current_stage="Wiederherstellung vollständig abgeschlossen",
                summary_json=preflight.model_dump(mode="json"),
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        db.add(
            AuditLog(
                actor_user_id=restored_admin.id,
                action="restore.completed",
                target_type="server",
                target_id=restore_id,
                details={"safety_backup": safety_path.name},
            )
        )
        db.flush()
        if db.get_bind().dialect.name == "postgresql":
            # Force all deferrable relational constraints before the media
            # generation is made visible. Non-deferrable constraints have
            # already been checked by the inserts/flush above.
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            db.flush()

        swap_active_generation(generation, settings=settings)
        swapped = True
        journal["state"] = "swapped"
        atomic_write_json(journal_path, journal)
    except Exception:
        db.rollback()
        if swapped:
            swap_active_generation(old_generation, settings=settings)
        shutil.rmtree(generation, ignore_errors=True)
        journal_path.unlink(missing_ok=True)
        raise

    # The commit result is a one-way boundary. Once commit() has been attempted,
    # neither this process nor cleanup may infer failure from an exception and
    # destroy/switch away from the new generation: a disconnected client can
    # receive an error after PostgreSQL durably committed it. Crash recovery
    # decides by reading the transaction's last_restore marker.
    try:
        db.commit()
    except Exception:
        with suppress(Exception):
            db.rollback()
        recover_interrupted_restore(settings)
        raise

    journal["state"] = "committed"
    try:
        atomic_write_json(journal_path, journal)
        journal_path.unlink()
    except OSError:
        # A late fsync/unlink failure is housekeeping, not a failed restore. The
        # durable marker lets startup/another worker finish this idempotently.
        logger.exception("Restore-Journal bleibt nach erfolgreichem Commit zur Recovery erhalten")
    return {
        "status": "completed",
        "restore_id": restore_id,
        "safety_backup": safety_path.name,
        "summary": preflight.model_dump(mode="json"),
    }
