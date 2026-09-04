from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.assets import frontend_assets
from app.auth.dependencies import csrf_user, current_session, current_user
from app.config import get_settings
from app.database import get_db
from app.i18n import (
    DEFAULT_LOCALE,
    Locale,
    format_datetime_locale,
    locale_context,
    normalize_locale,
    request_locale,
    translate,
)
from app.models import Recipe, RecipeVersion, User, UserSession
from app.schemas.productivity import (
    ShareCreateInput,
    SynonymPayload,
    TagPayload,
    VersionRestoreInput,
)
from app.schemas.recipe import RecipeKind
from app.services.productivity import (
    create_synonym,
    create_tag,
    delete_synonym,
    delete_tag,
    is_favorite,
    list_favorites,
    list_synonyms,
    list_tags,
    rename_tag,
    restore_version,
    set_favorite,
    version_history,
)
from app.services.recipes import RecipeConflict, get_recipe, recipe_load_options
from app.services.scaling import format_amount, format_decimal, format_duration
from app.services.shares import (
    create_share,
    list_shares,
    resolve_share,
    resolve_share_image,
    revoke_share,
)
from app.services.storage import resolve_storage_key

api_router = APIRouter(tags=["Produktivität"])
page_router = APIRouter(include_in_schema=False)
settings = get_settings()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


def _jinja_locale(context: object) -> Locale:
    return normalize_locale(cast(Any, context).get("locale")) or DEFAULT_LOCALE


@pass_context
def _format_amount(context: object, value: object) -> str:
    return format_amount(cast(Any, value), _jinja_locale(context))


@pass_context
def _format_decimal(context: object, value: object) -> str:
    return format_decimal(cast(Any, value), _jinja_locale(context))


@pass_context
def _format_duration(context: object, value: object) -> str:
    return format_duration(cast(Any, value), _jinja_locale(context))


templates.env.filters["format_amount"] = _format_amount
templates.env.filters["format_decimal"] = _format_decimal
templates.env.filters["duration"] = _format_duration
templates.env.globals["app_base_url"] = settings.app_base_url
templates.env.globals["app_version"] = __version__
templates.env.globals["asset"] = frontend_assets.url
templates.env.globals["pwa_manifest_url"] = frontend_assets.manifest_url


@pass_context
def _format_datetime(context: object, value: datetime | None) -> str:
    return format_datetime_locale(value, _jinja_locale(context), settings.display_timezone)


templates.env.filters["datetime"] = _format_datetime
templates.env.filters["datetime_de"] = _format_datetime


def _context(request: Request, session: UserSession, **values: Any) -> dict[str, Any]:
    locale = request_locale(request, session.user)
    result = {
        "request": request,
        "current_user": session.user,
        "csrf_token": session.csrf_token,
        "app_version": __version__,
        "pwa_enabled": settings.pwa_enabled,
        "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
        **values,
    }
    result.update(locale_context(locale))
    return result


