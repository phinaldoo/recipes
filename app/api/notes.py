from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import Session

from app import __version__
from app.assets import frontend_assets
from app.auth.dependencies import csrf_user, current_session, current_user
from app.config import get_settings
from app.database import get_db
from app.i18n import (
    DEFAULT_LOCALE,
    format_datetime_locale,
    locale_context,
    normalize_locale,
    request_locale,
    translate,
)
from app.models import User, UserNote, UserSession
from app.schemas.notes import NotePayload
from app.services.notes import create_note, delete_note, get_note, list_notes, update_note

router = APIRouter(prefix="/notes", tags=["Notizen"])
page_router = APIRouter(include_in_schema=False)
settings = get_settings()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
templates.env.globals["app_base_url"] = settings.app_base_url
templates.env.globals["app_version"] = __version__
templates.env.globals["asset"] = frontend_assets.url
templates.env.globals["pwa_manifest_url"] = frontend_assets.manifest_url


@pass_context
def _format_datetime(context: object, value: datetime | None) -> str:
    locale = normalize_locale(cast(Any, context).get("locale")) or DEFAULT_LOCALE
    return format_datetime_locale(value, locale, settings.display_timezone)


templates.env.filters["datetime"] = _format_datetime
templates.env.filters["datetime_de"] = _format_datetime


def _serialize(note: UserNote) -> dict[str, object]:
    return {
        "id": str(note.id),
        "title": note.title,
        "url": note.url,
        "content": note.content,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
    }


@router.get("")
def index(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"items": [_serialize(note) for note in list_notes(db, user)]}


@router.post("", status_code=201)
def create(
    payload: NotePayload,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    note = create_note(db, user, payload)
    db.commit()
    return {"note": _serialize(note), "message": translate(user.language, "api.note.saved")}


@router.put("/{note_id}")
def update(
    note_id: uuid.UUID,
    payload: NotePayload,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    note = update_note(db, get_note(db, user, note_id), payload)
    db.commit()
    return {"note": _serialize(note), "message": translate(user.language, "api.note.updated")}


@router.delete("/{note_id}")
def delete(
    note_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    delete_note(db, get_note(db, user, note_id))
    db.commit()
    return {"message": translate(user.language, "api.note.deleted")}


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


@page_router.get("/notizen", response_class=HTMLResponse)
def notes_page(
    request: Request,
    session: UserSession = Depends(current_session),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    notes = list_notes(db, session.user)
    return templates.TemplateResponse(
        request,
        "notes/index.html",
        _context(request, session, notes=notes),
    )
