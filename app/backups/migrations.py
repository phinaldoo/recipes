from __future__ import annotations

import re
import unicodedata
import uuid
from collections import defaultdict
from copy import deepcopy
from typing import Any

from app.backups.errors import InvalidBackup
from app.backups.exporter import BACKUP_MODELS, table_name
from app.backups.schemas import DATABASE_SCHEMA_VERSION

SUPPORTED_DATABASE_SCHEMA_VERSIONS = frozenset(
    {
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
        "0010",
        "0011",
        DATABASE_SCHEMA_VERSION,
    }
)

_CURRENT_TABLE_NAMES = tuple(table_name(model) for model in BACKUP_MODELS)
_CURRENT_TABLES = frozenset(_CURRENT_TABLE_NAMES)
_REMOVED_PRODUCTIVITY_TABLES = frozenset({"shopping_list_items", "meal_plan_entries"})
_ADDED_NUTRITION_TABLES = frozenset({"recipe_nutrition"})
_ADDED_IMPORT_CANDIDATE_TABLES = frozenset({"import_candidates"})
_ADDED_USER_NOTE_TABLES = frozenset({"user_notes"})
_SCHEMA_0010_TABLES = _CURRENT_TABLES
_SCHEMA_0009_TABLES = _CURRENT_TABLES - _ADDED_USER_NOTE_TABLES
_SCHEMA_0008_TABLES = _SCHEMA_0009_TABLES - _ADDED_IMPORT_CANDIDATE_TABLES
_SCHEMA_0004_TABLES = _SCHEMA_0008_TABLES - _ADDED_NUTRITION_TABLES
_SCHEMA_0002_TABLES = _SCHEMA_0004_TABLES | _REMOVED_PRODUCTIVITY_TABLES
_SCHEMA_0001_ADDED_TABLES = ("tags", "search_synonyms", "recipe_tags", "recipe_shares")
_SCHEMA_0001_TABLES = _SCHEMA_0002_TABLES - frozenset(_SCHEMA_0001_ADDED_TABLES)


def expected_tables_for_schema(schema_version: str) -> frozenset[str]:
    if schema_version == "0001":
        return _SCHEMA_0001_TABLES
    if schema_version in {"0002", "0003"}:
        return _SCHEMA_0002_TABLES
    if schema_version == "0004":
        return _SCHEMA_0004_TABLES
    if schema_version in {"0005", "0006", "0007", "0008"}:
        return _SCHEMA_0008_TABLES
    if schema_version == "0009":
        return _SCHEMA_0009_TABLES
    if schema_version in {"0010", "0011"}:
        return _SCHEMA_0010_TABLES
    if schema_version == DATABASE_SCHEMA_VERSION:
        return _CURRENT_TABLES
    raise InvalidBackup("Diese Datenbankschemaversion wird nicht unterstützt")


def normalize_tables(
    schema_version: str,
    raw_tables: dict[str, object],
) -> dict[str, list[dict[str, Any]]]:
    """Return current-schema data without mutating the verified archive payload."""
    expected_tables_for_schema(schema_version)
    tables = deepcopy(raw_tables)
    if schema_version == "0001":
        for name in _SCHEMA_0001_ADDED_TABLES:
            tables[name] = []
    if schema_version in {"0001", "0002", "0003"}:
        for name in _REMOVED_PRODUCTIVITY_TABLES:
            tables.pop(name, None)
    if schema_version in {"0001", "0002", "0003", "0004"}:
        for name in _ADDED_NUTRITION_TABLES:
            tables[name] = []
    if schema_version in {"0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"}:
        for name in _ADDED_IMPORT_CANDIDATE_TABLES:
            tables[name] = []
    if schema_version != DATABASE_SCHEMA_VERSION:
        for name in _ADDED_USER_NOTE_TABLES:
            if schema_version != "0010":
                tables[name] = []

        users = tables.get("users")
        if not isinstance(users, list):
            raise InvalidBackup("Die Tabelle users enthält eine ungültige Zeile")
        for user in users:
            if not isinstance(user, dict):
                raise InvalidBackup("Die Tabelle users enthält eine ungültige Zeile")
            user.setdefault("language", None)

        batches = tables.get("import_batches")
        if not isinstance(batches, list):
            raise InvalidBackup("Die Tabelle import_batches enthält eine ungültige Zeile")
        for batch in batches:
            if not isinstance(batch, dict):
                raise InvalidBackup("Die Tabelle import_batches enthält eine ungültige Zeile")
            batch.setdefault("target_language", "de")

    if set(tables) != _CURRENT_TABLES:
        raise InvalidBackup("Die Tabellenliste des Backups ist unvollständig")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for name in _CURRENT_TABLE_NAMES:
        raw_rows = tables[name]
        if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
            raise InvalidBackup(f"Die Tabelle {name} enthält eine ungültige Zeile")
        normalized[name] = raw_rows
    if schema_version != DATABASE_SCHEMA_VERSION:
        _activate_legacy_drafts(normalized)
        _add_legacy_recipe_kinds(normalized)
    return normalized


