from __future__ import annotations

import fcntl
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, cast

from fastapi import HTTPException, Request, Response
from fastapi.params import File
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.dependencies import admin_user, current_session
from app.auth.security import require_csrf
from app.config import get_settings
from app.database import get_db

FORM_BODY_LIMIT = 64 * 1024
MULTIPART_OVERHEAD = 1024 * 1024
UPLOAD_SLOTS = 2


class CompleteMultiPartParser(MultiPartParser):
    complete = False

    def on_end(self) -> None:
        self.complete = True

    async def parse(self) -> FormData:
        form = await super().parse()
        if not self.complete:
            raise MultiPartException("Incomplete multipart body")
        return form

    def close_files(self) -> None:
        # FormData only owns completed parts. Include unfinished parts on EOF,
        # disconnect, validation errors and cancellation as well.
        for file in self._files_to_close_on_error:
            file.close()


class BodyBudget:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.received = 0

    def consume(self, size: int) -> None:
        self.received += size
        if self.limit is not None and self.received > self.limit:
            raise HTTPException(status_code=413, detail="Die Anfrage ist zu groß.")


class FormBodyLimitMiddleware:
    """Count actual ASGI bytes, including unknown fields and chunked bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        content_type = (
            Request(scope).headers.get("content-type", "").split(";", 1)[0].lower().strip()
        )
        budget = BodyBudget(
            FORM_BODY_LIMIT
            if content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}
            else None
        )
        scope["upload_body_budget"] = budget

        async def limited_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                budget.consume(len(message.get("body", b"")))
            return message

        await self.app(scope, limited_receive, send)


def authorize_upload(request: Request, *, admin: bool) -> None:
    # Use the same checks as endpoint dependencies, but close the connection
    # before receiving the body. FastAPI normally parses File fields first.
    with contextmanager(get_db)() as db:
        session = current_session(request, db)
        require_csrf(request, session)
        if admin:
            admin_user(request, session.user)


def acquire_upload_slot(reserved_bytes: int) -> BinaryIO:
    # Advisory locks work across ASGI processes and are released on process
    # death. Keep the files: unlinking a held lock would permit another owner.
    directory = Path(tempfile.gettempdir()) / "recipes-upload-slots"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected: BinaryIO | None = None
    try:
        # Serialize reservation updates across processes. Active owners retain
        # their slot locks throughout parsing/processing; stale metadata from
        # completed or killed owners is ignored when the lock is available.
        with (directory / "admission.lock").open("a+b") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            outstanding = 0
            for index in range(UPLOAD_SLOTS):
                slot = (directory / f"{index}.lock").open("a+b")
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        slot.seek(0)
                        outstanding += int(slot.read(32))
                    finally:
                        slot.close()
                else:
                    if selected is None:
                        selected = slot
                    else:
                        slot.close()
            if selected is None:
                raise HTTPException(
                    status_code=503,
                    detail="Es laufen bereits zu viele Uploads. Bitte versuche es später erneut.",
                    headers={"Retry-After": "5"},
                )
            # Count active reservations conservatively even if some of their
            # bytes are already on disk. Do not charge small images for an
            # unrelated maximum-size restore.
            required = (
                outstanding + reserved_bytes + get_settings().storage_min_free_mb * 1024 * 1024
            )
            if shutil.disk_usage(directory).free < required:
                raise HTTPException(
                    status_code=507, detail="Nicht genügend freier Upload-Speicher."
                )
            selected.seek(0)
            selected.truncate()
            selected.write(str(reserved_bytes).encode("ascii"))
            selected.flush()
            return selected
    except BaseException:
        if selected is not None:
            selected.close()
        raise


class ProtectedUploadRoute(APIRoute):
    require_admin = False

    def upload_limit(self) -> int:
        return get_settings().max_upload_bytes + MULTIPART_OVERHEAD

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if self.body_field is None or not isinstance(self.body_field.field_info, File):
            return handler

        async def protected(request: Request) -> Response:
            await run_in_threadpool(authorize_upload, request, admin=self.require_admin)
            budget = cast(BodyBudget | None, request.scope.get("upload_body_budget"))
            if budget is None:
                raise RuntimeError("Upload routes require FormBodyLimitMiddleware")
            budget.limit = self.upload_limit()
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Ungültige Anfragegröße.") from exc
                if declared_size < 0:
                    raise HTTPException(status_code=400, detail="Ungültige Anfragegröße.")
                if declared_size > budget.limit:
                    raise HTTPException(status_code=413, detail="Die Anfrage ist zu groß.")
                # Reserve the declared length only when also enforcing it.
                budget.limit = declared_size
            slot = await run_in_threadpool(acquire_upload_slot, budget.limit)
            parser: CompleteMultiPartParser | None = None
            try:
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip()
                    == "multipart/form-data"
                ):
                    parser = CompleteMultiPartParser(
                        request.headers,
                        request.stream(),
                        max_files=20,
                        max_fields=32,
                        max_part_size=FORM_BODY_LIMIT,
                    )
                    try:
                        request._form = await parser.parse()
                    except MultiPartException as exc:
                        raise HTTPException(status_code=400, detail=exc.message) from exc
                    except HTTPException:
                        raise
                    except Exception as exc:
                        raise HTTPException(
                            status_code=400, detail="Ungültige Multipart-Anfrage."
                        ) from exc
                return await handler(request)
            finally:
                # FastAPI closes parsed forms after the route handler returns;
                # close explicitly before releasing the spool reservation.
                try:
                    await request.close()
                finally:
                    if parser is not None:
                        parser.close_files()
                    slot.close()

        return protected


class ImportUploadRoute(ProtectedUploadRoute):
    def upload_limit(self) -> int:
        return max(100 * 1024 * 1024, 20 * get_settings().max_upload_bytes) + MULTIPART_OVERHEAD


class RestoreUploadRoute(ProtectedUploadRoute):
    require_admin = True

    def upload_limit(self) -> int:
        return get_settings().max_backup_upload_bytes + MULTIPART_OVERHEAD