@api_router.get("/favorites/{recipe_id}")
def favorite_state(
    recipe_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    return {"favorite": is_favorite(db, user, recipe_id)}


@api_router.put("/favorites/{recipe_id}")
def add_favorite(
    recipe_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    set_favorite(db, user, recipe_id, True)
    db.commit()
    return {"favorite": True, "message": translate(user.language, "api.favorite.added")}


@api_router.delete("/favorites/{recipe_id}")
def remove_favorite(
    recipe_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    set_favorite(db, user, recipe_id, False)
    db.commit()
    return {"favorite": False, "message": translate(user.language, "api.favorite.removed")}


@api_router.get("/tags")
def tags_index(_: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    return {
        "items": [
            {"id": str(tag.id), "name": tag.name, "recipe_count": count}
            for tag, count in list_tags(db)
        ]
    }


@api_router.post("/tags", status_code=201)
def tags_create(
    payload: TagPayload,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    tag = create_tag(db, payload.name)
    db.commit()
    return {
        "tag": {"id": str(tag.id), "name": tag.name},
        "message": translate(_.language, "api.tag.created"),
    }


@api_router.put("/tags/{tag_id}")
def tags_update(
    tag_id: uuid.UUID,
    payload: TagPayload,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    tag = rename_tag(db, tag_id, payload.name, user)
    db.commit()
    return {
        "tag": {"id": str(tag.id), "name": tag.name},
        "message": translate(user.language, "api.tag.renamed"),
    }


@api_router.delete("/tags/{tag_id}")
def tags_delete(
    tag_id: uuid.UUID,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    delete_tag(db, tag_id)
    db.commit()
    return {"message": translate(_.language, "api.tag.deleted")}


@api_router.get("/search-synonyms")
def synonyms_index(
    _: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    return {
        "items": [
            {"id": str(item.id), "term": item.term, "synonym": item.synonym}
            for item in list_synonyms(db)
        ]
    }


@api_router.post("/search-synonyms", status_code=201)
def synonyms_create(
    payload: SynonymPayload,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    item = create_synonym(db, payload.term, payload.synonym)
    db.commit()
    return {
        "synonym": {"id": str(item.id), "term": item.term, "synonym": item.synonym},
        "message": translate(_.language, "api.synonym.created"),
    }


@api_router.delete("/search-synonyms/{synonym_id}")
def synonyms_delete(
    synonym_id: uuid.UUID,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    delete_synonym(db, synonym_id)
    db.commit()
    return {"message": translate(_.language, "api.synonym.deleted")}


@api_router.get("/recipes/{recipe_id}/shares")
def shares_index(
    recipe_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"items": [_share_dict(item) for item in list_shares(db, recipe_id)]}


def _share_dict(item: Any) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": str(item.id),
        "token_prefix": item.token_prefix,
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
        "expired": bool(item.expires_at and item.expires_at <= now),
        "last_accessed_at": item.last_accessed_at.isoformat() if item.last_accessed_at else None,
    }


@api_router.post("/recipes/{recipe_id}/shares", status_code=201)
def shares_create(
    recipe_id: uuid.UUID,
    payload: ShareCreateInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    share, token = create_share(db, user, recipe_id, payload.expires_in_days)
    db.commit()
    return {
        "share": _share_dict(share),
        "url": f"{settings.app_base_url}/freigabe/{token}",
        "message": translate(user.language, "api.share.created"),
    }


@api_router.delete("/recipes/{recipe_id}/shares/{share_id}")
def shares_revoke(
    recipe_id: uuid.UUID,
    share_id: uuid.UUID,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    revoke_share(db, recipe_id, share_id)
    db.commit()
    return {"message": translate(_.language, "api.share.revoked")}


@api_router.get("/recipes/{recipe_id}/versions")
def versions_index(
    recipe_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=50),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    history, total, pages, effective_page = version_history(
        db, recipe_id, page=page, page_size=page_size
    )
    return {
        "items": [
            {
                "id": str(version.id),
                "version_number": version.version_number,
                "summary": version.change_summary,
                "created_at": version.created_at.isoformat(),
                "changed_by": version.changed_by.visible_name if version.changed_by else None,
                "changes": changes,
            }
            for version, changes in history
        ],
        "total": total,
        "page": effective_page,
        "pages": pages,
        "page_size": page_size,
    }


@api_router.get("/recipes/{recipe_id}/versions/{version_id}")
def version_detail(
    recipe_id: uuid.UUID,
    version_id: uuid.UUID,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    get_recipe(db, recipe_id, include_deleted=True)
    version = db.get(RecipeVersion, version_id)
    if version is None or version.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail="Die Rezeptversion wurde nicht gefunden.")
    return {
        "id": str(version.id),
        "version_number": version.version_number,
        "summary": version.change_summary,
        "created_at": version.created_at.isoformat(),
        "snapshot": version.snapshot,
    }


@api_router.post("/recipes/{recipe_id}/versions/{version_id}/restore")
def versions_restore(
    recipe_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: VersionRestoreInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    try:
        recipe = restore_version(db, user, recipe_id, version_id, payload.expected_updated_at)
        db.commit()
        return {
            "message": translate(user.language, "api.version.restored"),
            "redirect": f"/rezepte/{recipe.id}",
        }
    except RecipeConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@page_router.get("/favoriten", response_class=HTMLResponse)
def favorites_page(
    request: Request,
    session: UserSession = Depends(current_session),
    db: Session = Depends(get_db),
    recipe_kind: RecipeKind | None = None,
) -> HTMLResponse:
    recipes = list_favorites(db, session.user, recipe_kind=recipe_kind)
    return templates.TemplateResponse(
        request,
        "productivity/favorites.html",
        _context(
            request,
            session,
            recipes=recipes,
            favorite_recipe_ids={recipe.id for recipe in recipes},
            selected_recipe_kind=recipe_kind,
        ),
    )


@page_router.get("/schlagwoerter", response_class=HTMLResponse)
def tags_page(
    request: Request,
    session: UserSession = Depends(current_session),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "productivity/tags.html",
        _context(request, session, tags=list_tags(db), synonyms=list_synonyms(db)),
    )


@page_router.get("/rezepte/{recipe_id}/teilen", response_class=HTMLResponse)
def share_page(
    recipe_id: uuid.UUID,
    request: Request,
    session: UserSession = Depends(current_session),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = db.scalar(select(Recipe).where(Recipe.id == recipe_id, Recipe.deleted_at.is_(None)))
    if recipe is None:
        raise HTTPException(status_code=404, detail="Das Rezept wurde nicht gefunden.")
    return templates.TemplateResponse(
        request,
        "productivity/shares.html",
        _context(
            request,
            session,
            recipe=recipe,
            shares=list_shares(db, recipe_id),
            now=datetime.now(UTC),
        ),
    )


@page_router.get("/rezepte/{recipe_id}/verlauf", response_class=HTMLResponse)
def history_page(
    recipe_id: uuid.UUID,
    request: Request,
    page: int = Query(default=1, ge=1),
    session: UserSession = Depends(current_session),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = db.scalar(select(Recipe).where(Recipe.id == recipe_id).options(*recipe_load_options()))
    if recipe is None:
        raise HTTPException(status_code=404, detail="Das Rezept wurde nicht gefunden.")
    history, total, pages, effective_page = version_history(db, recipe_id, page=page)
    return templates.TemplateResponse(
        request,
        "productivity/history.html",
        _context(
            request,
            session,
            recipe=recipe,
            history=history,
            total=total,
            pages=pages,
            page=effective_page,
        ),
    )


@page_router.get("/freigabe/{token}", response_class=HTMLResponse)
def public_share_page(token: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _, recipe = resolve_share(db, token)
    db.commit()
    locale = request_locale(request)
    context = {"request": request, "recipe": recipe, "token": token}
    context.update(locale_context(locale))
    return templates.TemplateResponse(
        request,
        "productivity/public-share.html",
        context,
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@page_router.get("/freigabe/{token}/bild/{image_id}")
def public_share_image(
    token: str,
    image_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> FileResponse:
    _, asset = resolve_share_image(db, token, image_id)
    db.commit()
    return FileResponse(
        resolve_storage_key(asset.storage_key),
        media_type=asset.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
