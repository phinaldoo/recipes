from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Favorite,
    Recipe,
    RecipeTag,
    RecipeVersion,
    SearchSynonym,
    Tag,
    User,
)
from app.schemas.recipe import RecipeInput, RecipeKind
from app.services.productivity_lock import (
    acquire_productivity_lock,
    acquire_tag_membership_lock,
    dialect_name,
)
from app.services.recipes import (
    RecipeConflict,
    apply_recipe_input,
    create_version,
    get_recipe,
    normalize_name,
    recipe_load_options,
    refresh_search_document,
)


def list_favorites(
    db: Session,
    user: User,
    *,
    recipe_kind: RecipeKind | None = None,
) -> list[Recipe]:
    query = (
        select(Recipe)
        .join(Favorite, Favorite.recipe_id == Recipe.id)
        .where(
            Favorite.user_id == user.id,
            Recipe.deleted_at.is_(None),
            Recipe.status == "active",
        )
    )
    if recipe_kind is not None:
        query = query.where(Recipe.recipe_kind == recipe_kind)
    return list(
        db.scalars(
            query.options(*recipe_load_options()).order_by(Favorite.created_at.desc())
        ).unique()
    )


def favorite_recipe_ids(db: Session, user: User, recipe_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    requested_ids = tuple(dict.fromkeys(recipe_ids))
    if not requested_ids:
        return set()
    return set(
        db.scalars(
            select(Favorite.recipe_id).where(
                Favorite.user_id == user.id,
                Favorite.recipe_id.in_(requested_ids),
            )
        )
    )


def is_favorite(db: Session, user: User, recipe_id: uuid.UUID) -> bool:
    return db.get(Favorite, (user.id, recipe_id)) is not None


def set_favorite(db: Session, user: User, recipe_id: uuid.UUID, enabled: bool) -> bool:
    get_recipe(db, recipe_id)
    acquire_productivity_lock(db)
    favorite = db.get(Favorite, (user.id, recipe_id))
    if enabled and favorite is None:
        db.add(Favorite(user_id=user.id, recipe_id=recipe_id))
    elif not enabled and favorite is not None:
        db.delete(favorite)
    db.flush()
    return enabled


def list_tags(db: Session) -> list[tuple[Tag, int]]:
    rows = db.execute(
        select(Tag, func.count(RecipeTag.recipe_id))
        .outerjoin(RecipeTag, RecipeTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(func.lower(Tag.name))
    )
    return [(row[0], int(row[1])) for row in rows]


def create_tag(db: Session, name: str) -> Tag:
    acquire_tag_membership_lock(db)
    normalized = normalize_name(name)
    if dialect_name(db) == "postgresql":
        identifier = uuid.uuid4()
        created_id = db.scalar(
            postgresql_insert(Tag)
            .values(id=identifier, name=name, normalized_name=normalized)
            .on_conflict_do_nothing(index_elements=[Tag.normalized_name])
            .returning(Tag.id)
        )
        if created_id is None:
            raise HTTPException(status_code=409, detail="Dieses Schlagwort gibt es bereits.")
        tag = db.get(Tag, created_id)
        if tag is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Das angelegte Schlagwort konnte nicht gelesen werden")
        return tag
    existing = db.scalar(select(Tag).where(Tag.normalized_name == normalized))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dieses Schlagwort gibt es bereits.")
    tag = Tag(name=name, normalized_name=normalized)
    db.add(tag)
    db.flush()
    return tag


def rename_tag(db: Session, tag_id: uuid.UUID, name: str, user: User) -> Tag:
    acquire_productivity_lock(db)
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Das Schlagwort wurde nicht gefunden.")
    normalized = normalize_name(name)
    duplicate = db.scalar(select(Tag).where(Tag.normalized_name == normalized, Tag.id != tag.id))
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Dieses Schlagwort gibt es bereits.")
    recipes = list(
        db.scalars(
            select(Recipe)
            .join(RecipeTag, RecipeTag.recipe_id == Recipe.id)
            .where(RecipeTag.tag_id == tag.id)
            .order_by(Recipe.id)
            .with_for_update(of=Recipe)
            .options(*recipe_load_options())
        ).unique()
    )
    acquire_tag_membership_lock(db)
    duplicate = db.scalar(select(Tag).where(Tag.normalized_name == normalized, Tag.id != tag.id))
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Dieses Schlagwort gibt es bereits.")
    tag.name = name
    tag.normalized_name = normalized
    db.flush()
    changed_at = datetime.now(UTC)
    for recipe in recipes:
        recipe.updated_at = changed_at
        recipe.updated_by_user_id = user.id
        recipe.updated_by_name_snapshot = user.visible_name
        refresh_search_document(db, recipe)
        create_version(db, recipe, user, f"Schlagwort in „{name}“ umbenannt")
    return tag


def delete_tag(db: Session, tag_id: uuid.UUID) -> None:
    acquire_productivity_lock(db)
    acquire_tag_membership_lock(db)
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Das Schlagwort wurde nicht gefunden.")
    usage = int(
        db.scalar(select(func.count()).select_from(RecipeTag).where(RecipeTag.tag_id == tag.id))
        or 0
    )
    if usage:
        raise HTTPException(
            status_code=409,
            detail=f"Das Schlagwort wird noch von {usage} Rezept(en) verwendet.",
        )
    db.delete(tag)


def list_synonyms(db: Session) -> list[SearchSynonym]:
    return list(db.scalars(select(SearchSynonym).order_by(SearchSynonym.normalized_term)))


def create_synonym(db: Session, term: str, synonym: str) -> SearchSynonym:
    pair = sorted(((normalize_name(term), term), (normalize_name(synonym), synonym)))
    normalized_term, display_term = pair[0]
    normalized_synonym, display_synonym = pair[1]
    if dialect_name(db) == "postgresql":
        identifier = uuid.uuid4()
        created_id = db.scalar(
            postgresql_insert(SearchSynonym)
            .values(
                id=identifier,
                term=display_term,
                normalized_term=normalized_term,
                synonym=display_synonym,
                normalized_synonym=normalized_synonym,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SearchSynonym.normalized_term,
                    SearchSynonym.normalized_synonym,
                ]
            )
            .returning(SearchSynonym.id)
        )
        if created_id is None:
            raise HTTPException(status_code=409, detail="Dieses Synonympaar gibt es bereits.")
        entry = db.get(SearchSynonym, created_id)
        if entry is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Das angelegte Synonympaar konnte nicht gelesen werden")
        return entry
    existing = db.scalar(
        select(SearchSynonym).where(
            SearchSynonym.normalized_term == normalized_term,
            SearchSynonym.normalized_synonym == normalized_synonym,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dieses Synonympaar gibt es bereits.")
    entry = SearchSynonym(
        term=display_term,
        normalized_term=normalized_term,
        synonym=display_synonym,
        normalized_synonym=normalized_synonym,
    )
    db.add(entry)
    db.flush()
    return entry


def delete_synonym(db: Session, synonym_id: uuid.UUID) -> None:
    entry = db.get(SearchSynonym, synonym_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Das Synonym wurde nicht gefunden.")
    db.delete(entry)


DIFF_LABELS = {
    "title": "Titel",
    "description": "Beschreibung",
    "recipe_kind": "Rezeptart",
    "base_servings": "Ausgangsmenge",
    "serving_label": "Mengenbezeichnung",
    "prep_time_minutes": "Vorbereitungszeit",
    "cook_time_minutes": "Koch-/Backzeit",
    "rest_time_minutes": "Ruhezeit",
    "total_time_minutes": "Gesamtzeit",
    "nutrition": "Nährwerte",
    "notes": "Hinweise",
    "status": "Status",
    "ingredient_groups": "Zutaten",
    "instruction_steps": "Zubereitung",
    "categories": "Kategorien",
    "tags": "Schlagwörter",
    "source": "Quelle",
}


def _truncate_diff(value: str, limit: int = 700) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


def _ingredient_summary(value: list[dict[str, Any]]) -> str:
    groups: list[str] = []
    for group in value:
        ingredients: list[str] = []
        for ingredient in group.get("ingredients", []):
            if not isinstance(ingredient, dict):
                continue
            minimum = ingredient.get("amount_min")
            maximum = ingredient.get("amount_max")
            amount = str(minimum) if minimum is not None else ""
            if maximum is not None:
                amount = f"{amount}–{maximum}" if amount else str(maximum)
            ingredients.append(
                " ".join(
                    str(part)
                    for part in (amount, ingredient.get("unit"), ingredient.get("name"))
                    if part
                )
            )
        title = str(group.get("title") or "Zutaten")
        groups.append(f"{title}: {', '.join(ingredients) or 'leer'}")
    return _truncate_diff(" | ".join(groups) or "–")


def _nutrition_summary(value: list[dict[str, Any]]) -> str:
    labels = {
        "energy_kj": "kJ",
        "energy_kcal": "kcal",
        "fat_g": "g Fett",
        "saturated_fat_g": "g gesättigte Fettsäuren",
        "carbohydrates_g": "g Kohlenhydrate",
        "sugars_g": "g Zucker",
        "fiber_g": "g Ballaststoffe",
        "protein_g": "g Eiweiß",
        "salt_g": "g Salz",
    }
    rows = []
    for item in value:
        basis = "pro Portion" if item.get("basis") == "per_serving" else "pro 100 g/ml"
        values = [
            f"{item.get(field)} {label}"
            for field, label in labels.items()
            if item.get(field) is not None
        ]
        if item.get("note"):
            values.append(str(item["note"]))
        rows.append(f"{basis}: {', '.join(values)}")
    return _truncate_diff(" | ".join(rows) or "–")


def _diff_value(value: Any, field: str | None = None) -> str:
    if value is None or value == "":
        return "–"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            dictionaries = [item for item in value if isinstance(item, dict)]
            if field == "ingredient_groups":
                return _ingredient_summary(dictionaries)
            if field == "nutrition":
                return _nutrition_summary(dictionaries)
            if field == "instruction_steps":
                return _truncate_diff(
                    " · ".join(
                        f"{index}. {item.get('text', '')}"
                        for index, item in enumerate(dictionaries, start=1)
                    )
                    or "–"
                )
            if field == "categories":
                paths = []
                for item in dictionaries:
                    raw_path = item.get("path", [])
                    paths.append(
                        raw_path
                        if isinstance(raw_path, str)
                        else " › ".join(str(part) for part in raw_path)
                    )
                return _truncate_diff(", ".join(paths) or "–")
            return _truncate_diff(str(dictionaries))
        return ", ".join(str(item) for item in value) or "–"
    if isinstance(value, dict):
        return _truncate_diff(
            ", ".join(f"{key}: {item}" for key, item in value.items() if item) or "–"
        )
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if field == "recipe_kind":
        return {"cooking": "Kochen", "baking": "Backen"}.get(str(value), str(value))
    return str(value)


def snapshot_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, str]]:
    previous = before or {}
    changes = []
    for key, label in DIFF_LABELS.items():
        if previous.get(key) != after.get(key):
            changes.append(
                {
                    "field": label,
                    "before": _diff_value(previous.get(key), key),
                    "after": _diff_value(after.get(key), key),
                }
            )
    return changes


def version_history(
    db: Session,
    recipe_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[tuple[RecipeVersion, list[dict[str, str]]]], int, int, int]:
    get_recipe(db, recipe_id, include_deleted=True)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(RecipeVersion)
            .where(RecipeVersion.recipe_id == recipe_id)
        )
        or 0
    )
    pages = max(1, math.ceil(total / page_size))
    effective_page = min(max(page, 1), pages)
    versions = list(
        db.scalars(
            select(RecipeVersion)
            .where(RecipeVersion.recipe_id == recipe_id)
            .options(selectinload(RecipeVersion.changed_by))
            .order_by(RecipeVersion.version_number.desc())
            .offset((effective_page - 1) * page_size)
            .limit(page_size)
        )
    )
    previous: dict[str, Any] | None = None
    if versions:
        previous = db.scalar(
            select(RecipeVersion.snapshot)
            .where(
                RecipeVersion.recipe_id == recipe_id,
                RecipeVersion.version_number < min(item.version_number for item in versions),
            )
            .order_by(RecipeVersion.version_number.desc())
            .limit(1)
        )
    result: list[tuple[RecipeVersion, list[dict[str, str]]]] = []
    for version in reversed(versions):
        result.append((version, snapshot_diff(previous, version.snapshot)))
        previous = version.snapshot
    return list(reversed(result)), total, pages, effective_page