_CATEGORY_UUID_NAMESPACE = uuid.UUID("a2a9739d-7142-4c2a-8ed8-01fd9f674dd2")


def _typed_uuid(value: str) -> dict[str, str]:
    return {"$type": "uuid", "value": value}


def _uuid_value(value: object) -> str | None:
    if isinstance(value, dict) and value.get("$type") == "uuid":
        raw_value = value.get("value")
        if isinstance(raw_value, str):
            return raw_value
    if isinstance(value, str):
        return value
    return None


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug[:240] or "rezept"


def _payload_recipe_kind(payload: object) -> str:
    if not isinstance(payload, dict):
        return "cooking"
    categories = payload.get("categories")
    if not isinstance(categories, list):
        return "cooking"
    for category in categories:
        raw_path = category.get("path") if isinstance(category, dict) else None
        if (
            isinstance(raw_path, list)
            and raw_path
            and isinstance(raw_path[0], str)
            and _normalize_name(raw_path[0]) == "backen"
        ):
            return "baking"
    return "cooking"


def _add_legacy_recipe_kinds(tables: dict[str, list[dict[str, Any]]]) -> None:
    children_by_parent: defaultdict[str | None, set[str]] = defaultdict(set)
    baking_categories: set[str] = set()
    for category in tables["categories"]:
        category_id = _uuid_value(category.get("id"))
        if category_id is None:
            continue
        parent_id = _uuid_value(category.get("parent_id"))
        children_by_parent[parent_id].add(category_id)
        if parent_id is None and category.get("normalized_name") == "backen":
            baking_categories.add(category_id)

    frontier = set(baking_categories)
    while frontier:
        descendants = {
            child_id
            for parent_id in frontier
            for child_id in children_by_parent.get(parent_id, set())
        } - baking_categories
        baking_categories.update(descendants)
        frontier = descendants

    baking_recipe_ids = {
        recipe_id
        for link in tables["recipe_categories"]
        if (recipe_id := _uuid_value(link.get("recipe_id"))) is not None
        and _uuid_value(link.get("category_id")) in baking_categories
    }
    kind_by_recipe_id: dict[str, str] = {}
    for recipe in tables["recipes"]:
        recipe_id = _uuid_value(recipe.get("id"))
        kind = "baking" if recipe_id in baking_recipe_ids else "cooking"
        recipe["recipe_kind"] = kind
        if recipe_id is not None:
            kind_by_recipe_id[recipe_id] = kind

    for version in tables["recipe_versions"]:
        snapshot = version.get("snapshot")
        recipe_id = _uuid_value(version.get("recipe_id"))
        if isinstance(snapshot, dict):
            snapshot.setdefault("recipe_kind", kind_by_recipe_id.get(recipe_id or "", "cooking"))

    for candidate in tables["import_candidates"]:
        payload = candidate.get("recipe_payload")
        if isinstance(payload, dict):
            payload.setdefault("recipe_kind", _payload_recipe_kind(payload))


def _legacy_suggested_paths(payload: object) -> tuple[list[list[str]], dict[str, Any] | None]:
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


