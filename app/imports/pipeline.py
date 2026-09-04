from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from redis import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.ai import (
    AIExtractionError,
    AIImageError,
    AIUnavailable,
    detect_recipes,
    extract_recipe,
    maybe_generate_recipe_image,
    verify_recipe_image,
)
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.i18n import (
    DEFAULT_LOCALE,
    Locale,
    normalize_locale,
    translate,
    translate_known_text,
)
from app.imports.source_media import (
    SourceRegionError,
    bounding_box_iou,
    bounding_box_overlap_ratio,
    crop_source_region,
    normalize_image_source,
    source_page_count,
)
from app.maintenance import database_maintenance_guard
from app.models import (
    Category,
    ImportBatch,
    ImportCandidate,
    ImportJob,
    MediaAsset,
    Recipe,
    RecipeImage,
    RecipeOriginalAsset,
    User,
)
from app.schemas.ai import (
    DetectedRecipe,
    ExtractedRecipe,
    RecipeImageCandidate,
    RecipeSourceRegion,
)
from app.schemas.recipe import CategoryPathInput, RecipeInput
from app.services.media import create_asset, create_thumbnail_asset
from app.services.media_quota import enforce_recipe_quota
from app.services.recipes import create_recipe
from app.services.storage import InvalidUpload, resolve_storage_key, store_bytes

logger = logging.getLogger(__name__)

STAGES = {
    "preparing": (10, "job.file_preparing"),
    "extracting": (35, "job.extracting"),
    "checking_images": (62, "job.checking_images"),
    "generating_image": (74, "job.generating_image"),
    "validating": (88, "job.validating"),
}
IMAGE_MATCH_THRESHOLD = 0.75
IMAGE_DETECTION_THRESHOLD = 0.55
IMAGE_DUPLICATE_IOU_THRESHOLD = 0.65
MAX_IMAGE_CANDIDATES_PER_RECIPE = 3
ACTIVE_STATUSES = (
    "preparing",
    "extracting",
    "checking_images",
    "generating_image",
    "validating",
)


class ImportLeaseLost(RuntimeError):
    pass


class ImportMaintenance(RuntimeError):
    pass


class URLRenderError(RuntimeError):
    pass


class ImportSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedImageOption:
    recipe_index: int
    image_candidate: RecipeImageCandidate
    image: bytes
    verification_confidence: float
    verification_reason: str


@dataclass
class PreparedCandidate:
    candidate_id: uuid.UUID
    detected: DetectedRecipe
    extracted: ExtractedRecipe
    warnings: list[str]


def _remove_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Temporäre Importdatei konnte nicht entfernt werden: %s", path)


def _lease_duration() -> timedelta:
    settings = get_settings()
    seconds = max(30 * 60, settings.ai_timeout_seconds * (settings.ai_max_retries + 1) + 300)
    return timedelta(seconds=seconds)


def _maintenance_check() -> None:
    settings = get_settings()
    try:
        with Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1) as redis:
            if redis.get("maintenance:restore") or redis.get("maintenance:backup"):
                raise ImportMaintenance("Ein Wartungsauftrag läuft; der Import wartet")
    except ImportMaintenance:
        raise
    except Exception as exc:
        raise ImportMaintenance(
            "Der Wartungsstatus ist nicht erreichbar; der Import wartet vorsorglich"
        ) from exc


def _claim_job(db: Session, job_id: uuid.UUID, lease_token: str) -> ImportJob | None:
    now = datetime.now(UTC)
    claimed = db.scalar(
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == "queued")
        .values(
            status="preparing",
            progress=10,
            current_stage="job.file_preparing",
            started_at=now,
            finished_at=None,
            attempt_count=ImportJob.attempt_count + 1,
            lease_token=lease_token,
            lease_expires_at=now + _lease_duration(),
        )
        .returning(ImportJob.id)
    )
    db.commit()
    return db.get(ImportJob, claimed) if claimed else None


def _stage(db: Session, job: ImportJob, status: str, lease_token: str) -> None:
    _maintenance_check()
    target_language = (
        normalize_locale(getattr(getattr(job, "batch", None), "target_language", None))
        or DEFAULT_LOCALE
    )
    result = db.execute(
        update(ImportJob)
        .where(ImportJob.id == job.id, ImportJob.lease_token == lease_token)
        .values(
            status=status,
            progress=STAGES[status][0],
            current_stage=translate(target_language, STAGES[status][1]),
            lease_expires_at=datetime.now(UTC) + _lease_duration(),
        )
    )
    cursor_result = result if isinstance(result, CursorResult) else None
    if cursor_result is None or cursor_result.rowcount != 1:
        db.rollback()
        raise ImportLeaseLost("Der Importauftrag wurde von einem anderen Worker übernommen")
    db.commit()