def restore_version(
    db: Session,
    user: User,
    recipe_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_updated_at: datetime,
) -> Recipe:
    recipe = get_recipe(db, recipe_id, include_deleted=True, for_update=True)
    actual = recipe.updated_at.astimezone(UTC)
    if actual != expected_updated_at.astimezone(UTC):
        raise RecipeConflict("Das Rezept wurde inzwischen geändert. Bitte lade den Verlauf neu.")
    version = db.get(RecipeVersion, version_id)
    if version is None or version.recipe_id != recipe.id:
        raise HTTPException(status_code=404, detail="Die Rezeptversion wurde nicht gefunden.")
    snapshot = dict(version.snapshot)
    categories = []
    for category in snapshot.get("categories", []):
        raw_path = category.get("path", [])
        path = raw_path.split(" › ") if isinstance(raw_path, str) else raw_path
        categories.append({"id": None, "path": path, "origin": category.get("origin", "manual")})
    snapshot["categories"] = categories
    snapshot.setdefault("tags", [])
    snapshot.setdefault("recipe_kind", recipe.recipe_kind)
    snapshot["expected_updated_at"] = expected_updated_at
    payload = RecipeInput.model_validate(snapshot)
    recipe.deleted_at = None
    restored = apply_recipe_input(db, recipe, payload, user)
    create_version(db, restored, user, f"Version {version.version_number} wiederhergestellt")
    db.flush()
    return restored
