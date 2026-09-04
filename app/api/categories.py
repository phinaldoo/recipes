from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.dependencies import csrf_user, current_user
from app.database import get_db
from app.i18n import translate
from app.models import Category, User
from app.schemas.recipe import CategoryCreate, CategoryMerge, CategoryMove, CategoryUpdate
from app.services.categories import (
    category_tree,
    create_category,
    delete_category,
    merge_category,
    update_category,
)

router = APIRouter(prefix="/categories", tags=["Kategorien"])


def serialize(category: Category) -> dict[str, object]:
    return {
        "id": str(category.id),
        "parent_id": str(category.parent_id) if category.parent_id else None,
        "name": category.name,
        "path": category.path,
        "position": category.position,
        "origin": category.origin,
        "recipe_count": len(category.recipe_links),
        "child_count": len(category.children),
    }


@router.get("")
def index(_: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    return {"items": [serialize(category) for category in category_tree(db)]}


@router.post("", status_code=201)
def create(
    payload: CategoryCreate,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    category = create_category(db, payload)
    db.commit()
    return {
        "category": serialize(category),
        "message": translate(_.language, "api.category.created"),
    }


def _category(db: Session, category_id: uuid.UUID) -> Category:
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Die Kategorie wurde nicht gefunden.")
    return category


@router.put("/{category_id}")
def update(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    category = update_category(db, _category(db, category_id), payload)
    db.commit()
    return {"category": serialize(category), "message": translate(_.language, "api.category.saved")}


@router.post("/{category_id}/move")
def move(
    category_id: uuid.UUID,
    payload: CategoryMove,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    update_payload = CategoryUpdate(parent_id=payload.parent_id, position=payload.position)
    category = update_category(db, _category(db, category_id), update_payload)
    db.commit()
    return {"category": serialize(category), "message": translate(_.language, "api.category.moved")}


@router.post("/{category_id}/merge")
def merge(
    category_id: uuid.UUID,
    payload: CategoryMerge,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    moved = merge_category(
        db, _category(db, category_id), _category(db, payload.target_category_id)
    )
    db.commit()
    return {"moved_recipe_links": moved, "message": translate(_.language, "api.category.merged")}


@router.delete("/{category_id}")
def delete(
    category_id: uuid.UUID,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    usage = delete_category(db, _category(db, category_id))
    db.commit()
    return {"affected_recipes": usage, "message": translate(_.language, "api.category.deleted")}
