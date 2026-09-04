from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.security import get_session, require_csrf
from app.database import get_db
from app.i18n import request_locale, translate
from app.models import User, UserSession


def current_session(request: Request, db: Session = Depends(get_db)) -> UserSession:
    session = get_session(db, request)
    if session is None:
        locale = request_locale(request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=translate(locale, "auth.required"),
            headers={"X-Login-URL": "/login"},
        )
    request.state.user = session.user
    request.state.session = session
    return session


def current_user(session: UserSession = Depends(current_session)) -> User:
    return session.user


def admin_user(request: Request, user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail=translate(request_locale(request, user), "auth.admin_required"),
        )
    return user


def csrf_session(request: Request, session: UserSession = Depends(current_session)) -> UserSession:
    require_csrf(request, session)
    return session


def csrf_user(session: UserSession = Depends(csrf_session)) -> User:
    return session.user


def csrf_admin(request: Request, user: User = Depends(csrf_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail=translate(request_locale(request, user), "auth.admin_required"),
        )
    return user
