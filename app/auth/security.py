from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type
from fastapi import HTTPException, Request, Response, status
from redis import Redis
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings, get_settings
from app.models import User, UserSession

password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_fallback_rate_lock = threading.Lock()
_fallback_rate_buckets: dict[str, tuple[int, float]] = {}
_FALLBACK_RATE_BUCKET_LIMIT = 10_000
_argon2_slots = threading.BoundedSemaphore(2)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Das Passwort muss mindestens 12 Zeichen lang sein")
    if len(password) > 1024:
        raise ValueError("Das Passwort ist zu lang")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not _argon2_slots.acquire(timeout=2):
        return False
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    finally:
        _argon2_slots.release()


# Keeps unknown-user login attempts on the same Argon2 path as valid users.
DUMMY_PASSWORD_HASH = password_hasher.hash("unused-login-sentinel-password")


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def _token_hash(token: str, settings: Settings) -> str:
    return hmac.new(settings.app_secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def _user_agent_hash(request: Request) -> str | None:
    user_agent = request.headers.get("user-agent")
    return hashlib.sha256(user_agent.encode()).hexdigest() if user_agent else None


def create_session(db: Session, user: User, request: Request, response: Response) -> UserSession:
    settings = get_settings()
    now = datetime.now(UTC)
    current_token = request.cookies.get(settings.session_cookie_name)
    replaceable_sessions = [UserSession.expires_at <= now]
    if current_token:
        # This response replaces the caller's cookie, so its previous database
        # session would otherwise become orphaned. Other devices have different
        # tokens and remain signed in.
        replaceable_sessions.append(UserSession.token_hash == _token_hash(current_token, settings))
    db.execute(delete(UserSession).where(or_(*replaceable_sessions)))

    raw_token = secrets.token_urlsafe(48)
    session = UserSession(
        user_id=user.id,
        token_hash=_token_hash(raw_token, settings),
        csrf_token=secrets.token_urlsafe(48),
        user_agent_hash=_user_agent_hash(request),
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    db.flush()
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    return session


def delete_session(db: Session, request: Request, response: Response) -> None:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token:
        db.execute(
            delete(UserSession).where(UserSession.token_hash == _token_hash(raw_token, settings))
        )
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.session_cookie_samesite,
    )


def get_session(db: Session, request: Request) -> UserSession | None:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        return None
    session = db.scalar(
        select(UserSession)
        .options(joinedload(UserSession.user))
        .where(UserSession.token_hash == _token_hash(raw_token, settings))
    )
    if session is None:
        return None
    now = datetime.now(UTC)
    if session.expires_at <= now or not session.user.is_active:
        db.delete(session)
        db.commit()
        return None
    if session.user_agent_hash and not hmac.compare_digest(
        session.user_agent_hash, _user_agent_hash(request) or ""
    ):
        db.delete(session)
        db.commit()
        return None
    if (now - session.last_seen_at).total_seconds() > 300:
        session.last_seen_at = now
        db.commit()
    return session


def require_csrf(request: Request, session: UserSession) -> None:
    supplied = request.headers.get("x-csrf-token")
    if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Die Sicherheitsprüfung ist fehlgeschlagen. Bitte lade die Seite neu.",
        )
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != get_settings().app_base_url:
        raise HTTPException(status_code=403, detail="Ungültige Anfragequelle")


def new_login_csrf_token() -> str:
    return secrets.token_urlsafe(48)


def set_login_csrf_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.login_csrf_cookie_name,
        token,
        max_age=settings.login_csrf_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/login",
    )


def clear_login_csrf_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        settings.login_csrf_cookie_name,
        path="/login",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def require_login_csrf(request: Request, supplied_token: str) -> None:
    settings = get_settings()
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source = origin or referer
    if source is None or _origin_tuple(source) != _origin_tuple(settings.app_base_url):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Die Anmeldeanfrage stammt nicht von dieser Anwendung.",
        )
    cookie_token = request.cookies.get(settings.login_csrf_cookie_name, "")
    if (
        not supplied_token
        or not cookie_token
        or not hmac.compare_digest(supplied_token, cookie_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Die Anmeldeseite ist abgelaufen. Bitte lade sie neu.",
        )


def _login_account_rate_key(email: str, settings: Settings) -> str:
    canonical_email = email.strip().casefold()
    account_digest = hmac.new(
        settings.app_secret_key.encode(),
        f"login-account:{canonical_email}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"login-rate:account:{account_digest}"


def clear_login_account_rate_limit(email: str) -> None:
    """Forget account-specific attempts after a successful authentication.

    The IP bucket intentionally remains intact so one valid account cannot be
    used to bypass the broader brute-force limit for other accounts.
    """

    settings = get_settings()
    account_key = _login_account_rate_key(email, settings)
    with (
        suppress(Exception),
        Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1) as redis,
    ):
        redis.delete(account_key)
    with _fallback_rate_lock:
        _fallback_rate_buckets.pop(account_key, None)


def check_login_rate_limit(request: Request, email: str) -> None:
    settings = get_settings()
    client = request.client.host if request.client else "unknown"
    ip_digest = hashlib.sha256(client.encode()).hexdigest()
    buckets = (
        (_login_account_rate_key(email, settings), settings.login_rate_limit_attempts),
        (f"login-rate:ip:{ip_digest}", settings.login_rate_limit_ip_attempts),
    )
    try:
        with Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1) as redis:
            pipeline = redis.pipeline(transaction=True)
            for key, _ in buckets:
                pipeline.incr(key)
                pipeline.expire(key, settings.login_rate_limit_window_seconds, nx=True)
            results = pipeline.execute()
        counts = (int(results[0]), int(results[2]))
        limited = any(count > limit for count, (_, limit) in zip(counts, buckets, strict=True))
    except HTTPException:
        raise
    except Exception:
        now = time.monotonic()
        limited = False
        with _fallback_rate_lock:
            expired = [
                key for key, (_, expires) in _fallback_rate_buckets.items() if expires <= now
            ]
            for key in expired:
                _fallback_rate_buckets.pop(key, None)
            new_keys = sum(key not in _fallback_rate_buckets for key, _ in buckets)
            if len(_fallback_rate_buckets) + new_keys > _FALLBACK_RATE_BUCKET_LIMIT:
                limited = True
            else:
                for key, limit in buckets:
                    count, expires = _fallback_rate_buckets.get(
                        key, (0, now + settings.login_rate_limit_window_seconds)
                    )
                    count += 1
                    _fallback_rate_buckets[key] = (count, expires)
                    limited = limited or count > limit
    if limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zu viele Anmeldeversuche. Bitte warte einen Moment.",
            headers={"Retry-After": str(settings.login_rate_limit_window_seconds)},
        )
