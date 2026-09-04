from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from redis import Redis
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Recipe, RecipeComment, User
from app.services.recipes import refresh_search_document


def _rate_limit(user: User, recipe: Recipe) -> None:
    try:
        with Redis.from_url(
            get_settings().redis_url, decode_responses=True, socket_timeout=1
        ) as redis:
            key = f"comment-rate:{user.id}:{recipe.id}"
            count = cast(int, redis.incr(key))
            if count == 1:
                redis.expire(key, 60)
            if count > 12:
                raise HTTPException(
                    status_code=429,
                    detail="Du hast sehr viele Notizen gesendet. Bitte warte einen Moment.",
                )
    except HTTPException:
        raise
    except Exception:
        return


def create_comment(db: Session, recipe: Recipe, user: User, text: str) -> RecipeComment:
    _rate_limit(user, recipe)
    value = text.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Die Notiz darf nicht leer sein.")
    if len(value) > get_settings().comment_max_length:
        raise HTTPException(status_code=422, detail="Die Notiz ist zu lang.")
    comment = RecipeComment(
        recipe_id=recipe.id,
        author_user_id=user.id,
        author_name_snapshot=user.visible_name,
        text=value,
    )
    db.add(comment)
    db.flush()
    recipe.comments.append(comment)
    refresh_search_document(db, recipe)
    return comment


def update_comment(
    db: Session, recipe: Recipe, comment: RecipeComment, user: User, text: str
) -> None:
    if comment.author_user_id != user.id:
        raise HTTPException(status_code=403, detail="Du darfst nur eigene Notizen bearbeiten.")
    value = text.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Die Notiz darf nicht leer sein.")
    if len(value) > get_settings().comment_max_length:
        raise HTTPException(status_code=422, detail="Die Notiz ist zu lang.")
    comment.text = value
    comment.updated_at = datetime.now(UTC)
    db.flush()
    refresh_search_document(db, recipe)


def delete_comment(db: Session, recipe: Recipe, comment: RecipeComment, user: User) -> None:
    if comment.author_user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Du darfst nur eigene Notizen löschen.")
    comment.deleted_at = datetime.now(UTC)
    comment.updated_at = datetime.now(UTC)
    db.flush()
    refresh_search_document(db, recipe)
