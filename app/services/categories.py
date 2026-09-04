from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Category, RecipeCategory
from app.schemas.recipe import CategoryCreate, CategoryUpdate
from app.services.recipes import normalize_name, refresh_search_document, slugify


def category_tree(db: Session) -> list[Category]:
    categories = list(
        db.scalars(
            select(Category).options(
                selectinload(Category.parent),
                selectinload(Category.children),
                selectinload(Category.recipe_links),
            )
        )
    )
    # A global ``ORDER BY position`` interleaves unrelated branches. Build a
    # deterministic depth-first list so the flat ARIA tree and all selectors
    # always render each subtree directly below its parent.
    by_parent: dict[uuid.UUID | None, list[Category]] = {}
    known_ids = {category.id for category in categories}
    for category in categories:
        parent_id = category.parent_id if category.parent_id in known_ids else None
        by_parent.setdefault(parent_id, []).append(category)
    for siblings in by_parent.values():
        siblings.sort(key=lambda item: (item.position, item.normalized_name, str(item.id)))

    ordered: list[Category] = []
    visited: set[uuid.UUID] = set()

    def append_subtree(category: Category) -> None:
        if category.id in visited:
            return
        visited.add(category.id)
        ordered.append(category)
        for child in by_parent.get(category.id, []):
            append_subtree(child)

    for root in by_parent.get(None, []):
        append_subtree(root)
    # Defensive recovery for legacy cycles/orphans: keep them visible rather
    # than silently dropping them from the category manager.
    for category in sorted(
        categories,
        key=lambda item: (item.position, item.normalized_name, str(item.id)),
    ):
        append_subtree(category)
    return ordered


def create_category(db: Session, payload: CategoryCreate, *, origin: str = "manual") -> Category:
    name = payload.name.strip()
    if payload.parent_id and db.get(Category, payload.parent_id) is None:
        raise HTTPException(status_code=404, detail="Die übergeordnete Kategorie existiert nicht.")
    existing = db.scalar(
        select(Category).where(
            Category.normalized_name == normalize_name(name),
            Category.parent_id == payload.parent_id
            if payload.parent_id
            else Category.parent_id.is_(None),
        )
    )
    if existing:
        raise HTTPException(
            status_code=409, detail="An dieser Stelle gibt es die Kategorie bereits."
        )
    max_position = db.scalar(
        select(func.max(Category.position)).where(
            Category.parent_id == payload.parent_id
            if payload.parent_id
            else Category.parent_id.is_(None)
        )
    )
    category = Category(
        parent_id=payload.parent_id,
        name=name,
        normalized_name=normalize_name(name),
        slug=slugify(name),
        position=(max_position if max_position is not None else -1) + 1,
        origin=origin,
    )
    db.add(category)
    db.flush()
    return category


def _descendant_ids(db: Session, category_id: uuid.UUID) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    frontier = {category_id}
    while frontier:
        children = set(
            db.scalars(select(Category.id).where(Category.parent_id.in_(frontier))).all()
        )
        children -= found
        found |= children
        frontier = children
    return found


def update_category(db: Session, category: Category, payload: CategoryUpdate) -> Category:
    if payload.parent_id == category.id or (
        payload.parent_id and payload.parent_id in _descendant_ids(db, category.id)
    ):
        raise HTTPException(
            status_code=409, detail="Eine Kategorie kann nicht unter sich selbst verschoben werden."
        )
    name = payload.name.strip() if payload.name is not None else category.name
    parent_id = payload.parent_id if "parent_id" in payload.model_fields_set else category.parent_id
    collision = db.scalar(
        select(Category).where(
            Category.id != category.id,
            Category.normalized_name == normalize_name(name),
            Category.parent_id == parent_id if parent_id else Category.parent_id.is_(None),
        )
    )
    if collision:
        raise HTTPException(
            status_code=409, detail="Am Ziel gibt es bereits eine gleichnamige Kategorie."
        )
    category.name = name
    category.normalized_name = normalize_name(name)
    category.slug = slugify(name)
    parent_changed = category.parent_id != parent_id
    category.parent_id = parent_id
    if payload.position is not None or parent_changed:
        siblings = list(
            db.scalars(
                select(Category)
                .where(
                    Category.id != category.id,
                    Category.parent_id == parent_id if parent_id else Category.parent_id.is_(None),
                )
                .order_by(Category.position, func.lower(Category.name), Category.id)
            )
        )
        target_position = (
            min(payload.position, len(siblings)) if payload.position is not None else len(siblings)
        )
        siblings.insert(target_position, category)
        for position, sibling in enumerate(siblings):
            sibling.position = position
    db.flush()
    _refresh_linked_recipes(db, category)
    return category


def _refresh_linked_recipes(db: Session, category: Category) -> None:
    categories = {category.id} | _descendant_ids(db, category.id)
    recipe_ids = set(
        db.scalars(
            select(RecipeCategory.recipe_id).where(RecipeCategory.category_id.in_(categories))
        ).all()
    )
    from app.services.recipes import get_recipe

    for recipe_id in recipe_ids:
        refresh_search_document(db, get_recipe(db, recipe_id, for_update=True))


def delete_category(db: Session, category: Category) -> int:
    if category.children:
        raise HTTPException(
            status_code=409,
            detail="Die Kategorie hat Unterkategorien. Verschiebe oder lösche diese zuerst.",
        )
    usage = len(category.recipe_links)
    affected = [link.recipe_id for link in category.recipe_links]
    db.delete(category)
    db.flush()
    from app.services.recipes import get_recipe

    for recipe_id in affected:
        refresh_search_document(db, get_recipe(db, recipe_id, for_update=True))
    return usage


def merge_category(db: Session, source: Category, target: Category) -> int:
    if source.id == target.id:
        raise HTTPException(status_code=409, detail="Quelle und Ziel müssen verschieden sein.")
    if target.id in _descendant_ids(db, source.id):
        raise HTTPException(
            status_code=409, detail="Ein Zusammenführen in einen Nachfahren ist nicht möglich."
        )
    source_children = sorted(
        source.children,
        key=lambda item: (item.position, item.normalized_name, str(item.id)),
    )
    target_children = [child for child in target.children if child.id != source.id]
    target_names = {child.normalized_name for child in target_children}
    for child in source_children:
        collision = db.scalar(
            select(Category).where(
                Category.parent_id == target.id,
                Category.id != source.id,
                Category.normalized_name == child.normalized_name,
            )
        )
        if child.normalized_name in target_names or collision is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Die Unterkategorie ‚{child.name}‘ existiert am Ziel bereits.",
            )
        target_names.add(child.normalized_name)

    affected: set[uuid.UUID] = set()
    moved = 0
    for link in list(source.recipe_links):
        affected.add(link.recipe_id)
        duplicate = db.get(RecipeCategory, (link.recipe_id, target.id))
        if duplicate:
            db.delete(link)
        else:
            link.category_id = target.id
            moved += 1
    combined_children = target_children + source_children
    for position, child in enumerate(combined_children):
        child.parent = target
        child.parent_id = target.id
        child.position = position
    db.delete(source)
    db.flush()
    from app.services.recipes import get_recipe

    for recipe_id in affected:
        refresh_search_document(db, get_recipe(db, recipe_id, for_update=True))
    return moved
