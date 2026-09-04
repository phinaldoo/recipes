from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserNote
from app.schemas.notes import NotePayload


def list_notes(db: Session, user: User) -> list[UserNote]:
    return list(
        db.scalars(
            select(UserNote)
            .where(UserNote.user_id == user.id)
            .order_by(UserNote.updated_at.desc(), UserNote.id.desc())
        )
    )


def get_note(db: Session, user: User, note_id: uuid.UUID) -> UserNote:
    note = db.scalar(select(UserNote).where(UserNote.id == note_id, UserNote.user_id == user.id))
    if note is None:
        raise HTTPException(status_code=404, detail="Die Notiz wurde nicht gefunden.")
    return note


def create_note(db: Session, user: User, payload: NotePayload) -> UserNote:
    note = UserNote(
        user_id=user.id,
        title=payload.title,
        url=payload.url,
        content=payload.content,
    )
    db.add(note)
    db.flush()
    return note


def update_note(db: Session, note: UserNote, payload: NotePayload) -> UserNote:
    note.title = payload.title
    note.url = payload.url
    note.content = payload.content
    note.updated_at = datetime.now(UTC)
    db.flush()
    return note


def delete_note(db: Session, note: UserNote) -> None:
    db.delete(note)
    db.flush()
