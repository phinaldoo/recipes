from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import __version__
from app.backups.schemas import DATABASE_SCHEMA_VERSION, BackupManifest
from app.config import Settings, get_settings
from app.models import (
    AppSetting,
    AuditLog,
    Category,
    Favorite,
    ImportBatch,
    ImportCandidate,
    ImportJob,
    Ingredient,
    IngredientGroup,
    InstructionStep,
    MediaAsset,
    Recipe,
    RecipeCategory,
    RecipeComment,
    RecipeImage,
    RecipeNutrition,
    RecipeOriginalAsset,
    RecipeShare,
    RecipeSource,
    RecipeTag,
    RecipeVersion,
    SearchSynonym,
    Tag,
    User,
    UserNote,
)
from app.services.storage import active_storage_root

BACKUP_MODELS = (
    User,
    UserNote,
    Category,
    Tag,
    SearchSynonym,
    Recipe,
    RecipeSource,
    RecipeNutrition,
    IngredientGroup,
    Ingredient,
    InstructionStep,
    RecipeCategory,
    RecipeTag,
    MediaAsset,
    RecipeImage,
    RecipeOriginalAsset,
    RecipeComment,
    RecipeVersion,
    RecipeShare,
    AppSetting,
    Favorite,
    ImportBatch,
    ImportJob,
    ImportCandidate,
    AuditLog,
)


def table_name(model: type[Any]) -> str:
    return str(cast(Any, model).__tablename__)


def encode_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.astimezone(UTC).isoformat()}
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def _included_media_ids(db: Session) -> set[uuid.UUID]:
    identifiers: set[uuid.UUID] = set()
    identifiers.update(db.scalars(select(RecipeImage.media_asset_id)))
    identifiers.update(
        identifier
        for identifier in db.scalars(select(RecipeImage.thumbnail_asset_id))
        if identifier is not None
    )
    identifiers.update(db.scalars(select(RecipeOriginalAsset.media_asset_id)))
    terminal_batches = select(ImportBatch.id).where(
        ImportBatch.status.in_(["review", "completed", "completed_with_errors"])
    )
    identifiers.update(
        identifier
        for identifier in db.scalars(
            select(ImportJob.source_asset_id).where(
                ImportJob.batch_id.in_(terminal_batches),
                ImportJob.status.in_(["review", "completed", "failed", "cancelled"]),
                ImportJob.source_asset_id.is_not(None),
            )
        )
        if identifier is not None
    )
    terminal_jobs = select(ImportJob.id).where(
        ImportJob.batch_id.in_(terminal_batches),
        ImportJob.status.in_(["review", "completed", "failed", "cancelled"]),
    )
    identifiers.update(
        identifier
        for identifier in db.scalars(
            select(ImportCandidate.image_asset_id).where(
                ImportCandidate.job_id.in_(terminal_jobs),
                ImportCandidate.image_asset_id.is_not(None),
            )
        )
        if identifier is not None
    )
    identifiers.update(
        identifier
        for identifier in db.scalars(
            select(ImportCandidate.thumbnail_asset_id).where(
                ImportCandidate.job_id.in_(terminal_jobs),
                ImportCandidate.thumbnail_asset_id.is_not(None),
            )
        )
        if identifier is not None
    )
    return identifiers


