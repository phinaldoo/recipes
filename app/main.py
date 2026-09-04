from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import auth as auth_api
from app.api import categories as categories_api
from app.api import comments as comments_api
from app.api import exports as exports_api
from app.api import imports as imports_api
from app.api import media as media_api
from app.api import notes as notes_api
from app.api import productivity as productivity_api
from app.api import recipes as recipes_api
from app.api import settings as settings_api
from app.assets import frontend_assets
from app.auth.dependencies import admin_user, current_user
from app.auth.security import (
    DUMMY_PASSWORD_HASH,
    check_login_rate_limit,
    clear_login_account_rate_limit,
    clear_login_csrf_cookie,
    create_session,
    delete_session,
    get_session,
    new_login_csrf_token,
    require_login_csrf,
    set_login_csrf_cookie,
    verify_password,
)
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.i18n import (
    DEFAULT_LOCALE,
    Locale,
    detect_browser_locale,
    format_datetime_locale,
    locale_context,
    normalize_locale,
    request_locale,
    translate,
    translate_known_text,
)
from app.imports.pipeline import requeue_stale_imports
from app.maintenance import database_maintenance_shared_guard
from app.models import (
    BackupRestoreJob,
    ImageGenerationJob,
    ImportBatch,
    ImportJob,
    User,
)
from app.schemas.recipe import RecipeKind
from app.services.categories import category_tree
from app.services.exports import scaled_recipe_view
from app.services.image_generation import (
    get_active_image_generation_job,
    image_generation_available,
    requeue_stale_image_generation_jobs,
)
from app.services.media_quota import cleanup_terminal_import_sources
from app.services.productivity import favorite_recipe_ids
from app.services.recipes import get_recipe, list_recipes
from app.services.scaling import format_amount, format_decimal, format_duration
from app.services.storage import (
    active_storage_root,
    cleanup_retained_files,
    recover_interrupted_restore,
)
from app.upload_limits import FormBodyLimitMiddleware
from app.workers.tasks import (
    backup_task,
    image_generation_task,
    import_job_task,
    recover_pending_restore,
    requeue_stale_maintenance_jobs,
    restore_task,
)

logger = logging.getLogger(__name__)
settings = get_settings()
stale_import_reaper: asyncio.Task[None] | None = None
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _jinja_locale(context: object) -> Locale:
    values = cast(Any, context)
    return normalize_locale(values.get("locale")) or DEFAULT_LOCALE


@pass_context
def localized_amount(context: object, value: object) -> str:
    return format_amount(cast(Any, value), _jinja_locale(context))


@pass_context
def localized_decimal(context: object, value: object) -> str:
    return format_decimal(cast(Any, value), _jinja_locale(context))


@pass_context
def localized_duration(context: object, value: object) -> str:
    return format_duration(cast(Any, value), _jinja_locale(context))


templates.env.filters["format_amount"] = localized_amount
templates.env.filters["format_decimal"] = localized_decimal


@pass_context
def format_datetime(context: object, value: datetime | None) -> str:
    return format_datetime_locale(value, _jinja_locale(context), settings.display_timezone)


templates.env.filters["datetime_de"] = format_datetime
templates.env.filters["datetime"] = format_datetime
templates.env.filters["duration"] = localized_duration
templates.env.globals["app_base_url"] = settings.app_base_url
templates.env.globals["app_version"] = __version__
templates.env.globals["asset"] = frontend_assets.url
templates.env.globals["pwa_manifest_url"] = frontend_assets.manifest_url


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))[:100]
        request.state.request_id = request_id
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                with Redis.from_url(
                    settings.redis_url, decode_responses=True, socket_timeout=0.5
                ) as redis:
                    if redis.get("maintenance:restore") or redis.get("maintenance:backup"):
                        return JSONResponse(
                            status_code=503,
                            content={
                                "detail": "Die Anwendung wird gerade wiederhergestellt. Schreibzugriffe sind vorübergehend pausiert."
                            },
                            headers={"Retry-After": "30"},
                        )
            except Exception:
                # Redis is only the fast UX signal. The PostgreSQL advisory lock
                # remains the authority, but accepting a write while that signal
                # is unavailable would let it wait indefinitely behind restore
                # and makes maintenance state ambiguous to callers.
                logger.exception("Wartungsstatus konnte nicht aus Redis gelesen werden")
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "Der Wartungsstatus ist momentan nicht verfügbar. Schreibzugriffe bleiben vorsorglich pausiert."
                    },
                    headers={"Retry-After": "10"},
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/static/assets/") and response.status_code in {
            200,
            206,
            304,
        }:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    startup()
    await start_stale_import_reaper()
    try:
        yield
    finally:
        await stop_stale_import_reaper()


