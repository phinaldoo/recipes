from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import UserNote
from app.schemas.notes import NotePayload
from app.services.notes import get_note, list_notes


def test_note_payload_normalizes_text_and_preserves_line_breaks() -> None:
    payload = NotePayload(
        title="  Schnelle   Pasta  ",
        url="  https://example.test/rezepte/pasta  ",
        content="  Erst einkaufen.\r\nDann kochen.  ",
    )

    assert payload.title == "Schnelle Pasta"
    assert payload.url == "https://example.test/rezepte/pasta"
    assert payload.content == "Erst einkaufen.\nDann kochen."


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.test/recipe",
        "https://user:secret@example.test/recipe",
        "https://example.test:invalid/recipe",
    ],
)
def test_note_payload_rejects_unsafe_or_invalid_links(url: str) -> None:
    with pytest.raises(ValidationError):
        NotePayload(url=url)


def test_note_payload_requires_at_least_one_nonempty_field() -> None:
    with pytest.raises(ValidationError):
        NotePayload(title="  ", url=None, content="\n")


def test_note_payload_checks_length_after_unicode_normalization() -> None:
    with pytest.raises(ValidationError):
        NotePayload(title="ﬃ" * 100)


def test_list_notes_query_is_user_scoped_and_newest_first() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    db = Mock()
    db.scalars.return_value = []

    assert list_notes(db, user) == []  # type: ignore[arg-type]

    statement = db.scalars.call_args.args[0]
    assert user.id in statement.compile().params.values()
    assert "user_notes.user_id" in str(statement)
    assert "user_notes.updated_at DESC" in str(statement)


def test_get_note_hides_notes_owned_by_another_user() -> None:
    note_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    db = Mock()
    db.scalar.return_value = None

    with pytest.raises(HTTPException) as missing:
        get_note(db, user, note_id)  # type: ignore[arg-type]

    assert missing.value.status_code == 404
    statement = db.scalar.call_args.args[0]
    parameters = set(statement.compile().params.values())
    assert {note_id, user.id} <= parameters


def test_user_note_model_requires_an_owner_and_some_content() -> None:
    table = UserNote.__table__

    assert table.c.user_id.nullable is False
    assert any(constraint.name == "ck_user_notes_has_content" for constraint in table.constraints)