def _activate_legacy_drafts(tables: dict[str, list[dict[str, Any]]]) -> None:
    recipes_by_id = {
        identifier: row
        for row in tables["recipes"]
        if (identifier := _uuid_value(row.get("id"))) is not None
    }
    for recipe_row in recipes_by_id.values():
        if recipe_row.get("status") == "draft":
            recipe_row["status"] = "active"

    category_by_parent_and_name: dict[tuple[str | None, str], str] = {}
    known_category_ids: set[str] = set()
    next_position: defaultdict[str | None, int] = defaultdict(int)
    for category in tables["categories"]:
        category_id = _uuid_value(category.get("id"))
        normalized_name = category.get("normalized_name")
        if category_id is None or not isinstance(normalized_name, str):
            continue
        existing_parent_id = _uuid_value(category.get("parent_id"))
        category_by_parent_and_name[(existing_parent_id, normalized_name)] = category_id
        known_category_ids.add(category_id)
        position = category.get("position")
        if isinstance(position, int):
            next_position[existing_parent_id] = max(next_position[existing_parent_id], position + 1)

    existing_links = {
        (link_recipe_id, link_category_id)
        for row in tables["recipe_categories"]
        if (link_recipe_id := _uuid_value(row.get("recipe_id"))) is not None
        and (link_category_id := _uuid_value(row.get("category_id"))) is not None
    }
    added_paths: defaultdict[str, list[str]] = defaultdict(list)

    for job in tables["import_jobs"]:
        if job.get("status") != "review":
            continue
        suggested_paths, metadata = _legacy_suggested_paths(job.get("suggestions_json"))
        recipe_id = _uuid_value(job.get("result_recipe_id"))
        target_recipe = recipes_by_id.get(recipe_id or "")
        for path in suggested_paths if target_recipe is not None else []:
            assert recipe_id is not None
            assert target_recipe is not None
            parent_id: str | None = None
            for name in path:
                normalized_name = _normalize_name(name)
                key = (parent_id, normalized_name)
                category_id = category_by_parent_and_name.get(key)
                if category_id is None:
                    candidate = uuid.uuid5(
                        _CATEGORY_UUID_NAMESPACE,
                        f"{parent_id or 'root'}\0{normalized_name}",
                    )
                    salt = 2
                    while str(candidate) in known_category_ids:
                        candidate = uuid.uuid5(
                            _CATEGORY_UUID_NAMESPACE,
                            f"{parent_id or 'root'}\0{normalized_name}\0{salt}",
                        )
                        salt += 1
                    category_id = str(candidate)
                    timestamp = target_recipe.get("updated_at") or target_recipe.get("created_at")
                    tables["categories"].append(
                        {
                            "parent_id": _typed_uuid(parent_id) if parent_id else None,
                            "name": name,
                            "normalized_name": normalized_name,
                            "slug": _slugify(name),
                            "position": next_position[parent_id],
                            "origin": "ai_import",
                            "id": _typed_uuid(category_id),
                            "created_at": timestamp,
                            "updated_at": timestamp,
                        }
                    )
                    category_by_parent_and_name[key] = category_id
                    known_category_ids.add(category_id)
                    next_position[parent_id] += 1
                parent_id = category_id
            assert parent_id is not None
            link = (recipe_id, parent_id)
            if link not in existing_links:
                tables["recipe_categories"].append(
                    {
                        "recipe_id": _typed_uuid(recipe_id),
                        "category_id": _typed_uuid(parent_id),
                    }
                )
                existing_links.add(link)
                added_paths[recipe_id].append(" › ".join(path))
        job["status"] = "completed"
        job["current_stage"] = "Import abgeschlossen"
        job["suggestions_json"] = metadata

    for recipe_id, added_path_values in added_paths.items():
        target_recipe = recipes_by_id[recipe_id]
        existing_document = target_recipe.get("search_document")
        target_recipe["search_document"] = "\n".join(
            part
            for part in [existing_document, *added_path_values]
            if isinstance(part, str) and part
        )
        target_recipe["search_vector"] = None