app = FastAPI(
    title="Rezeptverwaltung",
    version=__version__,
    docs_url=None if settings.app_env == "production" else "/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(FormBodyLimitMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
if settings.force_https:
    app.add_middleware(HTTPSRedirectMiddleware)

app.mount(
    "/static/assets",
    StaticFiles(directory=frontend_assets.asset_directory),
    name="frontend-assets",
)

api_prefix = "/api/v1"
for router in (
    auth_api.router,
    recipes_api.router,
    comments_api.router,
    categories_api.router,
    media_api.router,
    exports_api.router,
    imports_api.router,
    notes_api.router,
    productivity_api.api_router,
    settings_api.router,
):
    app.include_router(router, prefix=api_prefix)
app.include_router(productivity_api.page_router)
app.include_router(notes_api.page_router)


def startup() -> None:
    settings.ensure_directories()
    active_storage_root(settings)
    recover_interrupted_restore(settings)
    cleanup_retained_files(settings)
    cleanup_terminal_import_sources(settings)


def _cleanup_background_files() -> None:
    # Cleanup mutates both media and database state, so it participates in the
    # same shared barrier as request and import writes.
    with database_maintenance_shared_guard():
        cleanup_retained_files(settings)
        cleanup_terminal_import_sources(settings)


async def _stale_import_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(recover_pending_restore)
            identifiers = await asyncio.to_thread(requeue_stale_imports)
            image_identifiers = await asyncio.to_thread(requeue_stale_image_generation_jobs)
            await asyncio.to_thread(requeue_stale_maintenance_jobs)
            with SessionLocal() as db:
                queued_imports = list(
                    db.scalars(
                        select(ImportJob.id)
                        .where(ImportJob.status == "queued")
                        .order_by(ImportJob.created_at, ImportJob.id)
                        .limit(200)
                    )
                )
                queued_maintenance = list(
                    db.scalars(
                        select(BackupRestoreJob)
                        .where(BackupRestoreJob.status == "queued")
                        .order_by(BackupRestoreJob.created_at, BackupRestoreJob.id)
                        .limit(20)
                    )
                )
                queued_images = list(
                    db.scalars(
                        select(ImageGenerationJob.id)
                        .where(ImageGenerationJob.status == "queued")
                        .order_by(ImageGenerationJob.created_at, ImageGenerationJob.id)
                        .limit(200)
                    )
                )
            for identifier in set(identifiers) | set(queued_imports):
                import_job_task.send(str(identifier))
            for identifier in set(image_identifiers) | set(queued_images):
                image_generation_task.send(str(identifier))
            for job in queued_maintenance:
                if job.operation == "export":
                    backup_task.send(str(job.id))
                elif job.archive_filename:
                    restore_path = settings.backup_temp_root / job.archive_filename
                    restore_task.send(str(job.id), str(restore_path))
            await asyncio.to_thread(_cleanup_background_files)
        except Exception:
            logger.exception("Warteschlangen-Dispatcher wird beim nächsten Lauf erneut versuchen")
        await asyncio.sleep(60)


async def start_stale_import_reaper() -> None:
    global stale_import_reaper
    stale_import_reaper = asyncio.create_task(_stale_import_loop())


async def stop_stale_import_reaper() -> None:
    global stale_import_reaper
    if stale_import_reaper is not None:
        stale_import_reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stale_import_reaper
        stale_import_reaper = None


def _page_context(request: Request, db: Session) -> dict[str, object]:
    session = get_session(db, request)
    if session is None:
        raise HTTPException(status_code=401, detail="Bitte melde dich an.")
    request.state.user = session.user
    request.state.session = session
    locale = request_locale(request, session.user)
    result = {
        "request": request,
        "current_user": session.user,
        "csrf_token": session.csrf_token,
        "app_version": __version__,
        "pwa_enabled": settings.pwa_enabled,
        "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
    }
    result.update(locale_context(locale))
    return result


def _template(
    request: Request,
    db: Session,
    name: str,
    context: dict[str, object] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    values = _page_context(request, db)
    values.update(context or {})
    return templates.TemplateResponse(request, name, values, status_code=status_code)


def _recipe_search_template(request: Request) -> str:
    result_mode = request.headers.get("x-recipe-results")
    if result_mode == "append":
        return "recipes/_batch.html"
    if result_mode == "1":
        return "recipes/_results.html"
    return "recipes/list.html"


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    locale = request_locale(request)
    fallback_key = (
        "error.not_found"
        if exc.status_code == 404
        else "error.forbidden"
        if exc.status_code == 403
        else "error.validation_short"
        if exc.status_code in {400, 409, 413, 422}
        else "error.generic"
    )
    message = translate_known_text(locale, str(exc.detail), fallback_key=fallback_key)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": message,
                },
                "request_id": getattr(request.state, "request_id", None),
            },
            headers=exc.headers,
        )
    if exc.status_code == 401:
        target = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={target}", status_code=303)
    try:
        context = {
            "request": request,
            "status_code": exc.status_code,
            "message": message,
            "current_user": getattr(request.state, "user", None),
            "csrf_token": getattr(getattr(request.state, "session", None), "csrf_token", ""),
            "pwa_enabled": settings.pwa_enabled,
            "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
        }
        context.update(locale_context(locale))
        return templates.TemplateResponse(
            request,
            "error.html",
            context,
            status_code=exc.status_code,
        )
    except Exception:
        return HTMLResponse(message, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    locale = request_locale(request)
    messages = {
        "missing": translate(locale, "error.required"),
        "string_too_short": translate(locale, "error.too_short"),
        "string_too_long": translate(locale, "error.too_long"),
        "greater_than": translate(locale, "error.greater"),
        "greater_than_equal": translate(locale, "error.too_small"),
        "less_than_equal": translate(locale, "error.too_large"),
        "decimal_parsing": translate(locale, "error.decimal"),
        "int_parsing": translate(locale, "error.integer"),
        "uuid_parsing": translate(locale, "error.uuid"),
        "url_parsing": translate(locale, "error.url"),
        "literal_error": translate(locale, "error.selection"),
    }
    if request.url.path.startswith("/api/"):
        errors = [
            {
                "field": ".".join(str(item) for item in error["loc"] if item != "body"),
                "message": messages.get(error["type"], translate(locale, "error.validation_short")),
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": translate(locale, "error.validation"),
                    "fields": errors,
                },
                "request_id": getattr(request.state, "request_id", None),
            },
        )
    context = {
        "request": request,
        "status_code": 422,
        "message": translate(locale, "error.validation_short"),
        "current_user": None,
        "csrf_token": "",
        "pwa_enabled": settings.pwa_enabled,
        "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
    }
    context.update(locale_context(locale))
    return templates.TemplateResponse(
        request,
        "error.html",
        context,
        status_code=422,
    )


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    with Redis.from_url(settings.redis_url, socket_timeout=1) as redis:
        redis.ping()
    active_storage_root(settings)
    return {"status": "ready"}


