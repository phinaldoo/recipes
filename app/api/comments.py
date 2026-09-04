from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import csrf_user, current_user
from app.database import get_db
from app.i18n import translate
from app.models import RecipeComment, User
from app.schemas.recipe import CommentInput
from app.services.comments import create_comment, delete_comment, update_comment
from app.services.recipes import get_recipe

router = APIRouter(prefix="/recipes/{recipe_id}/comments", tags=["Kommentare"])


def serialize(comment: RecipeComment, user: User) -> dict[str, object]:
    return {
        "id": str(comment.id),
        "author_name": comment.author_name_snapshot,
        "text": comment.text,
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "can_edit": comment.author_user_id == user.id,
        "can_delete": comment.author_user_id == user.id or user.role == "admin",
    }


@router.get("")
def list_comments(
    recipe_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipe = get_recipe(db, recipe_id)
    return {
        "items": [
            serialize(comment, user) for comment in recipe.comments if comment.deleted_at is None
        ]
    }


@router.post("", status_code=201)
def create(
    recipe_id: uuid.UUID,
    payload: CommentInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    comment = create_comment(db, recipe, user, payload.text)
    db.commit()
    return {
        "comment": serialize(comment, user),
        "message": translate(user.language, "api.comment.added"),
    }


def _comment(db: Session, recipe_id: uuid.UUID, comment_id: uuid.UUID) -> RecipeComment:
    comment = db.scalar(
        select(RecipeComment).where(
            RecipeComment.id == comment_id,
            RecipeComment.recipe_id == recipe_id,
            RecipeComment.deleted_at.is_(None),
        )
    )
    if comment is None:
        raise HTTPException(status_code=404, detail="Die Notiz wurde nicht gefunden.")
    return comment


@router.put("/{comment_id}")
def update(
    recipe_id: uuid.UUID,
    comment_id: uuid.UUID,
    payload: CommentInput,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    comment = _comment(db, recipe_id, comment_id)
    update_comment(db, recipe, comment, user, payload.text)
    db.commit()
    return {
        "comment": serialize(comment, user),
        "message": translate(user.language, "api.comment.saved"),
    }


@router.delete("/{comment_id}")
def delete(
    recipe_id: uuid.UUID,
    comment_id: uuid.UUID,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    recipe = get_recipe(db, recipe_id, for_update=True)
    comment = _comment(db, recipe_id, comment_id)
    delete_comment(db, recipe, comment, user)
    db.commit()
    return {"message": translate(user.language, "api.comment.deleted")}
