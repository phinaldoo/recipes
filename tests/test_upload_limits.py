from __future__ import annotations

import asyncio
import fcntl
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI, File, Form, UploadFile
from starlette import formparsers

from app import upload_limits
from app.api import imports, media, settings
from app.config import get_settings
from app.upload_limits import FormBodyLimitMiddleware, ProtectedUploadRoute


def request(app, path, chunks, headers=()):  # type: ignore[no-untyped-def]
    messages = []
    reads = 0
    iterator = iter(chunks)

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        try:
            body = next(iterator)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": body, "more_body": True}

    async def send(message):  # type: ignore[no-untyped-def]
        messages.append(message)

    asyncio.run(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": path,
                "raw_path": path.encode(),
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "headers": list(headers),
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 1234),
            },
            receive,
            send,
        )
    )
    return next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    ), reads


@pytest.fixture
def authenticated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    session = SimpleNamespace(
        user=SimpleNamespace(role="member", is_active=True),
        csrf_token="csrf-test",
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
        user_agent_hash=None,
    )
    db = Mock()
    db.scalar.return_value = session

    def database():  # type: ignore[no-untyped-def]
        yield db

    monkeypatch.setattr(upload_limits, "get_db", database)
    monkeypatch.setattr(upload_limits.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        upload_limits.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10**12)
    )
    return session


def auth_headers():  # type: ignore[no-untyped-def]
    return [
        (b"cookie", f"{get_settings().session_cookie_name}=session-test".encode()),
        (b"x-csrf-token", b"csrf-test"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/imports/json",
        "/imports/files",
        "/recipes/00000000-0000-0000-0000-000000000001/images",
        "/settings/restores/preflight",
    ],
)
@pytest.mark.parametrize("identity", ["anonymous", "bad-csrf", "inactive"])
def test_real_upload_routes_reject_before_reading_body(
    authenticated, path: str, identity: str
) -> None:  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.add_middleware(FormBodyLimitMiddleware)
    for router in (imports.router, media.router, settings.router):
        app.include_router(router)
    headers = auth_headers()
    if identity == "anonymous":
        headers = []
    elif identity == "bad-csrf":
        headers = headers[:1]
    else:
        authenticated.user.is_active = False
    headers.append((b"content-type", b"multipart/form-data; boundary=upload"))
    status, reads = request(app, path, [b"untrusted body"], headers)
    assert status == (403 if identity == "bad-csrf" else 401)
    assert reads == 0


def test_restore_checks_admin_before_body(authenticated) -> None:  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.add_middleware(FormBodyLimitMiddleware)
    app.include_router(settings.router)
    status, reads = request(app, "/settings/restores/preflight", [b"body"], auth_headers())
    assert (status, reads) == (403, 0)


def upload_app(limit: int) -> FastAPI:
    class TestRoute(ProtectedUploadRoute):
        def upload_limit(self) -> int:
            return limit

    app = FastAPI()
    app.add_middleware(FormBodyLimitMiddleware)
    router = APIRouter(route_class=TestRoute)

    @router.post("/upload")
    async def upload(files: list[UploadFile] = File(...), caption: str = Form("")) -> dict:
        assert caption == "Dinner"
        return {"sizes": [len(await file.read()) for file in files]}

    @app.post("/login")
    async def login(name: str = Form("")) -> dict:
        return {"name": name}

    app.include_router(router)
    return app


def multipart(*sizes: int, field: str = "files"):  # type: ignore[no-untyped-def]
    for size in sizes:
        yield f'--upload\r\nContent-Disposition: form-data; name="{field}"; filename="test.bin"\r\n\r\n'.encode()
        for offset in range(0, size, 16384):
            yield b"x" * min(16384, size - offset)
        yield b"\r\n"
    yield b'--upload\r\nContent-Disposition: form-data; name="caption"\r\n\r\nDinner\r\n--upload--\r\n'


@pytest.mark.parametrize("declared_length", [None, b"1228800"])
def test_streamed_limit_closes_partial_spools(
    authenticated, monkeypatch: pytest.MonkeyPatch, declared_length: bytes | None
) -> None:  # type: ignore[no-untyped-def]
    spools = []
    original = formparsers.SpooledTemporaryFile

    def spool(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        spools.append(result)
        return result

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    app = upload_app(1200 * 1024)
    headers = auth_headers() + [(b"content-type", b"multipart/form-data; boundary=upload")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length))
    status, _ = request(app, "/upload", multipart(1500 * 1024, field="ignored"), headers)
    assert status == 413
    assert spools and all(spool.closed for spool in spools)
    # An aborted parse must also release its concurrency slot.
    slot = upload_limits.acquire_upload_slot(10000)
    slot.close()


