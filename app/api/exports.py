from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.config import get_settings
from app.database import get_db
from app.i18n import DEFAULT_LOCALE, normalize_locale
from app.models import User
from app.services.exports import (
    RecipeExportBusy,
    RecipeExportTooLarge,
    json_export_slot,
    recipe_package_dict,
    render_recipe_pdf,
)
from app.services.recipes import get_recipe

router = APIRouter(prefix="/recipes/{recipe_id}/export", tags=["Exporte"])


def _attachment_header(filename: str) -> str:
    if filename.isascii():
        return f'attachment; filename="{filename}"'
    suffix = ".rezept.json" if filename.endswith(".rezept.json") else ".pdf"
    fallback = f"recipe{suffix}"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/json")
def export_json(
    recipe_id: uuid.UUID,
    include_originals: bool = Query(default=True),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = get_recipe(db, recipe_id)
    try:
        with json_export_slot():
            content = json.dumps(
                recipe_package_dict(recipe, include_originals=include_originals),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            if len(content) > get_settings().recipe_json_export_max_bytes:
                raise RecipeExportTooLarge("Das erzeugte Rezeptpaket ist zu groß.")
    except RecipeExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RecipeExportBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": _attachment_header(f"{recipe.slug}.rezept.json"),
            "Cache-Control": "no-store",
        },
    )


@router.get("/pdf")
async def export_pdf(
    recipe_id: uuid.UUID,
    servings: float = Query(gt=0, le=100_000),
    include_comments: bool = Query(default=False),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = get_recipe(db, recipe_id)
    filename = f"{recipe.slug}.pdf"
    db.expunge_all()
    db.close()
    locale = normalize_locale(_.language) or DEFAULT_LOCALE
    if locale == DEFAULT_LOCALE:
        content = await render_recipe_pdf(
            recipe,
            desired_servings=servings,
            include_comments=include_comments,
        )
    else:
        content = await render_recipe_pdf(
            recipe,
            desired_servings=servings,
            include_comments=include_comments,
            locale=locale,
        )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _attachment_header(filename),
            "Cache-Control": "no-store",
        },
    )
