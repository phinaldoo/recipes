from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import csrf_user, current_user
from app.database import get_db
from app.i18n import DEFAULT_LOCALE, LOCALES, normalize_locale, translate
from app.models import Recipe, User
from app.schemas.recipe import RecipeInput, RecipeKind
from app.services.recipes import (
    RecipeConflict,
    create_recipe,
    get_recipe,
    list_recipes,
    restore_recipe,
    soft_delete_recipe,
    update_recipe,
)

router = APIRouter(prefix="/recipes", tags=["Rezepte"])


def _with_default_serving_label(
    payload: RecipeInput,
    *,
    user: User,
    existing_label: str | None = None,
) -> RecipeInput:
    if "serving_label" in payload.model_fields_set:
        return payload
    locale = normalize_locale(user.language) or DEFAULT_LOCALE
    return payload.model_copy(
        update={"serving_label": existing_label or LOCALES[locale].default_serving_label}
    )


def recipe_summary(recipe: Recipe) -> dict[str, object]:
    return {
        "id": str(recipe.id),
        "title": recipe.title,
        "slug": recipe.slug,
        "description": recipe.description,
        "recipe_kind": getattr(recipe, "recipe_kind", "cooking"),
        "base_servings": str(recipe.base_servings),
        "serving_label": recipe.serving_label,
        "total_time_minutes": recipe.total_time_minutes,
        "nutrition": [
            {
                "basis": value.basis,
                "energy_kj": str(value.energy_kj) if value.energy_kj is not None else None,
                "energy_kcal": str(value.energy_kcal) if value.energy_kcal is not None else None,
                "fat_g": str(value.fat_g) if value.fat_g is not None else None,
                "saturated_fat_g": str(value.saturated_fat_g)
                if value.saturated_fat_g is not None
                else None,
                "carbohydrates_g": str(value.carbohydrates_g)
                if value.carbohydrates_g is not None
                else None,
                "sugars_g": str(value.sugars_g) if value.sugars_g is not None else None,
                "fiber_g": str(value.fiber_g) if value.fiber_g is not None else None,
                "protein_g": str(value.protein_g) if value.protein_g is not None else None,
                "salt_g": str(value.salt_g) if value.salt_g is not None else None,
                "note": value.note,
            }
            for value in getattr(recipe, "nutrition", [])
        ],
        "status": recipe.status,
        "deleted_at": recipe.deleted_at.isoformat() if recipe.deleted_at else None,
        "categories": [
            {"id": str(category.id), "name": category.name, "path": category.path}
            for category in recipe.categories
        ],
        "comment_count": sum(comment.deleted_at is None for comment in recipe.comments),
        "cover_asset_id": str(recipe.cover_image.media_asset_id) if recipe.cover_image else None,
        "created_at": recipe.created_at.isoformat(),
        "updated_at": recipe.updated_at.isoformat(),
    }


@router.get("")
def index(
    q: str = Query(default="", max_length=300),
    category_ids: list[uuid.UUID] = Query(
        default=[],
        description=(
            "Kategorie-IDs; jede Auswahl umfasst ihre Unterkategorien, "
            "mehrere Auswahlen werden kombiniert."
        ),
    ),
    recipe_kind: RecipeKind | None = None,
    sort: str = Query(default="updated_desc", pattern="^(updated_desc|created_desc|title_asc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipes, total, pages, current_page = list_recipes(
        db,
        q=q,
        category_ids=category_ids,
        recipe_kind=recipe_kind,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [recipe_summary(recipe) for recipe in recipes],
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total": total,
            "pages": pages,
        },
    }


@router.post("", status_code=201)
def create(
    payload: RecipeInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        payload = _with_default_serving_label(payload, user=user)
        recipe = create_recipe(db, payload, user)
        db.commit()
        return {
            "recipe": recipe_summary(get_recipe(db, recipe.id)),
            "redirect": f"/rezepte/{recipe.id}",
        }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{recipe_id}")
def detail(
    recipe_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"recipe": recipe_summary(get_recipe(db, recipe_id))}


@router.put("/{recipe_id}")
def update(
    recipe_id: uuid.UUID,
    payload: RecipeInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    try:
        payload = _with_default_serving_label(
            payload,
            user=user,
            existing_label=recipe.serving_label,
        )
        update_recipe(db, recipe, payload, user)
        db.commit()
        return {
            "recipe": recipe_summary(get_recipe(db, recipe.id)),
            "message": translate(user.language, "api.recipe.saved"),
        }
    except RecipeConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{recipe_id}")
def delete(
    recipe_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    soft_delete_recipe(db, recipe, user)
    db.commit()
    return {"message": translate(user.language, "api.recipe.trashed"), "redirect": "/rezepte"}


@router.post("/{recipe_id}/restore")
def restore(
    recipe_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    recipe = get_recipe(db, recipe_id, include_deleted=True, for_update=True)
    if recipe.deleted_at is None:
        raise HTTPException(status_code=409, detail="Das Rezept ist nicht gelöscht.")
    restore_recipe(db, recipe, user)
    db.commit()
    return {"message": translate(user.language, "api.recipe.restored")}
