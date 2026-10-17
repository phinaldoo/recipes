from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import ImportBatch, ImportCandidate, ImportJob, MediaAsset, Recipe, User

PAGE_SIZE = 30


def workspace_batches(
    db: Session, user: User, *, query: str = "", filter_name: str = "all", offset: int = 0
) -> dict[str, Any]:
    statement = select(ImportBatch).where(ImportBatch.created_by_user_id == user.id)
    if filter_name == "attention":
        statement = statement.where(
            ImportBatch.jobs.any(
                or_(
                    ImportJob.status == "failed",
                    ImportJob.candidates.any(ImportCandidate.status == "ready"),
                )
            )
        )
    elif filter_name == "saved":
        statement = statement.where(
            ImportBatch.jobs.any(
                or_(
                    ImportJob.result_recipe_id.is_not(None),
                    ImportJob.candidates.any(ImportCandidate.result_recipe_id.is_not(None)),
                )
            )
        )
    if query.strip():
        pattern = (
            "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        statement = statement.where(
            ImportBatch.jobs.any(
                or_(
                    ImportJob.source_url.ilike(pattern, escape="\\"),
                    ImportJob.source_asset.has(
                        MediaAsset.original_filename.ilike(pattern, escape="\\")
                    ),
                    ImportJob.candidates.any(ImportCandidate.title.ilike(pattern, escape="\\")),
                    ImportJob.result_recipe_id.in_(
                        select(Recipe.id).where(Recipe.title.ilike(pattern, escape="\\"))
                    ),
                )
            )
        )
    batches = list(
        db.scalars(
            statement.options(
                selectinload(ImportBatch.jobs).selectinload(ImportJob.source_asset),
                selectinload(ImportBatch.jobs).selectinload(ImportJob.candidates),
            )
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    )
    return {
        "batches": batches[:PAGE_SIZE],
        "next_offset": offset + PAGE_SIZE if len(batches) > PAGE_SIZE else None,
        "import_query": query,
        "import_filter": filter_name,
    }


def duplicate_candidates(db: Session, batch: ImportBatch) -> dict[str, str]:
    candidates = [c for job in batch.jobs for c in job.candidates if c.status == "ready"]
    if not candidates:
        return {}
    titles = {c.title.strip().lower() for c in candidates}
    existing = list(
        db.scalars(
            select(Recipe)
            .where(Recipe.deleted_at.is_(None), func.lower(func.trim(Recipe.title)).in_(titles))
            .order_by(Recipe.created_at, Recipe.id)
        )
    )
    by_title = {recipe.title.strip().lower(): str(recipe.id) for recipe in existing}
    return {
        str(c.id): by_title[c.title.strip().lower()]
        for c in candidates
        if c.title.strip().lower() in by_title
    }
