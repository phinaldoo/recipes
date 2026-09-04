from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import csrf_user, current_session
from app.auth.security import (
    DUMMY_PASSWORD_HASH,
    check_login_rate_limit,
    clear_login_account_rate_limit,
    create_session,
    delete_session,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.database import get_db
from app.i18n import (
    SUPPORTED_LOCALES,
    detect_browser_locale,
    normalize_locale,
    request_locale,
    translate,
)
from app.models import User, UserSession

router = APIRouter(prefix="/auth", tags=["Authentifizierung"])


class LoginPayload(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class LanguagePayload(BaseModel):
    language: str = Field(min_length=2, max_length=10)


@router.post("/login")
def login(
    payload: LoginPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    email = payload.email.strip().casefold()
    check_login_rate_limit(request, email)
    user = db.scalar(select(User).where(User.email == email))
    # Always execute Argon2 to reduce account-enumeration timing differences.
    valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not valid or not user.is_active:
        locale = detect_browser_locale(request.headers.get("accept-language"))
        raise HTTPException(status_code=401, detail=translate(locale, "login.invalid"))
    clear_login_account_rate_limit(email)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    if user.language is None:
        user.language = detect_browser_locale(request.headers.get("accept-language"))
    session = create_session(db, user, request, response)
    db.commit()
    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.visible_name,
            "role": user.role,
            "language": user.language,
        },
        "csrf_token": session.csrf_token,
        "redirect": "/rezepte",
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    _: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    delete_session(db, request, response)
    db.commit()
    return {"redirect": "/login"}


@router.get("/me")
def me(session: UserSession = Depends(current_session)) -> dict[str, object]:
    user = session.user
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "name": user.visible_name,
        "role": user.role,
        "language": user.language,
        "csrf_token": session.csrf_token,
    }


@router.patch("/me/language")
def update_language(
    payload: LanguagePayload,
    request: Request,
    user: User = Depends(csrf_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    language = normalize_locale(payload.language, fallback=None)
    if language is None or payload.language not in SUPPORTED_LOCALES:
        raise HTTPException(
            status_code=422,
            detail=translate(request_locale(request, user), "account.invalid_language"),
        )
    user.language = language
    db.commit()
    return {
        "language": language,
        "message": translate(language, "account.saved"),
        "redirect": "/konto",
    }