def _progress(
    db: Session,
    job_id: uuid.UUID,
    lease_token: str,
    *,
    status: str,
    progress: int,
    label: str,
) -> None:
    _maintenance_check()
    result = db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.lease_token == lease_token)
        .values(
            status=status,
            progress=max(0, min(progress, 99)),
            current_stage=label,
            lease_expires_at=datetime.now(UTC) + _lease_duration(),
        )
    )
    cursor_result = result if isinstance(result, CursorResult) else None
    if cursor_result is None or cursor_result.rowcount != 1:
        db.rollback()
        raise ImportLeaseLost("Der Importauftrag wurde von einem anderen Worker übernommen")
    db.commit()


def recompute_batch(db: Session, batch_id: uuid.UUID) -> None:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        return
    jobs = list(db.scalars(select(ImportJob).where(ImportJob.batch_id == batch_id)))
    batch.completed_jobs = sum(job.status == "completed" for job in jobs)
    batch.failed_jobs = sum(job.status == "failed" for job in jobs)
    if any(job.status == "review" for job in jobs) and all(
        job.status in {"review", "completed", "failed", "cancelled"} for job in jobs
    ):
        batch.status = "review"
    elif all(job.status in {"completed", "failed", "cancelled"} for job in jobs):
        batch.status = "completed" if batch.failed_jobs == 0 else "completed_with_errors"
    else:
        batch.status = "processing"


def requeue_stale_imports() -> list[uuid.UUID]:
    now = datetime.now(UTC)
    with database_maintenance_guard(), SessionLocal() as db:
        identifiers = list(
            db.scalars(
                update(ImportJob)
                .where(
                    ImportJob.status.in_(ACTIVE_STATUSES),
                    ImportJob.lease_expires_at < now,
                )
                .values(
                    status="queued",
                    progress=0,
                    current_stage="job.worker_resume",
                    lease_token=None,
                    lease_expires_at=None,
                )
                .returning(ImportJob.id)
            )
        )
        db.commit()
        return identifiers


def _category_paths(db: Session) -> list[str]:
    categories = list(db.scalars(select(Category)))
    return [category.path for category in categories]


