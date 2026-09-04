from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import ImportCandidate, ImportJob, MediaAsset, RecipeImage, RecipeOriginalAsset
from app.services.storage import InvalidUpload, resolve_storage_key

logger = logging.getLogger(__name__)

# One transaction-scoped PostgreSQL advisory lock serializes every quota check and
# the MediaAsset row flushed immediately after it. This avoids check-then-insert
# races across the API and worker processes without adding a mutable quota ledger.
MEDIA_QUOTA_LOCK_ID = 7_218_661_931_337


class MediaQuotaExceeded(InvalidUpload):
    pass


@dataclass(frozen=True)
class MediaUsage:
    count: int
    byte_size: int

    def plus(self, *, count: int, byte_size: int) -> MediaUsage:
        return MediaUsage(self.count + count, self.byte_size + byte_size)


def _dialect_name(db: Session) -> str | None:
    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        return None  # Lightweight unit-test stand-ins do not have an engine.
    return str(get_bind().dialect.name)


def _acquire_quota_lock(db: Session) -> None:
    dialect = _dialect_name(db)
    if dialect == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": MEDIA_QUOTA_LOCK_ID},
        )
    elif dialect not in {None, "sqlite"}:
        raise RuntimeError("Medienquoten benötigen PostgreSQL-Transaktionssperren")


def _usage(db: Session, *, user_id: uuid.UUID | None = None) -> MediaUsage:
    statement = select(
        func.count(MediaAsset.id),
        func.coalesce(func.sum(MediaAsset.byte_size), 0),
    )
    if user_id is not None:
        statement = statement.where(MediaAsset.uploaded_by_user_id == user_id)
    count, byte_size = db.execute(statement).one()
    return MediaUsage(int(count), int(byte_size))


def _linked_usage(db: Session, recipe_id: uuid.UUID) -> MediaUsage:
    usages: list[tuple[int, int]] = []
    joins = (
        (
            RecipeImage,
            MediaAsset.id == RecipeImage.media_asset_id,
            RecipeImage.recipe_id == recipe_id,
        ),
        (
            RecipeImage,
            MediaAsset.id == RecipeImage.thumbnail_asset_id,
            RecipeImage.recipe_id == recipe_id,
        ),
        (
            RecipeOriginalAsset,
            MediaAsset.id == RecipeOriginalAsset.media_asset_id,
            RecipeOriginalAsset.recipe_id == recipe_id,
        ),
    )
    for model, join_condition, filter_condition in joins:
        count, byte_size = db.execute(
            select(
                func.count(MediaAsset.id),
                func.coalesce(func.sum(MediaAsset.byte_size), 0),
            )
            .select_from(MediaAsset)
            .join(model, join_condition)
            .where(filter_condition)
        ).one()
        usages.append((int(count), int(byte_size)))
    return MediaUsage(
        count=sum(item[0] for item in usages),
        byte_size=sum(item[1] for item in usages),
    )


def _check_limit(
    scope: str,
    current: MediaUsage,
    *,
    added_count: int,
    added_bytes: int,
    max_count: int,
    max_bytes: int,
) -> None:
    projected = current.plus(count=added_count, byte_size=added_bytes)
    if projected.count > max_count or projected.byte_size > max_bytes:
        raise MediaQuotaExceeded(
            f"Das {scope}-Medienlimit ist erreicht "
            f"({max_count} Dateien / {max_bytes // (1024 * 1024)} MB)."
        )


def enforce_new_asset_quota(
    db: Session,
    *,
    user_id: uuid.UUID,
    byte_size: int,
    settings: Settings | None = None,
) -> None:
    """Reserve room for one asset until the surrounding transaction completes.

    The caller must flush the new MediaAsset before doing any other quota check
    and commit or roll back the same transaction that holds the advisory lock.
    """

    settings = settings or get_settings()
    if _dialect_name(db) is None:
        return
    _acquire_quota_lock(db)
    _check_limit(
        "serverweite",
        _usage(db),
        added_count=1,
        added_bytes=byte_size,
        max_count=settings.media_global_max_count,
        max_bytes=settings.media_global_max_bytes,
    )
    _check_limit(
        "persönliche",
        _usage(db, user_id=user_id),
        added_count=1,
        added_bytes=byte_size,
        max_count=settings.media_user_max_count,
        max_bytes=settings.media_user_max_bytes,
    )


def enforce_recipe_quota(
    db: Session,
    recipe_id: uuid.UUID,
    assets: Iterable[MediaAsset | None],
    *,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    if _dialect_name(db) is None:
        return
    unique = {asset.id: asset for asset in assets if asset is not None}
    if not unique:
        return
    _acquire_quota_lock(db)
    _check_limit(
        "Rezept",
        _linked_usage(db, recipe_id),
        added_count=len(unique),
        added_bytes=sum(asset.byte_size for asset in unique.values()),
        max_count=settings.media_recipe_max_count,
        max_bytes=settings.media_recipe_max_bytes,
    )


def _asset_is_referenced(db: Session, asset_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    image_reference = db.scalar(
        select(RecipeImage.id).where(
            (RecipeImage.media_asset_id == asset_id) | (RecipeImage.thumbnail_asset_id == asset_id)
        )
    )
    original_reference = db.scalar(
        select(RecipeOriginalAsset.media_asset_id).where(
            RecipeOriginalAsset.media_asset_id == asset_id
        )
    )
    candidate_reference = db.scalar(
        select(ImportCandidate.id).where(
            (ImportCandidate.image_asset_id == asset_id)
            | (ImportCandidate.thumbnail_asset_id == asset_id)
        )
    )
    other_job_reference = db.scalar(
        select(ImportJob.id).where(
            ImportJob.source_asset_id == asset_id,
            ImportJob.id != job_id,
        )
    )
    return bool(image_reference or original_reference or candidate_reference or other_job_reference)


def cleanup_terminal_import_sources(
    settings: Settings | None = None,
    *,
    limit: int = 100,
) -> int:
    """Delete expired terminal sources that have no remaining recipe reference."""

    from app.database import SessionLocal

    settings = settings or get_settings()
    cutoff = datetime.now(UTC) - timedelta(hours=settings.import_source_retention_hours)
    paths: list[Path] = []
    removed = 0
    with SessionLocal() as db:
        _acquire_quota_lock(db)
        statement = (
            select(ImportJob)
            .where(
                ImportJob.status.in_({"completed", "failed", "cancelled"}),
                ImportJob.finished_at.is_not(None),
                ImportJob.finished_at < cutoff,
                ImportJob.source_asset_id.is_not(None),
            )
            .order_by(ImportJob.finished_at, ImportJob.id)
            .limit(limit)
        )
        if _dialect_name(db) == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        jobs = list(db.scalars(statement))
        try:
            for job in jobs:
                if job.source_asset_id is None or _asset_is_referenced(
                    db, job.source_asset_id, job.id
                ):
                    continue
                asset = db.get(MediaAsset, job.source_asset_id)
                if asset is None:
                    job.source_asset_id = None
                    continue
                try:
                    paths.append(resolve_storage_key(asset.storage_key, settings))
                except InvalidUpload:
                    logger.warning("Ungültiger Speicherschlüssel beim Import-Cleanup: %s", asset.id)
                job.source_asset_id = None
                db.flush()
                db.delete(asset)
                removed += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Abgelaufene Importquelle konnte nicht entfernt werden: %s", path)
    return removed