@app.get("/manifest.webmanifest")
def manifest(
    request: Request,
    lang: str | None = Query(default=None),
) -> JSONResponse:
    if not settings.pwa_enabled:
        raise HTTPException(status_code=404, detail="Die PWA-Funktion ist deaktiviert.")
    locale = normalize_locale(lang, fallback=None) or detect_browser_locale(
        request.headers.get("accept-language")
    )
    return JSONResponse(
        {
            "name": translate(locale, "app.name"),
            "short_name": translate(locale, "app.name"),
            "lang": locale,
            "start_url": settings.pwa_start_url,
            "scope": "/",
            "display": "standalone",
            "background_color": settings.pwa_background_color,
            "theme_color": settings.pwa_theme_color,
            "icons": [
                {
                    "src": frontend_assets.url("pwa/icon-192.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                },
                {
                    "src": frontend_assets.url("pwa/icon-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                },
                {
                    "src": frontend_assets.url("pwa/icon-maskable-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "shortcuts": [
                {"name": translate(locale, "pwa.shortcut.recipes"), "url": "/rezepte"},
                {"name": translate(locale, "pwa.shortcut.create"), "url": "/rezepte/neu"},
                {"name": translate(locale, "pwa.shortcut.import"), "url": "/importieren"},
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept-Language"},
    )


@app.get("/service-worker.js")
def service_worker() -> Response:
    if not settings.pwa_enabled:
        raise HTTPException(status_code=404, detail="Die PWA-Funktion ist deaktiviert.")
    content = frontend_assets.service_worker_path.read_bytes()
    return Response(
        content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/offline", response_class=HTMLResponse)
def offline_page(
    request: Request,
    lang: str | None = Query(default=None),
) -> HTMLResponse:
    locale = normalize_locale(lang, fallback=None) or detect_browser_locale(
        request.headers.get("accept-language")
    )
    context: dict[str, Any] = {
        "request": request,
        "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
    }
    context.update(locale_context(locale))
    return templates.TemplateResponse(
        request,
        "offline.html",
        context,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/", include_in_schema=False)
def root(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    destination = "/rezepte" if get_session(db, request) else "/login"
    return RedirectResponse(destination, status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_url: str = Query(default="/rezepte", alias="next"),
    db: Session = Depends(get_db),
) -> Response:
    if get_session(db, request):
        return RedirectResponse("/rezepte", status_code=303)
    safe_next = (
        next_url if next_url.startswith("/") and not next_url.startswith("//") else "/rezepte"
    )
    login_csrf_token = new_login_csrf_token()
    locale = detect_browser_locale(request.headers.get("accept-language"))
    context: dict[str, Any] = {
        "request": request,
        "next_url": safe_next,
        "login_csrf_token": login_csrf_token,
        "error": None,
        "pwa_enabled": settings.pwa_enabled,
        "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
    }
    context.update(locale_context(locale))
    response = templates.TemplateResponse(
        request,
        "login.html",
        context,
        headers={"Cache-Control": "no-store"},
    )
    set_login_csrf_cookie(response, login_csrf_token)
    return response


@app.post("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/rezepte"),
    login_csrf_token: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    require_login_csrf(request, login_csrf_token)
    normalized = email.strip().casefold()
    check_login_rate_limit(request, normalized)
    user = db.scalar(select(User).where(User.email == normalized))
    password_valid = verify_password(
        password, user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    )
    if user is None or not user.is_active or not password_valid:
        locale = detect_browser_locale(request.headers.get("accept-language"))
        replacement_csrf_token = new_login_csrf_token()
        context = {
            "request": request,
            "next_url": next_url,
            "login_csrf_token": replacement_csrf_token,
            "error": translate(locale, "login.invalid"),
            "email": email,
            "pwa_enabled": settings.pwa_enabled,
            "pwa_manifest_url": f"{frontend_assets.manifest_url}&lang={locale}",
        }
        context.update(locale_context(locale))
        login_response = templates.TemplateResponse(
            request,
            "login.html",
            context,
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
        set_login_csrf_cookie(login_response, replacement_csrf_token)
        return login_response
    clear_login_account_rate_limit(normalized)
    redirect_response = RedirectResponse(
        next_url if next_url.startswith("/") and not next_url.startswith("//") else "/rezepte",
        status_code=303,
    )
    if user.language is None:
        user.language = detect_browser_locale(request.headers.get("accept-language"))
    create_session(db, user, request, redirect_response)
    clear_login_csrf_cookie(redirect_response)
    db.commit()
    return redirect_response


@app.post("/logout")
async def logout_page(request: Request, db: Session = Depends(get_db)) -> Response:
    session = get_session(db, request)
    if session is not None:
        form = await request.form()
        supplied = str(form.get("_csrf", ""))
        if supplied != session.csrf_token:
            raise HTTPException(
                status_code=403, detail="Die Sicherheitsprüfung ist fehlgeschlagen."
            )
    response = RedirectResponse("/login", status_code=303)
    delete_session(db, request, response)
    db.commit()
    return response


@app.get("/konto", response_class=HTMLResponse)
def account_page(
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _template(request, db, "account/index.html")


@app.post("/konto")
async def update_account_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    session = get_session(db, request)
    if session is None:
        raise HTTPException(
            status_code=401, detail=translate(request_locale(request), "auth.required")
        )
    form = await request.form()
    if str(form.get("_csrf", "")) != session.csrf_token:
        raise HTTPException(
            status_code=403,
            detail=translate(request_locale(request, user), "auth.csrf_failed"),
        )
    raw_language = str(form.get("language", ""))
    language = normalize_locale(raw_language, fallback=None)
    if language is None or raw_language != language:
        raise HTTPException(
            status_code=422,
            detail=translate(request_locale(request, user), "account.invalid_language"),
        )
    user.language = language
    db.commit()
    return RedirectResponse("/konto?saved=1", status_code=303)


@app.get("/rezepte", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    user: User = Depends(current_user),
    q: str = Query(default="", max_length=300),
    category_ids: list[uuid.UUID] = Query(
        default=[],
        description=(
            "Kategorie-IDs; jede Auswahl umfasst ihre Unterkategorien, "
            "mehrere Auswahlen werden kombiniert."
        ),
    ),
    sort: str = Query(default="updated_desc"),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    recipe_kind: RecipeKind | None = None,
) -> HTMLResponse:
    recipes, total, pages, current_page = list_recipes(
        db,
        q=q,
        category_ids=category_ids,
        recipe_kind=recipe_kind,
        sort=sort,
        page=page,
        page_size=24,
    )
    template_name = _recipe_search_template(request)
    response = _template(
        request,
        db,
        template_name,
        {
            "recipes": recipes,
            "categories": [] if template_name == "recipes/_batch.html" else category_tree(db),
            "q": q,
            "selected_category_ids": {str(item) for item in category_ids},
            "selected_recipe_kind": recipe_kind,
            "sort": sort,
            "page": current_page,
            "pages": pages,
            "total": total,
            "favorite_recipe_ids": favorite_recipe_ids(db, user, (recipe.id for recipe in recipes)),
        },
    )
    response.headers["Vary"] = "X-Recipe-Results"
    return response


@app.get("/papierkorb", response_class=HTMLResponse)
def trash_page(
    request: Request,
    _: User = Depends(current_user),
    q: str = Query(default="", max_length=300),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipes, total, pages, current_page = list_recipes(
        db, q=q, sort="updated_desc", page=page, page_size=24, only_deleted=True
    )
    response = _template(
        request,
        db,
        _recipe_search_template(request),
        {
            "recipes": recipes,
            "categories": [],
            "q": q,
            "selected_category_ids": set(),
            "selected_recipe_kind": None,
            "sort": "updated_desc",
            "page": current_page,
            "pages": pages,
            "total": total,
            "trash_mode": True,
        },
    )
    response.headers["Vary"] = "X-Recipe-Results"
    return response


@app.get("/rezepte/neu", response_class=HTMLResponse)
def new_recipe_page(
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _template(
        request,
        db,
        "recipes/form.html",
        {"recipe": None, "categories": category_tree(db), "mode": "create"},
    )


@app.get("/rezepte/{recipe_id}", response_class=HTMLResponse)
def recipe_detail_page(
    recipe_id: uuid.UUID,
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = get_recipe(db, recipe_id)
    generation_job = get_active_image_generation_job(db, recipe.id)
    return _template(
        request,
        db,
        "recipes/detail.html",
        {
            "recipe": recipe,
            "image_generation_available": image_generation_available(settings),
            "image_generation_job": generation_job,
        },
    )


@app.get("/rezepte/{recipe_id}/bearbeiten", response_class=HTMLResponse)
def edit_recipe_page(
    recipe_id: uuid.UUID,
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = get_recipe(db, recipe_id)
    return _template(
        request,
        db,
        "recipes/form.html",
        {
            "recipe": recipe,
            "categories": category_tree(db),
            "mode": "edit",
        },
    )


@app.get("/rezepte/{recipe_id}/print", response_class=HTMLResponse)
def print_recipe_page(
    recipe_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    servings: float = Query(gt=0, le=100_000),
    include_comments: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = get_recipe(db, recipe_id)
    view = scaled_recipe_view(recipe, servings, request_locale(request, user))
    context = _page_context(request, db)
    context.update({**view, "include_comments": include_comments, "pdf_mode": False})
    return templates.TemplateResponse(
        request,
        "recipes/print.html",
        context,
    )


@app.get("/importieren", response_class=HTMLResponse)
def import_page(
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _template(request, db, "imports/index.html")


@app.get("/importieren/laufend", response_class=HTMLResponse)
def running_imports_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _page_context(request, db)
    batches = list(
        db.scalars(
            select(ImportBatch)
            .where(
                ImportBatch.created_by_user_id == user.id,
                ImportBatch.status.in_(("queued", "processing", "review")),
            )
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        )
    )
    context.update({"batches": batches})
    return templates.TemplateResponse(request, "imports/running.html", context)


@app.get("/importieren/verlauf", response_class=HTMLResponse)
def import_history_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _page_context(request, db)
    batches = list(
        db.scalars(
            select(ImportBatch)
            .options(selectinload(ImportBatch.jobs).selectinload(ImportJob.source_asset))
            .where(ImportBatch.created_by_user_id == user.id)
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        )
    )
    context.update({"batches": batches})
    return templates.TemplateResponse(request, "imports/history.html", context)


@app.get("/importieren/{batch_id}", response_class=HTMLResponse)
def import_batch_page(
    batch_id: uuid.UUID,
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _page_context(request, db)
    batch = db.scalar(
        select(ImportBatch)
        .options(
            selectinload(ImportBatch.jobs).selectinload(ImportJob.source_asset),
            selectinload(ImportBatch.jobs).selectinload(ImportJob.candidates),
        )
        .where(ImportBatch.id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Der Import wurde nicht gefunden.")
    user = cast(User, context["current_user"])
    if batch.created_by_user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Dieser Import gehört zu einem anderen Benutzer."
        )
    context.update({"batch": batch})
    return templates.TemplateResponse(request, "imports/batch.html", context)


@app.get("/kategorien", response_class=HTMLResponse)
def categories_page(
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _template(request, db, "categories.html", {"categories": category_tree(db)})


@app.get("/einstellungen", response_class=HTMLResponse)
def settings_page(
    request: Request,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _page_context(request, db)
    jobs = list(
        db.scalars(select(BackupRestoreJob).order_by(BackupRestoreJob.created_at.desc()).limit(20))
    )
    context.update({"jobs": jobs})
    return templates.TemplateResponse(request, "settings/index.html", context)