def _render_url(url: str) -> bytes:
    settings = get_settings()
    try:
        response = httpx.post(
            f"{settings.renderer_url.rstrip('/')}/render/pdf",
            headers={"Authorization": f"Bearer {settings.renderer_token}"},
            json={"url": url},
            timeout=120,
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise URLRenderError("Die Webseite hat nicht rechtzeitig geladen.") from exc
    except httpx.RequestError as exc:
        raise URLRenderError(
            "Der sichere Webseiten-Renderer ist momentan nicht erreichbar."
        ) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 413:
            message = "Die gerenderte Webseite ist zu groß."
        elif status_code == 422:
            detail = None
            try:
                payload = exc.response.json()
                if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                    detail = payload["detail"].strip()
            except ValueError:
                pass
            message = "Die Webseite wurde vom sicheren Renderer abgelehnt."
            if detail:
                message = f"{message} {detail}"
        elif status_code == 504:
            message = "Die Webseite hat nicht rechtzeitig geladen."
        else:
            message = "Der sichere Webseiten-Renderer konnte die Webseite nicht verarbeiten."
        raise URLRenderError(message) from exc
    if not response.content.startswith(b"%PDF-"):
        raise URLRenderError(
            "Der sichere Webseiten-Renderer hat keine gültige PDF-Datei geliefert."
        )
    return response.content


def _source_fallback(job: ImportJob, extracted_title: str | None) -> tuple[str | None, str | None]:
    source_url = job.source_url
    source_title = extracted_title
    if job.input_type == "url" and not source_title and source_url:
        source_title = urlsplit(source_url).hostname
    return source_title, source_url


def _prepare_detection_source(content: bytes, mime_type: str) -> tuple[bytes, str]:
    if mime_type == "application/pdf":
        return content, mime_type
    return normalize_image_source(content), "image/png"


def _sanitize_detections(
    recipes: list[DetectedRecipe],
    *,
    page_count: int,
    target_language: Locale = DEFAULT_LOCALE,
) -> list[DetectedRecipe]:
    """Drop impossible regions and obvious duplicates within each detected recipe."""

    sanitized: list[DetectedRecipe] = []
    for recipe in recipes:
        warnings = list(recipe.warnings)
        source_regions = [region for region in recipe.source_regions if region.page <= page_count]
        if len(source_regions) != len(recipe.source_regions):
            warnings.append(translate(target_language, "import.warning.region_outside"))
        if not source_regions:
            continue
        image_candidates: list[RecipeImageCandidate] = []
        for candidate in sorted(
            recipe.recipe_image_candidates,
            key=lambda item: item.confidence,
            reverse=True,
        ):
            if candidate.page > page_count or candidate.confidence < IMAGE_DETECTION_THRESHOLD:
                continue
            if any(_same_image_region(candidate, existing) for existing in image_candidates):
                continue
            image_candidates.append(candidate)
            if len(image_candidates) == MAX_IMAGE_CANDIDATES_PER_RECIPE:
                break
        sanitized.append(
            recipe.model_copy(
                update={
                    "source_regions": source_regions,
                    "recipe_image_candidates": image_candidates,
                    "warnings": warnings,
                }
            )
        )
    return sanitized


def _same_image_region(first: RecipeImageCandidate, second: RecipeImageCandidate) -> bool:
    if first.page != second.page:
        return False
    return (
        bounding_box_iou(first.bounding_box, second.bounding_box) >= IMAGE_DUPLICATE_IOU_THRESHOLD
        or bounding_box_overlap_ratio(first.bounding_box, second.bounding_box) >= 0.85
    )


def _recipe_input(job: ImportJob, extracted: ExtractedRecipe) -> RecipeInput:
    value = extracted
    source_title, fallback_source_url = _source_fallback(job, value.source_title)
    source_url = str(value.source_url) if value.source_url else fallback_source_url
    return RecipeInput(
        title=value.title,
        description=value.description,
        recipe_kind=value.recipe_kind,
        base_servings=value.base_servings,
        serving_label=value.serving_label,
        prep_time_minutes=value.prep_time_minutes,
        cook_time_minutes=value.cook_time_minutes,
        rest_time_minutes=value.rest_time_minutes,
        total_time_minutes=value.total_time_minutes,
        total_time_is_manual=value.total_time_minutes is not None,
        nutrition=value.nutrition,
        notes=value.notes,
        status="active",
        ingredient_groups=value.ingredient_groups,
        instruction_steps=value.instruction_steps,
        categories=[
            CategoryPathInput(path=item.path, origin="ai_import")
            for item in value.category_suggestions
        ],
        source={"title": source_title, "url": source_url} if source_title or source_url else None,
    )


def _verified_image_options(
    *,
    recipe_index: int,
    extracted: ExtractedRecipe,
    detected: DetectedRecipe,
    content: bytes,
    mime_type: str,
    settings: Settings,
    target_language: Locale = DEFAULT_LOCALE,
) -> tuple[list[VerifiedImageOption], list[str]]:
    options: list[VerifiedImageOption] = []
    warnings: list[str] = []
    for image_candidate in detected.recipe_image_candidates:
        region = RecipeSourceRegion(
            page=image_candidate.page,
            bounding_box=image_candidate.bounding_box,
        )
        try:
            crop = crop_source_region(content, mime_type, region, max_dimension=2400)
            match = verify_recipe_image(
                extracted=extracted,
                image=crop,
                target_language=target_language,
                settings=settings,
            )
        except (AIExtractionError, AIUnavailable, SourceRegionError) as exc:
            warnings.append(
                translate(
                    target_language,
                    "import.warning.image_check_failed",
                    detail=translate_known_text(
                        target_language,
                        str(exc),
                        fallback_key="error.generic",
                    ),
                )
            )
            continue
        if match.matches_recipe and match.confidence >= IMAGE_MATCH_THRESHOLD:
            options.append(
                VerifiedImageOption(
                    recipe_index=recipe_index,
                    image_candidate=image_candidate,
                    image=crop,
                    verification_confidence=match.confidence,
                    verification_reason=match.reason,
                )
            )
    if detected.recipe_image_candidates and not options:
        warnings.append(translate(target_language, "import.warning.no_image_match"))
    return options, warnings


def _assign_verified_images(
    options: list[VerifiedImageOption],
) -> dict[int, VerifiedImageOption]:
    """Maximize unique recipe/image matches, then their semantic confidence."""

    if not options:
        return {}

    # Detection may describe the same physical image with slightly different
    # rectangles for different recipes. Collapse those rectangles into one
    # image slot before solving the document-wide assignment.
    parents = list(range(len(options)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first_index, first in enumerate(options):
        for second_index in range(first_index + 1, len(options)):
            if _same_image_region(first.image_candidate, options[second_index].image_candidate):
                union(first_index, second_index)

    group_by_root: dict[int, int] = {}
    option_groups: list[int] = []
    for option_index in range(len(options)):
        root = find(option_index)
        group = group_by_root.setdefault(root, len(group_by_root))
        option_groups.append(group)

    recipe_indices = sorted({option.recipe_index for option in options})
    recipe_rows = {recipe_index: row for row, recipe_index in enumerate(recipe_indices)}
    edge_options: dict[tuple[int, int], VerifiedImageOption] = {}
    for option, group in zip(options, option_groups, strict=True):
        edge = (recipe_rows[option.recipe_index], group)
        existing = edge_options.get(edge)
        if existing is None or _option_score(option) > _option_score(existing):
            edge_options[edge] = option

    size = max(len(recipe_indices), len(group_by_root))
    cardinality_bonus = 1_000_000
    weights = [[0] * size for _ in range(size)]
    for (row, group), option in edge_options.items():
        weights[row][group] = cardinality_bonus + _option_score(option)

    selected_columns = _maximum_weight_columns(weights)
    assignments: dict[int, VerifiedImageOption] = {}
    for row, recipe_index in enumerate(recipe_indices):
        selected_option = edge_options.get((row, selected_columns[row]))
        if selected_option is not None:
            assignments[recipe_index] = selected_option
    return assignments


def _option_score(option: VerifiedImageOption) -> int:
    return round(option.verification_confidence * 10_000) + round(
        option.image_candidate.confidence * 1_000
    )


def _maximum_weight_columns(weights: list[list[int]]) -> list[int]:
    """Return a maximum-weight square assignment using the Hungarian algorithm."""

    size = len(weights)
    if size == 0:
        return []
    maximum = max(max(row) for row in weights)
    costs = [[maximum - value for value in row] for row in weights]
    row_potentials = [0] * (size + 1)
    column_potentials = [0] * (size + 1)
    matched_row = [0] * (size + 1)
    previous_column = [0] * (size + 1)
    infinity = 10**18

    for row in range(1, size + 1):
        matched_row[0] = row
        column = 0
        minimums = [infinity] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column] = True
            active_row = matched_row[column]
            delta = infinity
            next_column = 0
            for candidate_column in range(1, size + 1):
                if used[candidate_column]:
                    continue
                reduced_cost = (
                    costs[active_row - 1][candidate_column - 1]
                    - row_potentials[active_row]
                    - column_potentials[candidate_column]
                )
                if reduced_cost < minimums[candidate_column]:
                    minimums[candidate_column] = reduced_cost
                    previous_column[candidate_column] = column
                if minimums[candidate_column] < delta:
                    delta = minimums[candidate_column]
                    next_column = candidate_column
            for candidate_column in range(size + 1):
                if used[candidate_column]:
                    row_potentials[matched_row[candidate_column]] += delta
                    column_potentials[candidate_column] -= delta
                else:
                    minimums[candidate_column] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            next_column = previous_column[column]
            matched_row[column] = matched_row[next_column]
            column = next_column
            if column == 0:
                break

    selected = [-1] * size
    for column in range(1, size + 1):
        if matched_row[column] != 0:
            selected[matched_row[column] - 1] = column - 1
    return selected


def _image_metadata(option: VerifiedImageOption) -> dict[str, object]:
    image_candidate = option.image_candidate
    return {
        "origin": "verified_import_crop",
        "page": image_candidate.page,
        "bounding_box": image_candidate.bounding_box.model_dump(mode="json"),
        "detection_confidence": image_candidate.confidence,
        "verification_confidence": option.verification_confidence,
        "verification_reason": option.verification_reason,
    }


def _store_candidate_image(
    db: Session,
    *,
    candidate: ImportCandidate,
    user: User,
    image: bytes,
    generated: bool,
    metadata: dict[str, object],
    cleanup_paths: list[Path],
) -> tuple[MediaAsset, MediaAsset | None]:
    stored = store_bytes(
        image,
        filename=f"import-{candidate.id}.png",
        kind="generated_image" if generated else "recipe_image",
    )
    cleanup_paths.append(resolve_storage_key(stored.storage_key))
    image_asset = create_asset(
        db,
        stored,
        user,
        "generated_image" if generated else "recipe_image",
    )
    thumbnail = create_thumbnail_asset(db, image_asset, user)
    if thumbnail:
        cleanup_paths.append(resolve_storage_key(thumbnail.storage_key))
    candidate.image_asset_id = image_asset.id
    candidate.thumbnail_asset_id = thumbnail.id if thumbnail else None
    candidate.image_metadata_json = metadata
    return image_asset, thumbnail


def _clear_candidate_rows(db: Session, job: ImportJob) -> list[Path]:
    paths: list[Path] = []
    assets: dict[uuid.UUID, MediaAsset] = {}
    candidates = list(getattr(job, "candidates", []))
    for candidate in candidates:
        for asset in (candidate.image_asset, candidate.thumbnail_asset):
            if asset is not None:
                assets[asset.id] = asset
                paths.append(resolve_storage_key(asset.storage_key))
        candidate.image_asset_id = None
        candidate.thumbnail_asset_id = None
    if candidates:
        db.flush()
        db.execute(delete(ImportCandidate).where(ImportCandidate.job_id == job.id))
        for asset in assets.values():
            db.delete(asset)
        db.flush()
    return paths


def _promote_candidate(
    db: Session,
    *,
    candidate: ImportCandidate,
    user: User,
    source_asset: MediaAsset | None,
    image_asset: MediaAsset | None,
    thumbnail_asset: MediaAsset | None,
    target_language: Locale = DEFAULT_LOCALE,
) -> Recipe:
    if candidate.recipe_payload is None:
        raise ImportSelectionError(f"‚{candidate.title}‘ enthält keine Rezeptdaten")
    if source_asset is None:
        raise ImportSelectionError(
            f"Die Originaldatei für ‚{candidate.title}‘ ist nicht mehr verfügbar"
        )

    payload = RecipeInput.model_validate(candidate.recipe_payload)
    recipe = create_recipe(db, payload, user)
    enforce_recipe_quota(
        db,
        recipe.id,
        [source_asset, image_asset, thumbnail_asset],
    )
    recipe.original_assets.append(RecipeOriginalAsset(media_asset_id=source_asset.id, position=0))
    if image_asset is not None:
        origin = (candidate.image_metadata_json or {}).get("origin")
        recipe.images.append(
            RecipeImage(
                media_asset_id=image_asset.id,
                thumbnail_asset_id=thumbnail_asset.id if thumbnail_asset else None,
                position=0,
                is_cover=True,
                alt_text=(
                    translate(target_language, "ai.image.prepared_alt", title=recipe.title)
                    if origin == "ai_prepared_import"
                    else translate(target_language, "ai.image.imported_alt", title=recipe.title)
                ),
                generation_metadata=candidate.image_metadata_json,
            )
        )
        candidate.image_asset_id = None
        candidate.thumbnail_asset_id = None
    candidate.status = "imported"
    candidate.result_recipe_id = recipe.id
    candidate.finished_at = datetime.now(UTC)
    db.flush()
    return recipe


def import_selected_candidates(
    db: Session,
    *,
    batch: ImportBatch,
    selected_ids: set[uuid.UUID],
    user: User,
) -> tuple[list[Recipe], list[Path]]:
    if batch.status != "review":
        raise ImportSelectionError("Dieser Import ist nicht mehr zur Auswahl bereit")
    candidates = list(
        db.scalars(
            select(ImportCandidate)
            .join(ImportJob)
            .where(ImportJob.batch_id == batch.id)
            .order_by(ImportJob.created_at, ImportCandidate.position)
            .with_for_update()
        )
    )
    ready_ids = {candidate.id for candidate in candidates if candidate.status == "ready"}
    if not selected_ids <= ready_ids:
        raise ImportSelectionError("Mindestens ein ausgewähltes Rezept ist nicht mehr importierbar")
    if any(candidate.status == "processing" for candidate in candidates):
        raise ImportSelectionError("Die Rezepterkennung ist noch nicht abgeschlossen")

    recipes: list[Recipe] = []
    cleanup_paths: list[Path] = []
    first_recipe_by_job: dict[uuid.UUID, uuid.UUID] = {}
    for candidate in candidates:
        if candidate.status != "ready":
            continue
        if candidate.id not in selected_ids:
            removable = [
                asset
                for asset in (candidate.image_asset, candidate.thumbnail_asset)
                if asset is not None
            ]
            cleanup_paths.extend(resolve_storage_key(asset.storage_key) for asset in removable)
            candidate.image_asset_id = None
            candidate.thumbnail_asset_id = None
            candidate.status = "discarded"
            candidate.finished_at = datetime.now(UTC)
            db.flush()
            for asset in removable:
                db.delete(asset)
            continue

        recipe = _promote_candidate(
            db,
            candidate=candidate,
            user=user,
            source_asset=candidate.job.source_asset,
            image_asset=candidate.image_asset,
            thumbnail_asset=candidate.thumbnail_asset,
            target_language=normalize_locale(batch.target_language) or DEFAULT_LOCALE,
        )
        first_recipe_by_job.setdefault(candidate.job_id, recipe.id)
        recipes.append(recipe)

    for job in batch.jobs:
        if job.status != "review":
            continue
        imported_count = sum(
            candidate.status == "imported" for candidate in candidates if candidate.job_id == job.id
        )
        job.status = "completed"
        job.progress = 100
        target_language = normalize_locale(batch.target_language) or DEFAULT_LOCALE
        job.current_stage = (
            translate(target_language, "job.selection_empty")
            if imported_count == 0
            else translate(target_language, "job.selection.one")
            if imported_count == 1
            else translate(target_language, "job.selection.other", count=imported_count)
        )
        job.result_recipe_id = first_recipe_by_job.get(job.id)
        job.finished_at = datetime.now(UTC)
    recompute_batch(db, batch.id)
    db.flush()
    return recipes, cleanup_paths


def _process_import_job(job_id: uuid.UUID) -> None:
    settings = get_settings()
    lease_token = uuid.uuid4().hex
    with SessionLocal() as db:
        _maintenance_check()
        job = _claim_job(db, job_id, lease_token)
        if job is None:
            return
        batch_id = job.batch_id
        target_language = normalize_locale(job.batch.target_language) or DEFAULT_LOCALE
        cleanup_paths = []
        completed = False
        try:
            _stage(db, job, "preparing", lease_token)
            asset = job.source_asset
            if job.input_type == "url":
                if not job.source_url:
                    raise ValueError("Die Webadresse fehlt")
                pdf = _render_url(job.source_url)
                stored = store_bytes(
                    pdf,
                    filename="webseite.pdf",
                    kind="url_snapshot_pdf",
                    settings=settings,
                )
                source_file = resolve_storage_key(stored.storage_key)
                cleanup_paths.append(source_file)
                user = db.get(User, job.batch.created_by_user_id)
                if user is None:
                    raise ValueError("Das Benutzerkonto ist nicht mehr verfügbar")
                asset = create_asset(db, stored, user, "url_snapshot_pdf")
                job.source_asset_id = asset.id
                db.commit()
                cleanup_paths.remove(source_file)
            if asset is None:
                raise ValueError("Die Quelldatei fehlt")
            source_path = resolve_storage_key(asset.storage_key)
            content = source_path.read_bytes()
            user = db.get(User, job.batch.created_by_user_id)
            if user is None:
                raise ValueError("Das Benutzerkonto ist nicht mehr verfügbar")

            old_candidate_paths = _clear_candidate_rows(db, job)
            db.commit()
            _remove_paths(old_candidate_paths)

            detection_content, detection_mime = _prepare_detection_source(content, asset.mime_type)
            _progress(
                db,
                job.id,
                lease_token,
                status="extracting",
                progress=25,
                label=translate(target_language, "job.detecting"),
            )
            detected_document = detect_recipes(
                content=detection_content,
                mime_type=detection_mime,
                target_language=target_language,
                settings=settings,
            )
            detections = _sanitize_detections(
                detected_document.recipes,
                page_count=source_page_count(detection_content, detection_mime),
                target_language=target_language,
            )
            if not detections:
                raise AIExtractionError("Im Material wurde kein vollständiges Rezept erkannt")

            candidates: list[ImportCandidate] = []
            for position, detected in enumerate(detections):
                candidate = ImportCandidate(
                    job_id=job.id,
                    position=position,
                    status="processing",
                    title=detected.title_hint,
                    source_regions_json=[
                        region.model_dump(mode="json") for region in detected.source_regions
                    ],
                    warnings_json=[*detected_document.warnings, *detected.warnings],
                    confidence=detected.detection_confidence,
                )
                db.add(candidate)
                candidates.append(candidate)
            db.commit()

            prepared: list[PreparedCandidate] = []
            category_paths = _category_paths(db)
            for index, (candidate, detected) in enumerate(
                zip(candidates, detections, strict=True), start=1
            ):
                try:
                    progress = 30 + int((index - 1) / len(candidates) * 30)
                    _progress(
                        db,
                        job.id,
                        lease_token,
                        status="extracting",
                        progress=progress,
                        label=translate(
                            target_language,
                            "job.extracting_recipe",
                            current=index,
                            total=len(candidates),
                        ),
                    )
                    region_images = [
                        crop_source_region(detection_content, detection_mime, region)
                        for region in detected.source_regions
                    ]
                    extracted = extract_recipe(
                        images=region_images,
                        existing_category_paths=category_paths,
                        title_hint=detected.title_hint,
                        target_language=target_language,
                        settings=settings,
                    )
                    extracted = extracted.model_copy(
                        update={
                            "source_regions": detected.source_regions,
                            "recipe_image_candidates": detected.recipe_image_candidates,
                            "has_recipe_image": bool(detected.recipe_image_candidates),
                        }
                    )
                    payload = _recipe_input(job, extracted)
                    warnings = [
                        *detected_document.warnings,
                        *detected.warnings,
                        *extracted.warnings,
                    ]
                    candidate.title = extracted.title
                    candidate.recipe_payload = payload.model_dump(mode="json")
                    candidate.warnings_json = list(dict.fromkeys(warnings))[:100]
                    candidate.confidence = (
                        "low"
                        if "low" in {detected.detection_confidence, extracted.extraction_confidence}
                        else "medium"
                        if "medium"
                        in {detected.detection_confidence, extracted.extraction_confidence}
                        else "high"
                    )
                    candidate.error_message = None
                    db.commit()
                    prepared.append(
                        PreparedCandidate(
                            candidate_id=candidate.id,
                            detected=detected,
                            extracted=extracted,
                            warnings=warnings,
                        )
                    )
                except (AIExtractionError, AIUnavailable, SourceRegionError, ValueError) as exc:
                    db.rollback()
                    failed_candidate = db.get(ImportCandidate, candidate.id)
                    if failed_candidate is not None:
                        failed_candidate.status = "failed"
                        failed_candidate.error_message = str(exc)[:2000]
                        failed_candidate.finished_at = datetime.now(UTC)
                        db.commit()

            verified_options: list[VerifiedImageOption] = []
            for recipe_index, item in enumerate(prepared):
                _progress(
                    db,
                    job.id,
                    lease_token,
                    status="checking_images",
                    progress=62 + int(recipe_index / max(len(prepared), 1) * 18),
                    label=translate(
                        target_language,
                        "job.matching_images",
                        current=recipe_index + 1,
                        total=len(prepared),
                    ),
                )
                recipe_options, image_warnings = _verified_image_options(
                    recipe_index=recipe_index,
                    extracted=item.extracted,
                    detected=item.detected,
                    content=detection_content,
                    mime_type=detection_mime,
                    settings=settings,
                    target_language=target_language,
                )
                verified_options.extend(recipe_options)
                item.warnings.extend(image_warnings)
            image_assignments = _assign_verified_images(verified_options)
            recipes_with_verified_options = {option.recipe_index for option in verified_options}
            for recipe_index in recipes_with_verified_options - image_assignments.keys():
                prepared[recipe_index].warnings.append(
                    translate(target_language, "import.warning.image_used_elsewhere")
                )

            ready_count = 0
            candidate_media: dict[uuid.UUID, tuple[MediaAsset, MediaAsset | None]] = {}
            for recipe_index, item in enumerate(prepared):
                candidate_cleanup: list[Path] = []
                stored_media: tuple[MediaAsset, MediaAsset | None] | None = None
                stored_candidate = db.get(ImportCandidate, item.candidate_id)
                if stored_candidate is None:
                    raise ImportLeaseLost(
                        "Ein erkanntes Rezept wurde während der Verarbeitung entfernt"
                    )
                try:
                    option = image_assignments.get(recipe_index)
                    if option is not None:
                        final_image = option.image
                        generated = False
                        metadata = _image_metadata(option)
                        if settings.ai_image_generation_enabled:
                            _progress(
                                db,
                                job.id,
                                lease_token,
                                status="generating_image",
                                progress=82 + int(recipe_index / max(len(prepared), 1) * 14),
                                label=translate(
                                    target_language,
                                    "job.preparing_image",
                                    current=recipe_index + 1,
                                ),
                            )
                            try:
                                generated_image = maybe_generate_recipe_image(
                                    item.extracted,
                                    option.image,
                                    "image/png",
                                    settings=settings,
                                    reference_is_cropped=True,
                                )
                                if generated_image:
                                    generated_match = verify_recipe_image(
                                        extracted=item.extracted,
                                        image=generated_image,
                                        target_language=target_language,
                                        settings=settings,
                                    )
                                    if (
                                        generated_match.matches_recipe
                                        and generated_match.confidence >= IMAGE_MATCH_THRESHOLD
                                    ):
                                        final_image = generated_image
                                        generated = True
                                        metadata["generated_verification_confidence"] = (
                                            generated_match.confidence
                                        )
                                        metadata["generated_verification_reason"] = (
                                            generated_match.reason
                                        )
                                    else:
                                        item.warnings.append(
                                            translate(
                                                target_language,
                                                "import.warning.prepared_image_mismatch",
                                            )
                                        )
                            except (AIImageError, AIExtractionError, AIUnavailable) as exc:
                                item.warnings.append(
                                    translate(
                                        target_language,
                                        "import.warning.image_preparation_failed",
                                        detail=translate_known_text(
                                            target_language,
                                            str(exc),
                                            fallback_key="error.generic",
                                        ),
                                    )
                                )
                        metadata.update(
                            {
                                "origin": (
                                    "ai_prepared_import" if generated else "verified_import_crop"
                                ),
                                "model": settings.ai_image_model if generated else None,
                                "quality": settings.ai_image_quality if generated else None,
                            }
                        )
                        stored_media = _store_candidate_image(
                            db,
                            candidate=stored_candidate,
                            user=user,
                            image=final_image,
                            generated=generated,
                            metadata=metadata,
                            cleanup_paths=candidate_cleanup,
                        )
                        stored_candidate.image_region_json = option.image_candidate.model_dump(
                            mode="json"
                        )

                    stored_candidate.warnings_json = list(dict.fromkeys(item.warnings))[:100]
                    stored_candidate.status = "ready"
                    stored_candidate.error_message = None
                    stored_candidate.finished_at = datetime.now(UTC)
                    db.commit()
                    if stored_media is not None:
                        candidate_media[stored_candidate.id] = stored_media
                    candidate_cleanup.clear()
                    ready_count += 1
                except (InvalidUpload, OSError) as exc:
                    db.rollback()
                    _remove_paths(candidate_cleanup)
                    reloaded_candidate = db.get(ImportCandidate, item.candidate_id)
                    if reloaded_candidate is None:
                        raise ImportLeaseLost(
                            "Ein erkanntes Rezept wurde während der Verarbeitung entfernt"
                        ) from exc
                    item.warnings.append(
                        translate(
                            target_language,
                            "import.warning.image_save_failed",
                            detail=translate_known_text(
                                target_language,
                                str(exc),
                                fallback_key="error.generic",
                            ),
                        )
                    )
                    reloaded_candidate.image_asset_id = None
                    reloaded_candidate.thumbnail_asset_id = None
                    reloaded_candidate.image_region_json = None
                    reloaded_candidate.warnings_json = list(dict.fromkeys(item.warnings))[:100]
                    reloaded_candidate.status = "ready"
                    reloaded_candidate.error_message = None
                    reloaded_candidate.finished_at = datetime.now(UTC)
                    db.commit()
                    ready_count += 1
                except Exception:
                    db.rollback()
                    _remove_paths(candidate_cleanup)
                    raise

            job = db.get(ImportJob, job_id)
            if job is None:
                raise ImportLeaseLost("Der Importauftrag wurde während der Verarbeitung entfernt")
            if ready_count == 0:
                job.status = "failed"
                job.progress = 100
                job.current_stage = translate(target_language, "job.no_usable_recipes")
                job.error_code = "candidate_extraction_failed"
                candidate_errors: list[str] = []
                for candidate in candidates:
                    failed_candidate = db.get(ImportCandidate, candidate.id)
                    if failed_candidate is None or not failed_candidate.error_message:
                        continue
                    detail = " ".join(failed_candidate.error_message.split())
                    if detail not in candidate_errors:
                        candidate_errors.append(detail)
                if len(candidate_errors) == 1:
                    job.error_message = candidate_errors[0][:2000]
                elif candidate_errors:
                    joined_errors = "; ".join(candidate_errors[:3])
                    job.error_message = (
                        "Keines der erkannten Rezepte konnte vollständig übernommen werden. "
                        f"Details: {joined_errors}"
                    )[:2000]
                else:
                    job.error_message = (
                        "Die erkannten Rezepte konnten nicht zuverlässig extrahiert werden."
                    )
            elif len(detections) == 1:
                sole_candidate = db.get(ImportCandidate, candidates[0].id)
                if sole_candidate is None or sole_candidate.status != "ready":
                    raise ImportLeaseLost(
                        "Das erkannte Rezept wurde während der Verarbeitung entfernt"
                    )
                stored_assets = candidate_media.get(sole_candidate.id)
                if stored_assets is None:
                    image_asset = sole_candidate.image_asset
                    thumbnail_asset = sole_candidate.thumbnail_asset
                else:
                    image_asset, thumbnail_asset = stored_assets
                recipe = _promote_candidate(
                    db,
                    candidate=sole_candidate,
                    user=user,
                    source_asset=asset,
                    image_asset=image_asset,
                    thumbnail_asset=thumbnail_asset,
                    target_language=target_language,
                )
                job.status = "completed"
                job.progress = 100
                job.current_stage = translate(target_language, "job.recipe_imported")
                job.result_recipe_id = recipe.id
                job.error_code = None
                job.error_message = None
            else:
                job.status = "review"
                job.progress = 100
                job.current_stage = translate(
                    target_language,
                    "job.ready.one" if ready_count == 1 else "job.ready.other",
                    count=ready_count,
                )
                job.error_code = None
                job.error_message = None
            job.finished_at = datetime.now(UTC)
            job.lease_token = None
            job.lease_expires_at = None
            job.suggestions_json = {
                "detected_recipes": len(detections),
                "ready_recipes": ready_count,
                "warnings": detected_document.warnings,
            }
            recompute_batch(db, batch_id)
            db.commit()
            completed = True
        except URLRenderError as exc:
            db.rollback()
            job = db.get(ImportJob, job_id)
            if job:
                job.status = "failed"
                job.current_stage = translate(target_language, "job.failed")
                job.error_code = "url_render_failed"
                job.error_message = str(exc)
                job.finished_at = datetime.now(UTC)
                job.lease_token = None
                job.lease_expires_at = None
                recompute_batch(db, batch_id)
                db.commit()
        except (AIUnavailable, AIExtractionError) as exc:
            db.rollback()
            job = db.get(ImportJob, job_id)
            if job:
                job.status = "failed"
                job.current_stage = translate(target_language, "job.failed")
                job.error_code = "ai_unavailable"
                job.error_message = str(exc)
                job.finished_at = datetime.now(UTC)
                job.lease_token = None
                job.lease_expires_at = None
                recompute_batch(db, batch_id)
                db.commit()
        except ImportMaintenance:
            db.rollback()
            db.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id, ImportJob.lease_token == lease_token)
                .values(
                    status="queued",
                    progress=0,
                    current_stage=translate(target_language, "job.maintenance_wait"),
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            recompute_batch(db, batch_id)
            db.commit()
            raise
        except ImportLeaseLost:
            db.rollback()
        except Exception:
            db.rollback()
            job = db.get(ImportJob, job_id)
            if job:
                job.status = "failed"
                job.current_stage = translate(target_language, "job.failed")
                job.error_code = "pipeline_error"
                job.error_message = (
                    "Der Import konnte nicht abgeschlossen werden. Das Original bleibt erhalten."
                )
                job.finished_at = datetime.now(UTC)
                job.lease_token = None
                job.lease_expires_at = None
                recompute_batch(db, batch_id)
                db.commit()
        finally:
            if not completed:
                _remove_paths(cleanup_paths)


def process_import_job(job_id: uuid.UUID) -> None:
    with database_maintenance_guard():
        _process_import_job(job_id)


def _process_import_batch(batch_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        batch = db.get(ImportBatch, batch_id)
        if batch is None:
            return
        batch.status = "processing"
        db.commit()
        job_ids = list(
            db.scalars(
                select(ImportJob.id)
                .where(ImportJob.batch_id == batch_id, ImportJob.status == "queued")
                .order_by(ImportJob.created_at, ImportJob.id)
            )
        )
    for job_id in job_ids:
        process_import_job(job_id)
    with SessionLocal() as db:
        batch = db.get(ImportBatch, batch_id)
        if batch:
            recompute_batch(db, batch_id)
            db.commit()


def process_import_batch(batch_id: uuid.UUID) -> None:
    # Batch bookkeeping is a write too; keep the shared barrier for the whole
    # orchestration, including the gaps between individual jobs.
    with database_maintenance_guard():
        _process_import_batch(batch_id)
