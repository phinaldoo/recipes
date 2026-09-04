"""activate imports and materialize suggested categories

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-30
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug[:240] or "rezept"


def _suggested_paths(payload: object) -> tuple[list[list[str]], dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return [], None
    metadata = dict(payload)
    raw_categories = metadata.pop("categories", [])
    paths: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    if isinstance(raw_categories, list):
        for raw_category in raw_categories[:20]:
            raw_path = (
                raw_category.get("path")
                if isinstance(raw_category, dict)
                else [raw_category]
                if isinstance(raw_category, str)
                else None
            )
            if not isinstance(raw_path, list):
                continue
            path = [part.strip() for part in raw_path if isinstance(part, str) and part.strip()]
            if not path or len(path) > 20 or any(len(part) > 200 for part in path):
                continue
            key = tuple(_normalize_name(part) for part in path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths, metadata or None


def _materialize_review_categories() -> None:
    bind = op.get_bind()
    categories = sa.table(
        "categories",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("parent_id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("normalized_name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("position", sa.Integer()),
        sa.column("origin", sa.String()),
    )
    recipe_categories = sa.table(
        "recipe_categories",
        sa.column("recipe_id", postgresql.UUID(as_uuid=True)),
        sa.column("category_id", postgresql.UUID(as_uuid=True)),
    )
    import_jobs = sa.table(
        "import_jobs",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("status", sa.String()),
        sa.column("result_recipe_id", postgresql.UUID(as_uuid=True)),
        sa.column("current_stage", sa.String()),
        sa.column("suggestions_json", sa.JSON()),
    )

    category_by_parent_and_name: dict[tuple[uuid.UUID | None, str], uuid.UUID] = {}
    next_position: defaultdict[uuid.UUID | None, int] = defaultdict(int)
    for row in bind.execute(
        sa.select(
            categories.c.id,
            categories.c.parent_id,
            categories.c.normalized_name,
            categories.c.position,
        )
    ).mappings():
        key = (row["parent_id"], row["normalized_name"])
        category_by_parent_and_name[key] = row["id"]
        next_position[row["parent_id"]] = max(
            next_position[row["parent_id"]], int(row["position"]) + 1
        )

    existing_links = {
        (row["recipe_id"], row["category_id"])
        for row in bind.execute(
            sa.select(recipe_categories.c.recipe_id, recipe_categories.c.category_id)
        ).mappings()
    }
    added_paths: defaultdict[uuid.UUID, list[str]] = defaultdict(list)

    review_jobs = list(
        bind.execute(
            sa.select(
                import_jobs.c.id,
                import_jobs.c.result_recipe_id,
                import_jobs.c.suggestions_json,
            ).where(import_jobs.c.status == "review")
        ).mappings()
    )
    for job in review_jobs:
        suggested_paths, metadata = _suggested_paths(job["suggestions_json"])
        recipe_id = job["result_recipe_id"]
        if recipe_id is not None:
            for path in suggested_paths:
                parent_id: uuid.UUID | None = None
                for name in path:
                    normalized_name = _normalize_name(name)
                    key = (parent_id, normalized_name)
                    category_id = category_by_parent_and_name.get(key)
                    if category_id is None:
                        category_id = uuid.uuid4()
                        bind.execute(
                            categories.insert().values(
                                id=category_id,
                                parent_id=parent_id,
                                name=name,
                                normalized_name=normalized_name,
                                slug=_slugify(name),
                                position=next_position[parent_id],
                                origin="ai_import",
                            )
                        )
                        category_by_parent_and_name[key] = category_id
                        next_position[parent_id] += 1
                    parent_id = category_id
                if parent_id is not None:
                    link = (recipe_id, parent_id)
                else:
                    continue
                if link not in existing_links:
                    bind.execute(
                        recipe_categories.insert().values(
                            recipe_id=recipe_id,
                            category_id=parent_id,
                        )
                    )
                    existing_links.add(link)
                    added_paths[recipe_id].append(" › ".join(path))

        bind.execute(
            import_jobs.update()
            .where(import_jobs.c.id == job["id"])
            .values(
                status="completed",
                current_stage="Import abgeschlossen",
                suggestions_json=metadata,
            )
        )

    for recipe_id, added_path_values in added_paths.items():
        category_text = "\n".join(added_path_values)
        bind.execute(
            sa.text(
                """
                UPDATE recipes
                SET search_document = concat_ws(
                        E'\\n',
                        NULLIF(search_document, ''),
                        CAST(:category_text AS text)
                    ),
                    search_vector = COALESCE(search_vector, ''::tsvector)
                        || setweight(
                            to_tsvector('german', unaccent(CAST(:category_text AS text))),
                            'B'
                        )
                WHERE id = :recipe_id
                """
            ),
            {"recipe_id": recipe_id, "category_text": category_text},
        )


def upgrade() -> None:
    _materialize_review_categories()
    op.execute("UPDATE recipes SET status = 'active' WHERE status = 'draft'")

    op.drop_constraint("ck_recipes_status", "recipes", type_="check")
    op.create_check_constraint(
        "ck_recipes_status",
        "recipes",
        "status IN ('active', 'archived')",
    )
    op.drop_constraint("ck_import_jobs_status", "import_jobs", type_="check")
    op.create_check_constraint(
        "ck_import_jobs_status",
        "import_jobs",
        "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
        "'generating_image', 'validating', 'completed', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_import_jobs_status", "import_jobs", type_="check")
    op.create_check_constraint(
        "ck_import_jobs_status",
        "import_jobs",
        "status IN ('queued', 'preparing', 'extracting', 'checking_images', "
        "'generating_image', 'validating', 'review', 'completed', 'failed', 'cancelled')",
    )
    op.drop_constraint("ck_recipes_status", "recipes", type_="check")
    op.create_check_constraint(
        "ck_recipes_status",
        "recipes",
        "status IN ('draft', 'active', 'archived')",
    )
