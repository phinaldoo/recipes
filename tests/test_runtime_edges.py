from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app import cli, egress_proxy, main, renderer
from app.database import get_db
from app.imports.url_security import UnsafeURL
from app.models import BackupRestoreJob, ImportBatch, User


class FakeQuery:
    def __init__(self) -> None:
        self.deleted = False

    def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
        return self

    def delete(self, **_kwargs: object) -> int:
        self.deleted = True
        return 1


class FakeDatabase:
    def __init__(
        self,
        *,
        scalar_values: list[object | None] | None = None,
        scalars_values: list[list[object]] | None = None,
    ) -> None:
        self.scalar_values = scalar_values or []
        self.scalars_values = scalars_values or []
        self.added: list[object] = []
        self.query_result = FakeQuery()
        self.commits = 0
        self.executed: list[object] = []

    def __enter__(self) -> FakeDatabase:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def scalar(self, _statement: object) -> object | None:
        return self.scalar_values.pop(0) if self.scalar_values else None

    def scalars(self, _statement: object) -> list[object]:
        return self.scalars_values.pop(0) if self.scalars_values else []

    def query(self, _model: object) -> FakeQuery:
        return self.query_result

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                cast(Any, item).id = uuid.uuid4()

    def commit(self) -> None:
        self.commits += 1

    def execute(self, statement: object) -> None:
        self.executed.append(statement)


def make_user(*, role: str = "member", active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{role}-{uuid.uuid4()}@example.test",
        display_name=role.title(),
        password_hash="old-hash",
        role=role,
        is_active=active,
    )