def _table_rows(
    db: Session,
    model: type[Any],
    *,
    included_media_ids: set[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    table = model.__table__
    query = select(table)
    if model is ImportBatch:
        query = query.where(table.c.status.in_(["review", "completed", "completed_with_errors"]))
    elif model is ImportJob:
        terminal_batches = select(ImportBatch.id).where(
            ImportBatch.status.in_(["review", "completed", "completed_with_errors"])
        )
        query = query.where(
            table.c.batch_id.in_(terminal_batches),
            table.c.status.in_(["review", "completed", "failed", "cancelled"]),
        )
    elif model is ImportCandidate:
        terminal_batches = select(ImportBatch.id).where(
            ImportBatch.status.in_(["review", "completed", "completed_with_errors"])
        )
        terminal_jobs = select(ImportJob.id).where(
            ImportJob.batch_id.in_(terminal_batches),
            ImportJob.status.in_(["review", "completed", "failed", "cancelled"]),
        )
        query = query.where(table.c.job_id.in_(terminal_jobs))
    elif model is MediaAsset:
        query = query.where(table.c.id.in_(included_media_ids or set()))
    rows = db.execute(query).mappings().all()
    if model is Category:
        pending = {row["id"]: row for row in rows}
        ordered = []
        emitted: set[uuid.UUID] = set()
        while pending:
            ready = [
                row
                for row in pending.values()
                if row["parent_id"] is None or row["parent_id"] in emitted
            ]
            if not ready:
                raise RuntimeError("Der Kategoriebaum enthält einen Zyklus oder fehlende Eltern")
            ready.sort(key=lambda row: (row["position"], row["name"].casefold(), str(row["id"])))
            for row in ready:
                ordered.append(row)
                emitted.add(row["id"])
                pending.pop(row["id"])
        rows = ordered
    result = []
    for row in rows:
        data = {key: encode_value(value) for key, value in row.items()}
        if table.name == "app_settings" and data.get("key") == "last_restore":
            continue
        result.append(data)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def export_backup(
    db: Session,
    *,
    destination: Path | None = None,
    settings: Settings | None = None,
) -> tuple[Path, BackupManifest, str]:
    settings = settings or get_settings()
    settings.backup_temp_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    destination = destination or (
        settings.backup_temp_root
        / f"rezeptserver-backup-{timestamp.strftime('%Y-%m-%d-%H%M%S')}.zip"
    )
    partial = destination.with_suffix(".zip.partial")

    # A repeatable-read transaction keeps all relational rows mutually consistent.
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    included_media_ids = _included_media_ids(db)
    tables = {
        table_name(model): _table_rows(
            db,
            model,
            included_media_ids=included_media_ids,
        )
        for model in BACKUP_MODELS
    }
    application_data = {
        "format": "rezeptverwaltung-application-data",
        "version": "1.0",
        "tables": tables,
    }
    application_bytes = json.dumps(
        application_data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    storage_root = active_storage_root(settings)
    assets = list(
        db.scalars(
            select(MediaAsset)
            .where(MediaAsset.id.in_(included_media_ids))
            .order_by(MediaAsset.storage_key)
        )
    )
    media_total = 0
    media_files: list[tuple[Path, str, str]] = []
    for asset in assets:
        path = (storage_root / asset.storage_key).resolve()
        if storage_root.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"Referenzierte Mediendatei fehlt: {asset.storage_key}")
        digest = _sha256_file(path)
        if digest != asset.sha256:
            raise RuntimeError(f"Prüfsumme stimmt nicht: {asset.storage_key}")
        archive_name = f"media/{asset.storage_key}"
        media_files.append((path, archive_name, digest))
        media_total += path.stat().st_size

    estimated_uncompressed = len(application_bytes) + media_total + 16 * 1024 * 1024
    reserve = max(64 * 1024 * 1024, estimated_uncompressed // 10)
    if shutil.disk_usage(destination.parent).free < estimated_uncompressed + reserve:
        raise RuntimeError(
            "Für ein vollständig prüfbares Backup ist nicht genügend Speicherplatz frei"
        )

    counts = {name: len(rows) for name, rows in tables.items()}
    manifest = BackupManifest(
        application_version=__version__,
        database_schema_version=DATABASE_SCHEMA_VERSION,
        created_at=timestamp,
        counts=counts,
        media_file_count=len(media_files),
        media_total_bytes=media_total,
        archive_contents=["manifest.json", "application-data.json", "checksums.sha256", "media/"],
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    checksums = {
        "manifest.json": hashlib.sha256(manifest_bytes).hexdigest(),
        "application-data.json": hashlib.sha256(application_bytes).hexdigest(),
    }
    checksums.update({name: digest for _, name, digest in media_files})
    checksum_bytes = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(checksums.items())
    ).encode("utf-8")

    try:
        with zipfile.ZipFile(
            partial, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("application-data.json", application_bytes)
            archive.writestr("checksums.sha256", checksum_bytes)
            for path, name, _ in media_files:
                archive.write(path, name)
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, destination)
        _fsync_directory(destination.parent)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    try:
        # Re-open the final inode and run the same verifier used for restore.
        # A completed job therefore never advertises a truncated or internally
        # inconsistent archive, even after a late storage error.
        from app.backups.preflight import preflight_backup

        verified = preflight_backup(destination, settings)
        if (
            verified.counts != manifest.counts
            or verified.media_file_count != manifest.media_file_count
            or verified.media_total_bytes != manifest.media_total_bytes
        ):
            raise RuntimeError(
                "Die abschließende Backup-Prüfung stimmt nicht mit dem Manifest überein"
            )
        archive_digest = _sha256_file(destination)
        return destination, manifest, archive_digest
    except Exception:
        destination.unlink(missing_ok=True)
        raise