def test_valid_multi_file_upload_and_form_fields(authenticated) -> None:  # type: ignore[no-untyped-def]
    app = upload_app(10000)
    status, reads = request(
        app,
        "/upload",
        multipart(*([10] * 20)),
        auth_headers() + [(b"content-type", b"multipart/form-data; boundary=upload")],
    )
    assert status == 200
    assert reads > 0


def test_non_upload_form_cannot_spool_unexpected_file(authenticated) -> None:  # type: ignore[no-untyped-def]
    status, _ = request(
        upload_app(10**7),
        "/login",
        multipart(2 * 1024 * 1024),
        [(b"content-type", b"multipart/form-data; boundary=upload")],
    )
    assert status == 413


def test_upload_concurrency_is_bounded_across_lock_owners(authenticated) -> None:  # type: ignore[no-untyped-def]
    directory = Path(tempfile.gettempdir()) / "recipes-upload-slots"
    directory.mkdir()
    held = [
        (directory / f"{index}.lock").open("a+b") for index in range(upload_limits.UPLOAD_SLOTS)
    ]
    try:
        for file in held:
            file.write(b"10000")
            file.flush()
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status, reads = request(upload_app(10000), "/upload", multipart(10), auth_headers())
        assert (status, reads) == (503, 0)
    finally:
        for file in held:
            file.close()


def test_insufficient_disk_rejects_before_body(
    authenticated, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(upload_limits.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    status, reads = request(upload_app(10000), "/upload", multipart(10), auth_headers())
    assert (status, reads) == (507, 0)


def test_small_upload_does_not_reserve_maximum_restore_size(
    authenticated, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        upload_limits.shutil, "disk_usage", lambda _path: SimpleNamespace(free=2 * 1024**3)
    )
    body = b"".join(multipart(10))
    status, _ = request(
        upload_app(20 * 1024**3),
        "/upload",
        [body],
        auth_headers()
        + [
            (b"content-type", b"multipart/form-data; boundary=upload"),
            (b"content-length", str(len(body)).encode()),
        ],
    )
    assert status == 200


def test_admission_accounts_for_outstanding_reservations(
    authenticated, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    reserve = get_settings().storage_min_free_mb * 1024 * 1024
    monkeypatch.setattr(
        upload_limits.shutil, "disk_usage", lambda _path: SimpleNamespace(free=reserve + 15000)
    )
    first = upload_limits.acquire_upload_slot(10000)
    try:
        with pytest.raises(upload_limits.HTTPException) as caught:
            upload_limits.acquire_upload_slot(10000)
        assert caught.value.status_code == 507
    finally:
        first.close()
    next_slot = upload_limits.acquire_upload_slot(10000)
    next_slot.close()


def test_understated_content_length_cannot_exceed_reservation(authenticated) -> None:  # type: ignore[no-untyped-def]
    status, _ = request(
        upload_app(10000),
        "/upload",
        multipart(10),
        auth_headers()
        + [
            (b"content-type", b"multipart/form-data; boundary=upload"),
            (b"content-length", b"1"),
        ],
    )
    assert status == 413


def test_incomplete_multipart_closes_unfinished_files(
    authenticated, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    spools = []
    original = formparsers.SpooledTemporaryFile

    def spool(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        spools.append(result)
        return result

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    chunks = list(multipart(1300 * 1024))[:-2]
    status, _ = request(
        upload_app(2 * 1024**2),
        "/upload",
        chunks,
        auth_headers()
        + [
            (b"content-type", b"multipart/form-data; boundary=upload"),
        ],
    )
    assert status == 400
    assert spools and all(spool.closed for spool in spools)


def test_malformed_multipart_remains_a_client_error(authenticated) -> None:  # type: ignore[no-untyped-def]
    status, _ = request(
        upload_app(10000),
        "/upload",
        [b"bad multipart"],
        auth_headers()
        + [
            (b"content-type", b"multipart/form-data; boundary=upload"),
        ],
    )
    assert status == 400