def make_request(
    path: str = "/",
    *,
    method: str = "GET",
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_cli_create_and_list_users(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    create_db = FakeDatabase(scalar_values=[None])
    monkeypatch.setattr(cli, "SessionLocal", lambda: create_db)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _length: "generated-password")
    monkeypatch.setattr(cli, "hash_password", lambda value: f"hashed:{value}")

    result = runner.invoke(
        cli.cli,
        [
            "users",
            "create",
            "--email",
            " Alice@Example.TEST ",
            "--display-name",
            " Alice ",
            "--role",
            "admin",
            "--generate-password",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Einmaliges Startpasswort: generated-password" in result.output
    assert "alice@example.test wurde als admin angelegt" in result.output
    created = next(item for item in create_db.added if isinstance(item, User))
    assert created.email == "alice@example.test"
    assert created.display_name == "Alice"
    assert created.password_hash == "hashed:generated-password"
    assert create_db.commits == 1
    audit = next(item for item in create_db.added if item.__class__.__name__ == "AuditLog")
    assert cast(Any, audit).action == "user.create.cli"

    list_db = FakeDatabase(scalars_values=[[created, make_user(active=False)]])
    monkeypatch.setattr(cli, "SessionLocal", lambda: list_db)
    listed = runner.invoke(cli.cli, ["users", "list"])
    assert listed.exit_code == 0
    assert "alice@example.test\tAlice\tadmin\taktiv" in listed.output
    assert "deaktiviert" in listed.output

    monkeypatch.setattr(cli, "SessionLocal", lambda: FakeDatabase(scalars_values=[[]]))
    empty = runner.invoke(cli.cli, ["users", "list"])
    assert empty.exit_code == 0
    assert "Noch keine Benutzer vorhanden" in empty.output


@pytest.mark.parametrize(
    ("email", "message"),
    [
        ("missing-at.example", "gültige E-Mail-Adresse"),
        ("@example.test", "gültige E-Mail-Adresse"),
        ("alice@", "gültige E-Mail-Adresse"),
        (f"a@{'x' * 319}", "gültige E-Mail-Adresse"),
    ],
)
def test_cli_rejects_invalid_email(email: str, message: str) -> None:
    with pytest.raises(Exception, match=message):
        cli.normalize_email(email)


def test_cli_rejects_invalid_roles_duplicates_and_password_mismatch(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_role = runner.invoke(
        cli.cli,
        ["users", "create", "--email", "a@example.test", "--role", "owner"],
    )
    assert invalid_role.exit_code != 0
    assert "member oder admin" in invalid_role.output

    duplicate_db = FakeDatabase(scalar_values=[uuid.uuid4()])
    monkeypatch.setattr(cli, "SessionLocal", lambda: duplicate_db)
    duplicate = runner.invoke(
        cli.cli,
        ["users", "create", "--email", "a@example.test", "--generate-password"],
    )
    assert duplicate.exit_code != 0
    assert "existiert bereits" in duplicate.output

    mismatch = runner.invoke(
        cli.cli,
        ["users", "create", "--email", "b@example.test"],
        input="one-long-password\ntwo-long-password\n",
    )
    assert mismatch.exit_code != 0
    assert "stimmen nicht überein" in mismatch.output


def test_cli_account_mutations_invalidate_sessions_and_audit(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user()
    reset_db = FakeDatabase(scalar_values=[user])
    monkeypatch.setattr(cli, "SessionLocal", lambda: reset_db)
    monkeypatch.setattr(cli, "hash_password", lambda value: f"new:{value}")
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _length: "temporary-secret")

    reset = runner.invoke(
        cli.cli,
        ["users", "reset-password", "--email", user.email, "--generate-password"],
    )
    assert reset.exit_code == 0, reset.output
    assert user.password_hash == "new:temporary-secret"
    assert reset_db.query_result.deleted
    assert reset_db.commits == 1
    assert any(
        getattr(item, "action", None) == "user.password_reset.cli" for item in reset_db.added
    )

    role_db = FakeDatabase(scalar_values=[user])
    monkeypatch.setattr(cli, "SessionLocal", lambda: role_db)
    changed = runner.invoke(
        cli.cli, ["users", "set-role", "--email", user.email, "--role", "admin"]
    )
    assert changed.exit_code == 0, changed.output
    assert user.role == "admin"
    assert role_db.query_result.deleted
    assert any(getattr(item, "action", None) == "user.role_change.cli" for item in role_db.added)

    deactivate_db = FakeDatabase(scalar_values=[user, 2])
    monkeypatch.setattr(cli, "SessionLocal", lambda: deactivate_db)
    deactivated = runner.invoke(cli.cli, ["users", "deactivate", "--email", user.email])
    assert deactivated.exit_code == 0, deactivated.output
    assert user.is_active is False
    assert deactivate_db.query_result.deleted
    assert any(
        getattr(item, "action", None) == "user.deactivate.cli" for item in deactivate_db.added
    )


@pytest.mark.parametrize(
    ("command", "scalar_values", "message"),
    [
        (
            ["users", "reset-password", "--email", "none@example.test", "--generate-password"],
            [None],
            "nicht gefunden",
        ),
        (
            ["users", "set-role", "--email", "none@example.test", "--role", "admin"],
            [None],
            "nicht gefunden",
        ),
        (
            ["users", "deactivate", "--email", "none@example.test"],
            [None],
            "nicht gefunden",
        ),
    ],
)
def test_cli_missing_accounts(
    command: list[str],
    scalar_values: list[object | None],
    message: str,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "SessionLocal", lambda: FakeDatabase(scalar_values=scalar_values))
    result = runner.invoke(cli.cli, command)
    assert result.exit_code != 0
    assert message in result.output


def test_cli_protects_last_active_admin(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_user(role="admin")
    for command in (
        ["users", "set-role", "--email", admin.email, "--role", "member"],
        ["users", "deactivate", "--email", admin.email],
    ):
        db = FakeDatabase(scalar_values=[admin, 1])
        monkeypatch.setattr(cli, "SessionLocal", lambda db=db: db)
        result = runner.invoke(cli.cli, command)
        assert result.exit_code != 0
        assert "letzte aktive Administrator" in result.output
        assert db.commits == 0


def test_cli_backup_create_and_verify(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "backup.zip"
    manifest = SimpleNamespace(counts={"recipes": 7}, media_file_count=3)
    monkeypatch.setattr(cli, "SessionLocal", lambda: FakeDatabase())
    monkeypatch.setattr(cli, "export_backup", lambda _db: (archive, manifest, "a" * 64))
    created = runner.invoke(cli.cli, ["backups", "create"])
    assert created.exit_code == 0, created.output
    assert str(archive) in created.output
    assert "Rezepte: 7 · Dateien: 3" in created.output

    archive.write_bytes(b"zip")
    preflight = SimpleNamespace(
        application_version="1.0.0", media_file_count=3, media_total_bytes=123
    )
    verify = Mock(return_value=preflight)
    monkeypatch.setattr(cli, "preflight_backup", verify)
    checked = runner.invoke(cli.cli, ["backups", "verify", str(archive)])
    assert checked.exit_code == 0, checked.output
    assert "Backup ist gültig" in checked.output
    assert "Version 1.0.0 · 3 Dateien · 123 Bytes" in checked.output
    verify.assert_called_once_with(archive)


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "document", *, redirects: int = 0) -> None:
        redirected_from = None
        for _ in range(redirects):
            redirected_from = SimpleNamespace(redirected_from=redirected_from)
        self.request = SimpleNamespace(
            url=url,
            resource_type=resource_type,
            redirected_from=redirected_from,
        )
        self.aborted = 0
        self.continued = 0

    async def abort(self) -> None:
        self.aborted += 1

    async def continue_(self) -> None:
        self.continued += 1


class FakePage:
    def __init__(
        self,
        pdf: bytes,
        *,
        fail_goto: bool = False,
        navigation_status: int = 200,
    ) -> None:
        self.pdf_bytes = pdf
        self.fail_goto = fail_goto
        self.navigation_status = navigation_status
        self.guard: Callable[[Any], Awaitable[None]] | None = None
        self.routes: list[FakeRoute] = []
        self.goto_calls: list[tuple[str, str, int]] = []
        self.pdf_options: dict[str, object] | None = None

    async def goto(self, url: str, **options: object) -> SimpleNamespace:
        self.goto_calls.append(
            (url, cast(str, options["wait_until"]), cast(int, options["timeout"]))
        )
        if self.fail_goto:
            raise RuntimeError("navigation failed")
        assert self.guard is not None
        seed = [
            FakeRoute("https://example.test/socket", "websocket"),
            FakeRoute("http://private.test/secret"),
            FakeRoute("https://example.test/style.css", "stylesheet"),
        ]
        self.routes.extend(seed)
        for route in seed:
            await self.guard(route)
        for index in range(renderer.MAX_RENDER_REQUESTS):
            route = FakeRoute(f"https://example.test/asset-{index}.png", "image")
            self.routes.append(route)
            await self.guard(route)
        return SimpleNamespace(
            ok=200 <= self.navigation_status < 400,
            status=self.navigation_status,
        )

    async def pdf(self, **kwargs: object) -> bytes:
        self.pdf_options = kwargs
        return self.pdf_bytes


class FakeBrowserContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.route_pattern: str | None = None

    async def new_page(self) -> FakePage:
        return self.page

    async def route(self, pattern: str, guard: Callable[[Any], Awaitable[None]]) -> None:
        self.route_pattern = pattern
        self.page.guard = guard


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.context = FakeBrowserContext(page)
        self.context_options: dict[str, object] | None = None
        self.closed = False

    async def new_context(self, **kwargs: object) -> FakeBrowserContext:
        self.context_options = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_args: list[str] | None = None

    async def launch(self, *, args: list[str]) -> FakeBrowser:
        self.launch_args = args
        return self.browser


class FakePlaywrightManager:
    def __init__(self, chromium: FakeChromium) -> None:
        self.playwright = SimpleNamespace(chromium=chromium)

    async def __aenter__(self) -> SimpleNamespace:
        return self.playwright

    async def __aexit__(self, *_args: object) -> None:
        return None


def renderer_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pdf: bytes = b"%PDF-test",
    fail_goto: bool = False,
    navigation_status: int = 200,
    proxy_url: str = "http://egress:8888",
) -> tuple[TestClient, FakeBrowser, FakeChromium, FakePage]:
    page = FakePage(
        pdf,
        fail_goto=fail_goto,
        navigation_status=navigation_status,
    )
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    monkeypatch.setattr(
        renderer,
        "get_settings",
        lambda: SimpleNamespace(renderer_token="renderer-secret", renderer_proxy_url=proxy_url),
    )
    monkeypatch.setattr(renderer, "async_playwright", lambda: FakePlaywrightManager(chromium))

    async def dismiss_cookie_dialog(page_to_check: FakePage) -> bool:
        page_to_check.consent_checked = True
        return False

    monkeypatch.setattr(renderer, "_dismiss_cookie_dialog", dismiss_cookie_dialog)

    async def focus_recipe_content(page_to_check: FakePage) -> str:
        page_to_check.content_focused = True
        return "structured"

    monkeypatch.setattr(renderer, "_focus_recipe_content", focus_recipe_content)

    def validate(url: str) -> None:
        if url.startswith("file:"):
            raise UnsafeURL("unsafe URL shape")

    monkeypatch.setattr(renderer, "validate_http_url_shape", validate)
    return (
        TestClient(renderer.renderer_app, raise_server_exceptions=False),
        browser,
        chromium,
        page,
    )


def test_renderer_authentication_and_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    client, browser, chromium, _page = renderer_client(monkeypatch)
    try:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.post("/render/pdf", json={"url": "https://example.test"}).status_code == 401
        denied = client.post(
            "/render/pdf",
            json={"url": "file:///etc/passwd"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert denied.status_code == 422
        assert denied.json()["detail"] == "unsafe URL shape"
        assert chromium.launch_args is None
        assert browser.closed is False
    finally:
        client.close()


@pytest.mark.parametrize(
    "proxy_url",
    [
        "",
        "direct://",
        "socks5://egress:8888",
        "http://egress",
        "http://egress:8888/path",
        "http://user:secret@egress:8888",
        "http://worker:8888",
        " http://egress:8888",
    ],
)
def test_renderer_fails_closed_for_untrusted_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
    proxy_url: str,
) -> None:
    client, browser, chromium, _page = renderer_client(monkeypatch, proxy_url=proxy_url)
    try:
        response = client.post(
            "/render/pdf",
            json={"url": "https://example.test/recipe"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert response.status_code == 503
        assert "Egress-Proxy" in response.json()["detail"]
        assert chromium.launch_args is None
        assert browser.closed is False
    finally:
        client.close()


def test_renderer_guards_subrequests_and_returns_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    client, browser, chromium, page = renderer_client(monkeypatch)
    try:
        response = client.post(
            "/render/pdf",
            json={"url": "https://example.test/recipe"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert response.status_code == 200
        assert response.content == b"%PDF-test"
        assert response.headers["content-type"] == "application/pdf"
        assert browser.closed is True
        assert chromium.launch_args is not None
        assert "--proxy-server=http://egress:8888" in chromium.launch_args
        assert "--proxy-bypass-list=<-loopback>" in chromium.launch_args
        assert "--disable-quic" in chromium.launch_args
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in chromium.launch_args
        assert browser.context_options == {
            "accept_downloads": False,
            "java_script_enabled": True,
            "service_workers": "block",
        }
        assert browser.context.route_pattern == "**/*"
        assert page.goto_calls == [("https://example.test/recipe", "networkidle", 60_000)]
        assert page.consent_checked is True
        assert page.content_focused is True
        assert page.pdf_options is not None
        assert page.pdf_options["format"] == "A4"
        assert page.routes[0].aborted == 1
        assert page.routes[1].continued == 1
        assert page.routes[2].continued == 1
        assert page.routes[-1].aborted == 1
        redirect_loop = FakeRoute("https://example.test/final", redirects=11)
        run(page.guard(redirect_loop))  # type: ignore[arg-type]
        assert redirect_loop.aborted == 1
    finally:
        client.close()


class FakeConsentLocator:
    def __init__(self, *, matches: bool = False, visible: bool = True) -> None:
        self.matches = matches
        self.visible = visible
        self.clicked = False

    async def count(self) -> int:
        return int(self.matches)

    def nth(self, index: int) -> FakeConsentLocator:
        assert index == 0
        return self

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self, **_options: object) -> None:
        self.clicked = True


class FakeConsentFrame:
    def __init__(self, *, selector: str | None = None, label: str | None = None) -> None:
        self.selector = selector
        self.label = label
        self.locators: list[FakeConsentLocator] = []

    def locator(self, selector: str) -> FakeConsentLocator:
        locator = FakeConsentLocator(matches=selector == self.selector)
        self.locators.append(locator)
        return locator

    def get_by_role(self, role: str, *, name: object, exact: bool) -> FakeConsentLocator:
        assert role == "button"
        assert exact is True
        matches = bool(self.label and cast(Any, name).search(self.label))
        locator = FakeConsentLocator(matches=matches)
        self.locators.append(locator)
        return locator


class FakeConsentPage:
    def __init__(self, frame: FakeConsentFrame) -> None:
        self.frames = [frame]
        self.waits: list[int] = []
        self.load_state_calls: list[tuple[str, int]] = []

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    async def wait_for_load_state(self, state: str, **options: object) -> None:
        self.load_state_calls.append((state, cast(int, options["timeout"])))


@pytest.mark.parametrize(
    ("selector", "label"),
    [
        (None, "Einwilligen und weiter"),
        ("#onetrust-accept-btn-handler", None),
        (None, "Accept all cookies"),
    ],
)
def test_renderer_dismisses_recognized_cookie_dialogs(
    selector: str | None,
    label: str | None,
) -> None:
    frame = FakeConsentFrame(selector=selector, label=label)
    page = FakeConsentPage(frame)

    assert run(renderer._dismiss_cookie_dialog(cast(Any, page))) is True
    assert any(locator.clicked for locator in frame.locators)
    assert page.load_state_calls == [("networkidle", 5_000)]


def test_renderer_does_not_click_unrelated_page_buttons() -> None:
    frame = FakeConsentFrame(label="Kostenpflichtiges PUR-Abo abschließen")
    page = FakeConsentPage(frame)

    assert run(renderer._dismiss_cookie_dialog(cast(Any, page))) is False
    assert not any(locator.clicked for locator in frame.locators)
    assert page.load_state_calls == []


def test_renderer_size_limit_and_navigation_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    client, browser, _chromium, _page = renderer_client(
        monkeypatch, pdf=b"x" * (50 * 1024 * 1024 + 1), proxy_url=""
    )
    try:
        missing_proxy = client.post(
            "/render/pdf",
            json={"url": "https://example.test/recipe"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert missing_proxy.status_code == 503
        assert "Egress-Proxy" in missing_proxy.json()["detail"]
        assert browser.closed is False
    finally:
        client.close()

    oversized_client, oversized_browser, _chromium, _page = renderer_client(
        monkeypatch, pdf=b"x" * (50 * 1024 * 1024 + 1)
    )
    try:
        oversized = oversized_client.post(
            "/render/pdf",
            json={"url": "https://example.test/recipe"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert oversized.status_code == 413
        assert oversized_browser.closed is True
    finally:
        oversized_client.close()

    rejected_client, rejected_browser, _chromium, rejected_page = renderer_client(
        monkeypatch, navigation_status=403
    )
    try:
        rejected = rejected_client.post(
            "/render/pdf",
            json={"url": "http://127.0.0.1/"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert rejected.status_code == 422
        assert "Verbindungsaufbau abgelehnt" in rejected.json()["detail"]
        assert rejected_page.pdf_options is None
        assert rejected_browser.closed is True
    finally:
        rejected_client.close()

    failed_client, failed_browser, _chromium, _page = renderer_client(monkeypatch, fail_goto=True)
    try:
        failed = failed_client.post(
            "/render/pdf",
            json={"url": "https://example.test/recipe"},
            headers={"Authorization": "Bearer renderer-secret"},
        )
        assert failed.status_code == 500
        assert failed_browser.closed is True
    finally:
        failed_client.close()


class FakeReader:
    def __init__(self, lines: list[bytes] | None = None, chunks: list[bytes] | None = None) -> None:
        self.lines = list(lines or [])
        self.chunks = list(chunks or [])

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    async def read(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


def test_egress_pipe_stops_at_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_proxy, "MAX_STREAM_BYTES", 4)
    writer = FakeWriter()
    run(egress_proxy._pipe(FakeReader(chunks=[b"1234", b"5", b""]), writer))
    assert bytes(writer.buffer) == b"1234"
    assert writer.closed


class FakeWriter:
    def __init__(self, *, closing: bool = False, fail_drain: bool = False) -> None:
        self.buffer = bytearray()
        self.closed = closing
        self.fail_drain = fail_drain
        self.drain_count = 0
        self.waited = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        self.drain_count += 1
        if self.fail_drain:
            raise ConnectionError("closed")

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    async def wait_closed(self) -> None:
        self.waited = True
        if self.fail_drain:
            raise ConnectionError("closed")


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def test_egress_public_address_accepts_only_global_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLoop:
        def __init__(self, addresses: list[str]) -> None:
            self.addresses = addresses

        async def getaddrinfo(self, _host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
            assert type == 0
            return [(2, 1, 6, "", (address, port)) for address in self.addresses]

    monkeypatch.setattr(
        egress_proxy.asyncio, "get_running_loop", lambda: FakeLoop(["1.1.1.1", "8.8.8.8"])
    )
    assert run(egress_proxy._public_address("example.test", 443)) == "1.1.1.1"

    for addresses in ([], ["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["224.0.0.1"]):
        monkeypatch.setattr(
            egress_proxy.asyncio,
            "get_running_loop",
            lambda addresses=addresses: FakeLoop(addresses),
        )
        with pytest.raises(ValueError, match="Nicht-öffentliche"):
            run(egress_proxy._public_address("example.test", 443))


def test_egress_header_parser_and_pipe_limits() -> None:
    headers = run(egress_proxy._headers(FakeReader([b"Host: example.test\r\n", b"\r\n"])))
    assert headers == [b"Host: example.test\r\n"]

    with pytest.raises(ValueError, match="Ungültiger Header"):
        run(egress_proxy._headers(FakeReader([b"Bad: value\x00\r\n"])))
    with pytest.raises(ValueError, match="Ungültiger Header"):
        run(egress_proxy._headers(FakeReader([b"Bad: unterminated"])))
    with pytest.raises(ValueError, match="Header zu groß"):
        run(egress_proxy._headers(FakeReader([b"X: " + b"a" * egress_proxy.MAX_HEADER_BYTES])))
    with pytest.raises(ValueError, match="Zu viele Header"):
        run(egress_proxy._headers(FakeReader([b"X: y\r\n"] * egress_proxy.MAX_HEADER_COUNT)))

    writer = FakeWriter()
    run(egress_proxy._pipe(FakeReader(chunks=[b"one", b"two", b""]), writer))
    assert bytes(writer.buffer) == b"onetwo"
    assert writer.closed

    failed_writer = FakeWriter(fail_drain=True)
    run(egress_proxy._pipe(FakeReader(chunks=[b"ignored"]), failed_writer))
    assert failed_writer.closed


def test_egress_plain_http_filters_headers_and_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_reader = FakeReader(chunks=[b"HTTP/1.1 200 OK\r\n\r\nbody", b""])
    remote_writer = FakeWriter()
    client_writer = FakeWriter()

    async def public(_host: str, port: int) -> str:
        assert port == 80
        return "93.184.216.34"

    async def open_connection(address: str, port: int) -> tuple[FakeReader, FakeWriter]:
        assert (address, port) == ("93.184.216.34", 80)
        return remote_reader, remote_writer

    monkeypatch.setattr(egress_proxy, "_public_address", public)
    monkeypatch.setattr(egress_proxy.asyncio, "open_connection", open_connection)
    run(
        egress_proxy._plain_http(
            "GET",
            "http://example.test/path?q=one",
            "HTTP/1.1",
            [
                b"Host: example.test\r\n",
                b"Connection: keep-alive\r\n",
                b"Proxy-Connection: keep-alive\r\n",
                b"Accept: text/html\r\n",
            ],
            client_writer,
        )
    )
    outbound = bytes(remote_writer.buffer)
    assert outbound.startswith(b"GET /path?q=one HTTP/1.1\r\n")
    assert b"Host: example.test\r\n" in outbound
    assert b"Accept: text/html\r\n" in outbound
    assert b"keep-alive" not in outbound
    assert outbound.endswith(b"Connection: close\r\n\r\n")
    assert bytes(client_writer.buffer).endswith(b"body")

    for method, target in (
        ("POST", "http://example.test/"),
        ("GET", "https://example.test/"),
        ("HEAD", "http://example.test:8080/"),
    ):
        with pytest.raises(ValueError):
            run(egress_proxy._plain_http(method, target, "HTTP/1.1", [], FakeWriter()))


def test_egress_connect_tunnel_and_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_reader = FakeReader()
    remote_writer = FakeWriter()
    client_reader = FakeReader()
    client_writer = FakeWriter()
    pipe_calls: list[tuple[object, object]] = []

    async def public(host: str, port: int) -> str:
        assert (host, port) == ("example.test", 443)
        return "93.184.216.34"

    async def open_connection(_address: str, _port: int) -> tuple[FakeReader, FakeWriter]:
        return remote_reader, remote_writer

    async def pipe(reader: object, writer: object) -> None:
        pipe_calls.append((reader, writer))

    monkeypatch.setattr(egress_proxy, "_public_address", public)
    monkeypatch.setattr(egress_proxy.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(egress_proxy, "_pipe", pipe)
    run(egress_proxy._connect_tunnel("example.test:443", client_reader, client_writer))
    assert bytes(client_writer.buffer) == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    assert pipe_calls == [(client_reader, remote_writer), (remote_reader, client_writer)]

    for target in ("", "example.test:80"):
        with pytest.raises(ValueError, match="Port 443"):
            run(egress_proxy._connect_tunnel(target, FakeReader(), FakeWriter()))


def test_egress_handle_client_health_forwarding_and_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_writer = FakeWriter()
    run(
        egress_proxy.handle_client(
            FakeReader([b"GET /health/live HTTP/1.1\r\n", b"\r\n"]), health_writer
        )
    )
    assert b"200 OK" in health_writer.buffer
    assert b'{"status":"ok"}' in health_writer.buffer
    assert health_writer.closed and health_writer.waited

    forwarded: list[tuple[str, str, str, list[bytes]]] = []

    async def plain(
        method: str,
        target: str,
        version: str,
        headers: list[bytes],
        _writer: object,
    ) -> None:
        forwarded.append((method, target, version, headers))

    monkeypatch.setattr(egress_proxy, "_plain_http", plain)
    get_writer = FakeWriter()
    run(
        egress_proxy.handle_client(
            FakeReader([b"HEAD http://example.test/ HTTP/1.1\r\n", b"X: y\r\n", b"\r\n"]),
            get_writer,
        )
    )
    assert forwarded == [("HEAD", "http://example.test/", "HTTP/1.1", [b"X: y\r\n"])]

    tunnel_targets: list[str] = []

    async def tunnel(target: str, _reader: object, _writer: object) -> None:
        tunnel_targets.append(target)

    monkeypatch.setattr(egress_proxy, "_connect_tunnel", tunnel)
    tunnel_writer = FakeWriter()
    run(
        egress_proxy.handle_client(
            FakeReader([b"CONNECT example.test:443 HTTP/1.1\r\n", b"\r\n"]), tunnel_writer
        )
    )
    assert tunnel_targets == ["example.test:443"]

    for request_line in (
        b"malformed\r\n",
        b"GET " + b"x" * 8192 + b" HTTP/1.1\r\n",
        b"\xff invalid ascii\r\n",
    ):
        rejected = FakeWriter()
        run(egress_proxy.handle_client(FakeReader([request_line, b"\r\n"]), rejected))
        assert b"403 Forbidden" in rejected.buffer
        assert rejected.closed

    already_closed = FakeWriter(closing=True)
    run(egress_proxy.handle_client(FakeReader([b"bad\r\n"]), already_closed))
    assert already_closed.buffer == b""


def test_main_format_startup_readiness_and_maintenance_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = {"locale": "de"}
    assert main.format_datetime(context, None) == "–"
    formatted = main.format_datetime(context, datetime(2026, 8, 29, 12, 30, tzinfo=UTC))
    assert formatted.startswith("29.08.2026 · ")

    startup_calls: list[str] = []
    monkeypatch.setattr(
        main.settings.__class__,
        "ensure_directories",
        lambda _settings: startup_calls.append("dirs"),
    )
    monkeypatch.setattr(main, "active_storage_root", lambda _settings: startup_calls.append("root"))
    monkeypatch.setattr(
        main, "recover_interrupted_restore", lambda _settings: startup_calls.append("recover")
    )
    monkeypatch.setattr(
        main, "cleanup_retained_files", lambda _settings: startup_calls.append("cleanup")
    )
    monkeypatch.setattr(
        main,
        "cleanup_terminal_import_sources",
        lambda _settings: startup_calls.append("import-cleanup"),
    )
    main.startup()
    assert startup_calls == ["dirs", "root", "recover", "cleanup", "import-cleanup"]

    class RedisContext:
        def __init__(self, maintenance: bool = False) -> None:
            self.maintenance = maintenance
            self.pinged = False

        def __enter__(self) -> RedisContext:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _key: str) -> str | None:
            return "1" if self.maintenance else None

        def ping(self) -> bool:
            self.pinged = True
            return True

    ready_redis = RedisContext()
    monkeypatch.setattr(main.Redis, "from_url", lambda *_args, **_kwargs: ready_redis)
    db = FakeDatabase()
    assert main.health_ready(cast(Session, db)) == {"status": "ready"}
    assert len(db.executed) == 1
    assert ready_redis.pinged

    main.app.dependency_overrides[get_db] = lambda: db
    maintenance = RedisContext(maintenance=True)
    monkeypatch.setattr(main.Redis, "from_url", lambda *_args, **_kwargs: maintenance)
    client = TestClient(main.app, raise_server_exceptions=False)
    try:
        paused = client.post("/login", data={"email": "x@y.de", "password": "anything"})
        assert paused.status_code == 503
        assert paused.headers["retry-after"] == "30"
        assert "wiederhergestellt" in paused.json()["detail"]
    finally:
        client.close()
        main.app.dependency_overrides.clear()


def test_main_dispatcher_requeues_all_work_and_recovers_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale_id = uuid.uuid4()
    queued_id = uuid.uuid4()
    stale_image_id = uuid.uuid4()
    queued_image_id = uuid.uuid4()
    export_job = BackupRestoreJob(id=uuid.uuid4(), operation="export", status="queued")
    restore_job = BackupRestoreJob(
        id=uuid.uuid4(),
        operation="restore",
        status="queued",
        archive_filename="restore.zip",
    )
    ignored_job = BackupRestoreJob(id=uuid.uuid4(), operation="restore", status="queued")
    db = FakeDatabase(
        scalars_values=[
            [queued_id],
            [export_job, restore_job, ignored_job],
            [queued_image_id],
        ]
    )
    monkeypatch.setattr(main, "SessionLocal", lambda: db)

    async def immediate_to_thread(function: Callable[..., Any], *args: object) -> Any:
        return function(*args)

    monkeypatch.setattr(main.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(main, "recover_pending_restore", lambda: False)
    monkeypatch.setattr(main, "requeue_stale_imports", lambda: [stale_id])
    monkeypatch.setattr(main, "requeue_stale_image_generation_jobs", lambda: [stale_image_id])
    monkeypatch.setattr(main, "requeue_stale_maintenance_jobs", lambda: [])
    cleanup = Mock()
    import_cleanup = Mock()
    monkeypatch.setattr(main, "cleanup_retained_files", cleanup)
    monkeypatch.setattr(main, "cleanup_terminal_import_sources", import_cleanup)
    monkeypatch.setattr(main, "database_maintenance_shared_guard", nullcontext)
    import_send = Mock()
    image_send = Mock()
    backup_send = Mock()
    restore_send = Mock()
    monkeypatch.setattr(main.import_job_task, "send", import_send)
    monkeypatch.setattr(main.image_generation_task, "send", image_send)
    monkeypatch.setattr(main.backup_task, "send", backup_send)
    monkeypatch.setattr(main.restore_task, "send", restore_send)
    monkeypatch.setattr(main.settings, "backup_temp_root", tmp_path)

    async def stop_after_iteration(_seconds: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        run(main._stale_import_loop())
    assert {call.args[0] for call in import_send.call_args_list} == {
        str(stale_id),
        str(queued_id),
    }
    assert {call.args[0] for call in image_send.call_args_list} == {
        str(stale_image_id),
        str(queued_image_id),
    }
    backup_send.assert_called_once_with(str(export_job.id))
    restore_send.assert_called_once_with(str(restore_job.id), str(tmp_path / "restore.zip"))
    cleanup.assert_called_once_with(main.settings)
    import_cleanup.assert_called_once_with(main.settings)

    monkeypatch.setattr(main, "requeue_stale_imports", Mock(side_effect=RuntimeError("db down")))
    logged = Mock()
    monkeypatch.setattr(main.logger, "exception", logged)
    with pytest.raises(asyncio.CancelledError):
        run(main._stale_import_loop())
    logged.assert_called_once()


def test_main_reaper_task_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "stale_import_reaper", None)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def loop() -> None:
            await blocker.wait()

        monkeypatch.setattr(main, "_stale_import_loop", loop)
        await main.start_stale_import_reaper()
        task = main.stale_import_reaper
        assert task is not None and not task.done()
        await main.stop_stale_import_reaper()
        assert task.cancelled()

    run(scenario())


def test_main_page_context_templates_and_exception_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request("/rezepte")
    db = cast(Session, FakeDatabase())
    monkeypatch.setattr(main, "get_session", lambda *_args: None)
    with pytest.raises(HTTPException) as missing:
        main._page_context(request, db)
    assert missing.value.status_code == 401

    user = make_user()
    session = SimpleNamespace(user=user, csrf_token="csrf")
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    context = main._page_context(request, db)
    assert context["current_user"] is user
    assert context["csrf_token"] == "csrf"
    assert request.state.user is user

    template_response = Mock(return_value=Response("rendered", media_type="text/html"))
    monkeypatch.setattr(main.templates, "TemplateResponse", template_response)
    rendered = main._template(request, db, "example.html", {"extra": 1}, status_code=201)
    assert rendered.status_code == 200
    passed_context = template_response.call_args.args[2]
    assert passed_context["extra"] == 1
    assert template_response.call_args.kwargs["status_code"] == 201

    template_response.side_effect = RuntimeError("template missing")
    fallback = run(main.http_exception_handler(request, HTTPException(418, "Teekanne")))
    assert fallback.status_code == 418
    assert fallback.body == b"Teekanne"


def test_main_root_login_and_logout_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDatabase()
    session = SimpleNamespace(user=make_user(), csrf_token="csrf-token")
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    assert main.root(make_request(), cast(Session, db)).headers["location"] == "/rezepte"
    assert main.login_page(make_request("/login"), "/rezepte", cast(Session, db)).status_code == 303

    monkeypatch.setattr(main, "get_session", lambda *_args: None)
    assert main.root(make_request(), cast(Session, db)).headers["location"] == "/login"

    user = make_user()
    login_db = FakeDatabase(scalar_values=[user])
    rate_limit = Mock()
    clear_rate_limit = Mock()
    session_create = Mock()
    monkeypatch.setattr(main, "check_login_rate_limit", rate_limit)
    monkeypatch.setattr(main, "clear_login_account_rate_limit", clear_rate_limit)
    monkeypatch.setattr(main, "verify_password", lambda *_args: True)
    monkeypatch.setattr(main, "create_session", session_create)
    login_csrf_token = "login-csrf-token"
    login_request = make_request(
        "/login",
        method="POST",
        headers={
            "origin": main.settings.app_base_url,
            "cookie": f"{main.settings.login_csrf_cookie_name}={login_csrf_token}",
        },
    )
    redirect = main.login_form(
        login_request,
        "  MEMBER@EXAMPLE.TEST ",
        "correct password",
        "//evil.test",
        login_csrf_token,
        cast(Session, login_db),
    )
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/rezepte"
    rate_limit.assert_called_once()
    clear_rate_limit.assert_called_once_with("member@example.test")
    assert not login_db.query_result.deleted
    assert login_db.commits == 1
    session_create.assert_called_once()

    invalid_db = FakeDatabase(scalar_values=[None])
    monkeypatch.setattr(main, "verify_password", lambda password, digest: False)
    failed = main.login_form(
        login_request,
        "missing@example.test",
        "wrong",
        "/rezepte",
        login_csrf_token,
        cast(Session, invalid_db),
    )
    assert failed.status_code == 401
    assert b"falsch" in failed.body

    delete = Mock()
    monkeypatch.setattr(main, "delete_session", delete)
    monkeypatch.setattr(main, "get_session", lambda *_args: None)
    logout_db = FakeDatabase()
    logged_out = run(
        main.logout_page(make_request("/logout", method="POST"), cast(Session, logout_db))
    )
    assert logged_out.status_code == 303
    assert logout_db.commits == 1
    delete.assert_called_once()

    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    encoded = urlencode({"_csrf": "wrong"}).encode()
    bad_request = make_request(
        "/logout",
        method="POST",
        body=encoded,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(HTTPException, match="Sicherheitsprüfung"):
        run(main.logout_page(bad_request, cast(Session, FakeDatabase())))


def test_main_recipe_and_admin_pages_delegate_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request("/rezepte")
    user = make_user()
    db = FakeDatabase()
    recipe = SimpleNamespace(id=uuid.uuid4())
    template = Mock(return_value=Response("ok", media_type="text/html"))
    monkeypatch.setattr(main, "_template", template)
    monkeypatch.setattr(main, "list_recipes", lambda *_args, **_kwargs: ([recipe], 1, 2, 1))
    monkeypatch.setattr(main, "category_tree", lambda _db: ["tree"])
    monkeypatch.setattr(main, "get_recipe", lambda *_args: recipe)

    category_id = uuid.uuid4()
    main.recipes_page(request, user, "suppe", [category_id], "title_asc", 1, cast(Session, db))
    context = template.call_args.args[3]
    assert context["selected_category_ids"] == {str(category_id)}
    assert context["categories"] == ["tree"]

    main.trash_page(request, user, "alt", 2, cast(Session, db))
    assert template.call_args.args[3]["trash_mode"] is True
    main.new_recipe_page(request, user, cast(Session, db))
    assert template.call_args.args[2] == "recipes/form.html"
    main.recipe_detail_page(recipe.id, request, user, cast(Session, db))
    assert template.call_args.args[2] == "recipes/detail.html"
    main.import_page(request, user, cast(Session, db))
    main.categories_page(request, user, cast(Session, db))

    main.edit_recipe_page(recipe.id, request, user, cast(Session, db))
    edit_context = template.call_args.args[3]
    assert edit_context["recipe"] is recipe
    assert edit_context["categories"] == ["tree"]
    assert edit_context["mode"] == "edit"


def test_main_print_import_batch_and_settings_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user()
    admin = make_user(role="admin")
    request = make_request("/importieren")
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(id=recipe_id)
    session = SimpleNamespace(user=user, csrf_token="csrf")
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    monkeypatch.setattr(main, "get_recipe", lambda *_args: recipe)
    monkeypatch.setattr(main, "scaled_recipe_view", lambda *_args: {"recipe": recipe})
    template_response = Mock(return_value=Response("ok", media_type="text/html"))
    monkeypatch.setattr(main.templates, "TemplateResponse", template_response)

    main.print_recipe_page(recipe_id, request, user, 6, True, cast(Session, FakeDatabase()))
    print_context = template_response.call_args.args[2]
    assert print_context["include_comments"] is True
    assert print_context["pdf_mode"] is False

    batch_id = uuid.uuid4()
    with pytest.raises(HTTPException) as missing:
        main.import_batch_page(
            batch_id, request, user, cast(Session, FakeDatabase(scalar_values=[None]))
        )
    assert missing.value.status_code == 404

    other_batch = ImportBatch(
        id=batch_id, created_by_user_id=uuid.uuid4(), status="queued", total_jobs=0
    )
    with pytest.raises(HTTPException) as forbidden:
        main.import_batch_page(
            batch_id,
            request,
            user,
            cast(Session, FakeDatabase(scalar_values=[other_batch])),
        )
    assert forbidden.value.status_code == 403

    monkeypatch.setattr(
        main, "get_session", lambda *_args: SimpleNamespace(user=admin, csrf_token="x")
    )
    main.import_batch_page(
        batch_id,
        request,
        admin,
        cast(Session, FakeDatabase(scalar_values=[other_batch])),
    )
    assert template_response.call_args.args[1] == "imports/batch.html"

    active_batch = ImportBatch(
        id=uuid.uuid4(), created_by_user_id=user.id, status="processing", total_jobs=2
    )
    main.running_imports_page(
        request,
        user,
        cast(Session, FakeDatabase(scalars_values=[[active_batch]])),
    )
    assert template_response.call_args.args[1] == "imports/running.html"
    assert template_response.call_args.args[2]["batches"] == [active_batch]

    job = BackupRestoreJob(id=uuid.uuid4(), operation="export", status="completed")
    settings_db = FakeDatabase(scalars_values=[[job]])
    main.settings_page(request, admin, cast(Session, settings_db))
    settings_context = template_response.call_args.args[2]
    assert settings_context["jobs"] == [job]
