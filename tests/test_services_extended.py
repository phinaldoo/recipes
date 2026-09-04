from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.requests import Request
from starlette.responses import Response

from app.auth import security
from app.config import Settings
from app.models import Category, MediaAsset, Recipe, RecipeCategory, RecipeImage, User
from app.pdf_backend import PasswordProtectedPDF, PDFInfo
from app.schemas.recipe import CategoryCreate, CategoryUpdate, ImageMetadataInput
from app.services import categories, media, storage
from app.services.storage import InvalidUpload, StoredFile


class ScalarCollection:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)

    def all(self) -> list[Any]:
        return self.values


class FakeDB:
    def __init__(
        self,
        *,
        scalar_results: list[Any] | None = None,
        scalars_results: list[list[Any]] | None = None,
        get_result: Any = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.scalars_results = list(scalars_results or [])
        self.get_result = get_result
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.flushes = 0
        self.commits = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    def delete(self, value: Any) -> None:
        self.deleted.append(value)

    def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def scalar(self, _statement: Any) -> Any:
        return self.scalar_results.pop(0) if self.scalar_results else None

    def scalars(self, _statement: Any) -> ScalarCollection:
        values = self.scalars_results.pop(0) if self.scalars_results else []
        return ScalarCollection(values)

    def get(self, _model: Any, _identifier: Any) -> Any:
        if callable(self.get_result):
            return self.get_result(_model, _identifier)
        return self.get_result


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        storage_root=tmp_path / "storage",
        backup_temp_root=tmp_path / "backups",
        **overrides,
    )


def request_for(
    *,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("192.0.2.10", 4242),
) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if cookie:
        raw_headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw_headers,
            "client": client,
            "server": ("example.test", 443),
        }
    )


def png_bytes(size: tuple[int, int] = (24, 18)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, color=(35, 91, 69, 180)).save(output, format="PNG")
    return output.getvalue()


def pdf_bytes() -> bytes:
    content = b"BT /F1 12 Tf 20 50 Td (Test) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(document)


def make_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="test@example.test",
        display_name="Test",
        password_hash="not-used",
        role="member",
        is_active=True,
    )


def make_asset(
    *,
    kind: str = "recipe_image",
    mime_type: str = "image/png",
    storage_key: str | None = None,
) -> MediaAsset:
    return MediaAsset(
        id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        kind=kind,
        storage_key=storage_key or f"images/{uuid.uuid4()}.png",
        original_filename="bild.png",
        mime_type=mime_type,
        byte_size=123,
        sha256="a" * 64,
        width=24 if mime_type.startswith("image/") else None,
        height=18 if mime_type.startswith("image/") else None,
    )


def make_recipe(images: list[RecipeImage] | None = None) -> Recipe:
    return Recipe(
        id=uuid.uuid4(),
        title="Testrezept",
        slug=f"test-{uuid.uuid4()}",
        base_servings=4,
        serving_label="Personen",
        status="active",
        search_document="",
        images=images or [],
    )


def make_category(name: str, *, parent_id: uuid.UUID | None = None) -> Category:
    return Category(
        id=uuid.uuid4(),
        parent_id=parent_id,
        name=name,
        normalized_name=name.casefold(),
        slug=name.casefold(),
        position=0,
        origin="manual",
        children=[],
        recipe_links=[],
    )


def test_recipe_expanded_categories_are_parent_first_and_deduplicated() -> None:
    baking = make_category("Backen")
    cake = make_category("Kuchen", parent_id=baking.id)
    cake.parent = baking
    nut_cake = make_category("Nusskuchen", parent_id=cake.id)
    nut_cake.parent = cake
    alcohol_cake = make_category("Kuchen mit Alkohol", parent_id=cake.id)
    alcohol_cake.parent = cake
    chocolate_cake = make_category("Schokoladenkuchen", parent_id=cake.id)
    chocolate_cake.parent = cake
    desserts = make_category("Desserts")
    egg_liqueur = make_category("Eierlikör", parent_id=desserts.id)
    egg_liqueur.parent = desserts
    recipe = make_recipe()
    assigned = [cake, nut_cake, alcohol_cake, egg_liqueur, chocolate_cake]
    recipe.category_links = [
        RecipeCategory(
            recipe_id=recipe.id,
            category_id=category.id,
            category=category,
        )
        for category in assigned
    ]

    assert [category.name for category in recipe.expanded_categories] == [
        "Backen",
        "Kuchen",
        "Nusskuchen",
        "Kuchen mit Alkohol",
        "Desserts",
        "Eierlikör",
        "Schokoladenkuchen",
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Authentication and request security


def test_password_validation_verification_and_rehash() -> None:
    with pytest.raises(ValueError, match="mindestens 12"):
        security.hash_password("short")
    with pytest.raises(ValueError, match="zu lang"):
        security.hash_password("x" * 1025)

    encoded = security.hash_password("a-secure-test-password")
    assert encoded.startswith("$argon2id$")
    assert security.verify_password("a-secure-test-password", encoded)
    assert not security.verify_password("wrong-password", encoded)
    assert not security.verify_password("anything", "invalid-hash")
    assert security.password_needs_rehash("invalid-hash")

    assert not security.password_needs_rehash(encoded)


def test_password_verification_fails_closed_when_argon2_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semaphore = SimpleNamespace(acquire=Mock(return_value=False), release=Mock())
    monkeypatch.setattr(security, "_argon2_slots", semaphore)

    assert not security.verify_password("password", "hash")
    semaphore.acquire.assert_called_once_with(timeout=2)
    semaphore.release.assert_not_called()


def test_create_and_delete_session_uses_hashed_cookie_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, session_ttl_hours=2, session_cookie_secure=True)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    tokens = iter(["raw-session-token", "csrf-token"])
    monkeypatch.setattr(security.secrets, "token_urlsafe", lambda _length: next(tokens))
    db = FakeDB()
    response = Response()
    request = request_for(headers={"user-agent": "Test Browser"})

    session = security.create_session(db, make_user(), request, response)  # type: ignore[arg-type]

    assert session.token_hash != "raw-session-token"
    assert session.token_hash == security._token_hash("raw-session-token", settings)
    assert session.csrf_token == "csrf-token"
    assert session.user_agent_hash == hashlib.sha256(b"Test Browser").hexdigest()
    assert session.expires_at > datetime.now(UTC) + timedelta(minutes=119)
    cookie = response.headers["set-cookie"]
    assert "rezepte_session=raw-session-token" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert db.added == [session] and db.flushes == 1
    assert len(db.executed) == 1
    cleanup_statement = str(db.executed[0]).lower()
    assert "delete from user_sessions" in cleanup_statement
    assert "expires_at" in cleanup_statement
    assert "token_hash" not in cleanup_statement

    delete_response = Response()
    security.delete_session(
        db,  # type: ignore[arg-type]
        request_for(cookie="rezepte_session=raw-session-token"),
        delete_response,
    )
    assert len(db.executed) == 2
    assert "delete from user_sessions" in str(db.executed[1]).lower()
    delete_params = db.executed[1].compile().params
    assert security._token_hash("raw-session-token", settings) in delete_params.values()
    assert 'rezepte_session=""' in delete_response.headers["set-cookie"]


def test_create_session_keeps_parallel_devices_and_rotates_only_the_presented_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    tokens = iter(
        [
            "device-a-token",
            "device-a-csrf",
            "device-b-token",
            "device-b-csrf",
            "device-a-replacement-token",
            "device-a-replacement-csrf",
        ]
    )
    monkeypatch.setattr(security.secrets, "token_urlsafe", lambda _length: next(tokens))
    db = FakeDB()
    user = make_user()

    device_a = security.create_session(
        db,  # type: ignore[arg-type]
        user,
        request_for(headers={"user-agent": "Device A"}),
        Response(),
    )
    device_b = security.create_session(
        db,  # type: ignore[arg-type]
        user,
        request_for(headers={"user-agent": "Device B"}),
        Response(),
    )

    assert device_a.token_hash != device_b.token_hash
    assert db.added == [device_a, device_b]
    assert all("expires_at" in str(statement).lower() for statement in db.executed)
    assert all("token_hash" not in str(statement).lower() for statement in db.executed)

    replacement = security.create_session(
        db,  # type: ignore[arg-type]
        user,
        request_for(
            cookie="rezepte_session=device-a-token",
            headers={"user-agent": "Device A"},
        ),
        Response(),
    )

    rotation_statement = db.executed[-1]
    rotation_sql = str(rotation_statement).lower()
    rotation_params = rotation_statement.compile().params
    assert "expires_at" in rotation_sql and "token_hash" in rotation_sql
    assert security._token_hash("device-a-token", settings) in rotation_params.values()
    assert device_b.token_hash not in rotation_params.values()
    assert replacement.token_hash not in {device_a.token_hash, device_b.token_hash}


def test_delete_session_without_cookie_still_expires_browser_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    db = FakeDB()
    response = Response()

    security.delete_session(db, request_for(), response)  # type: ignore[arg-type]

    assert db.executed == []
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_get_session_rejects_missing_expired_inactive_and_wrong_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    assert security.get_session(FakeDB(), request_for()) is None  # type: ignore[arg-type]
    assert (
        security.get_session(
            FakeDB(scalar_results=[None]),  # type: ignore[arg-type]
            request_for(cookie="rezepte_session=unknown"),
        )
        is None
    )

    now = datetime.now(UTC)
    expired = SimpleNamespace(
        expires_at=now - timedelta(seconds=1),
        user=SimpleNamespace(is_active=True),
        user_agent_hash=None,
        last_seen_at=now,
    )
    inactive = SimpleNamespace(
        expires_at=now + timedelta(hours=1),
        user=SimpleNamespace(is_active=False),
        user_agent_hash=None,
        last_seen_at=now,
    )
    agent_bound = SimpleNamespace(
        expires_at=now + timedelta(hours=1),
        user=SimpleNamespace(is_active=True),
        user_agent_hash=hashlib.sha256(b"Expected Browser").hexdigest(),
        last_seen_at=now,
    )
    for candidate, headers in (
        (expired, {}),
        (inactive, {}),
        (agent_bound, {"user-agent": "Other Browser"}),
    ):
        db = FakeDB(scalar_results=[candidate])
        result = security.get_session(
            db,  # type: ignore[arg-type]
            request_for(cookie="rezepte_session=token", headers=headers),
        )
        assert result is None
        assert db.deleted == [candidate]
        assert db.commits == 1


def test_get_session_touches_only_stale_valid_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    now = datetime.now(UTC)
    user_agent = hashlib.sha256(b"Browser").hexdigest()
    stale = SimpleNamespace(
        expires_at=now + timedelta(hours=1),
        user=SimpleNamespace(is_active=True),
        user_agent_hash=user_agent,
        last_seen_at=now - timedelta(seconds=301),
    )
    db = FakeDB(scalar_results=[stale])

    assert (
        security.get_session(
            db,  # type: ignore[arg-type]
            request_for(cookie="rezepte_session=token", headers={"user-agent": "Browser"}),
        )
        is stale
    )
    assert db.commits == 1
    assert stale.last_seen_at > now

    fresh = SimpleNamespace(
        expires_at=now + timedelta(hours=1),
        user=SimpleNamespace(is_active=True),
        user_agent_hash=None,
        last_seen_at=now,
    )
    fresh_db = FakeDB(scalar_results=[fresh])
    assert (
        security.get_session(
            fresh_db,
            request_for(cookie="rezepte_session=token"),  # type: ignore[arg-type]
        )
        is fresh
    )
    assert fresh_db.commits == 0


def test_csrf_requires_matching_token_and_same_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, app_base_url="https://recipes.example")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    session = SimpleNamespace(csrf_token="known-token")

    with pytest.raises(HTTPException) as missing:
        security.require_csrf(request_for(), session)  # type: ignore[arg-type]
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException, match="Ungültige Anfragequelle"):
        security.require_csrf(
            request_for(headers={"x-csrf-token": "known-token", "origin": "https://evil.test"}),
            session,  # type: ignore[arg-type]
        )

    security.require_csrf(
        request_for(headers={"x-csrf-token": "known-token", "origin": "https://recipes.example/"}),
        session,  # type: ignore[arg-type]
    )


def test_login_csrf_requires_double_submit_and_same_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, app_base_url="https://recipes.example")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    token = security.new_login_csrf_token()
    response = Response()
    security.set_login_csrf_cookie(response, token)
    assert settings.login_csrf_cookie_name in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]

    cookie = f"{settings.login_csrf_cookie_name}={token}"
    security.require_login_csrf(
        request_for(cookie=cookie, headers={"origin": "https://recipes.example"}), token
    )
    security.require_login_csrf(
        request_for(
            cookie=cookie,
            headers={"referer": "https://recipes.example/login?next=%2Frezepte"},
        ),
        token,
    )

    with pytest.raises(HTTPException) as foreign:
        security.require_login_csrf(
            request_for(cookie=cookie, headers={"origin": "https://evil.example"}), token
        )
    assert foreign.value.status_code == 403
    with pytest.raises(HTTPException) as missing_source:
        security.require_login_csrf(request_for(cookie=cookie), token)
    assert missing_source.value.status_code == 403
    with pytest.raises(HTTPException) as mismatched:
        security.require_login_csrf(
            request_for(cookie=cookie, headers={"origin": "https://recipes.example"}),
            "different-token",
        )
    assert mismatched.value.status_code == 403


class FakeRatePipeline:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, Any]] = []

    def incr(self, key: str) -> None:
        self.calls.append(("incr", key, None))

    def expire(self, key: str, seconds: int, *, nx: bool) -> None:
        self.calls.append(("expire", key, (seconds, nx)))

    def execute(self) -> list[Any]:
        return self.results


class FakeRateRedis:
    def __init__(self, pipeline: FakeRatePipeline) -> None:
        self.rate_pipeline = pipeline
        self.deleted: list[str] = []

    def __enter__(self) -> FakeRateRedis:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def pipeline(self, *, transaction: bool) -> FakeRatePipeline:
        assert transaction is True
        return self.rate_pipeline

    def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


def test_login_rate_limit_uses_two_atomic_redis_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        login_rate_limit_attempts=2,
        login_rate_limit_ip_attempts=5,
        login_rate_limit_window_seconds=20,
    )
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    pipeline = FakeRatePipeline([2, True, 5, True])
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        lambda *_args, **_kwargs: FakeRateRedis(pipeline),
    )

    security.check_login_rate_limit(request_for(), "User@Example.test")

    assert [call[0] for call in pipeline.calls] == ["incr", "expire", "incr", "expire"]
    assert all(call[2] == (20, True) for call in pipeline.calls if call[0] == "expire")


def test_successful_login_clears_only_the_account_rate_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    redis = FakeRateRedis(FakeRatePipeline([]))
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        lambda *_args, **_kwargs: redis,
    )
    account_key = security._login_account_rate_key("user@example.test", settings)
    ip_key = "login-rate:ip:keep-this-bucket"
    security._fallback_rate_buckets.clear()
    security._fallback_rate_buckets.update(
        {
            account_key: (3, 100.0),
            ip_key: (7, 100.0),
        }
    )

    security.clear_login_account_rate_limit(" User@Example.Test ")

    assert redis.deleted == [account_key]
    assert account_key not in security._fallback_rate_buckets
    assert security._fallback_rate_buckets[ip_key] == (7, 100.0)


def test_login_rate_limit_redis_account_bucket_survives_ip_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        login_rate_limit_attempts=1,
        login_rate_limit_ip_attempts=5,
        login_rate_limit_window_seconds=20,
    )
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    first = FakeRatePipeline([1, True, 1, True])
    second = FakeRatePipeline([2, True, 1, True])
    pipelines = iter([first, second])
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        lambda *_args, **_kwargs: FakeRateRedis(next(pipelines)),
    )

    security.check_login_rate_limit(request_for(client=("192.0.2.10", 4242)), " User@Example.test ")
    with pytest.raises(HTTPException) as limited:
        security.check_login_rate_limit(
            request_for(client=("198.51.100.20", 4242)), "user@example.TEST"
        )
    assert limited.value.status_code == 429
    first_account_key = first.calls[0][1]
    second_account_key = second.calls[0][1]
    expected_digest = hmac.new(
        settings.app_secret_key.encode(),
        b"login-account:user@example.test",
        hashlib.sha256,
    ).hexdigest()
    assert first_account_key == second_account_key == f"login-rate:account:{expected_digest}"
    assert first.calls[2][1] != second.calls[2][1]
    assert "user@example.test" not in first_account_key


def test_login_rate_limit_fallback_account_bucket_survives_ip_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        login_rate_limit_attempts=1,
        login_rate_limit_ip_attempts=5,
        login_rate_limit_window_seconds=20,
    )
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        Mock(side_effect=ConnectionError("redis unavailable")),
    )
    security._fallback_rate_buckets.clear()

    security.check_login_rate_limit(request_for(client=("192.0.2.10", 4242)), "User@Example.test")
    with pytest.raises(HTTPException) as limited:
        security.check_login_rate_limit(
            request_for(client=("198.51.100.20", 4242)), " user@example.test "
        )
    assert limited.value.status_code == 429


def test_login_rate_limit_rejects_redis_and_fallback_overages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        login_rate_limit_attempts=1,
        login_rate_limit_ip_attempts=5,
        login_rate_limit_window_seconds=10,
    )
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    pipeline = FakeRatePipeline([2, True, 1, True])
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        lambda *_args, **_kwargs: FakeRateRedis(pipeline),
    )
    with pytest.raises(HTTPException) as limited:
        security.check_login_rate_limit(request_for(), "user@example.test")
    assert limited.value.status_code == 429
    assert limited.value.headers == {"Retry-After": "10"}

    security._fallback_rate_buckets.clear()
    monkeypatch.setattr(
        security.Redis,
        "from_url",
        Mock(side_effect=ConnectionError("redis unavailable")),
    )
    security.check_login_rate_limit(request_for(), "fallback@example.test")
    with pytest.raises(HTTPException) as fallback_limited:
        security.check_login_rate_limit(request_for(), "FALLBACK@example.test")
    assert fallback_limited.value.status_code == 429


def test_login_rate_fallback_resets_expired_and_caps_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, login_rate_limit_attempts=1)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    monkeypatch.setattr(security.Redis, "from_url", Mock(side_effect=OSError("offline")))
    security._fallback_rate_buckets.clear()
    security._fallback_rate_buckets.update({f"stale-{index}": (1, 0.0) for index in range(10_001)})
    monkeypatch.setattr(security.time, "monotonic", lambda: 100.0)

    security.check_login_rate_limit(request_for(client=None), "new@example.test")

    assert len(security._fallback_rate_buckets) == 2
    assert all(value == (1, 160.0) for value in security._fallback_rate_buckets.values())


# Filesystem storage


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"\xff\xd8\xffrest", ("image/jpeg", ".jpg")),
        (b"\x89PNG\r\n\x1a\nrest", ("image/png", ".png")),
        (b"GIF89a-rest", ("image/gif", ".gif")),
        (b"RIFFxxxxWEBPrest", ("image/webp", ".webp")),
        (b"xxxxftypheicrest", ("image/heic", ".heic")),
        (b"%PDF-1.7", ("application/pdf", ".pdf")),
    ],
)
def test_detect_type_accepts_supported_magic_bytes(
    header: bytes, expected: tuple[str, str]
) -> None:
    assert storage.detect_type(header) == expected


def test_detect_type_and_download_name_reject_or_sanitize_untrusted_input() -> None:
    with pytest.raises(InvalidUpload, match="Nicht unterstütztes"):
        storage.detect_type(b"not-an-upload")
    assert storage.safe_download_name("../../ unsafe\x00 name.png ") == "unsafe name.png"
    assert storage.safe_download_name("\x00") == "datei"
    assert len(storage.safe_download_name("x" * 700)) == 500


def test_active_storage_root_bootstraps_and_rejects_external_symlink(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    active = storage.active_storage_root(settings)

    assert active == (settings.storage_root / "generations" / "bootstrap").resolve()
    assert (settings.storage_root / "current").is_symlink()

    (settings.storage_root / "current").unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, settings.storage_root / "current")
    with pytest.raises(RuntimeError, match="ungültiges Ziel"):
        storage.active_storage_root(settings)


@pytest.mark.parametrize("committed", [False, True])
def test_recover_interrupted_restore_chooses_database_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    settings = make_settings(tmp_path)
    generations = settings.storage_root / "generations"
    generations.mkdir(parents=True)
    old_target = generations / "old-generation"
    new_target = generations / "new-generation"
    old_target.mkdir()
    new_target.mkdir()
    journal = settings.storage_root / ".restore-journal.json"
    journal.write_text(
        (
            '{"restore_id":"restore-1","old_target":"'
            + str(old_target)
            + '","new_target":"'
            + str(new_target)
            + '"}'
        ),
        encoding="utf-8",
    )
    marker = SimpleNamespace(value={"restore_id": "restore-1" if committed else "other"})

    class DBContext:
        def __enter__(self) -> DBContext:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get(self, _model: Any, _identifier: Any) -> Any:
            return marker

    monkeypatch.setattr("app.database.SessionLocal", lambda: DBContext())
    swap = Mock()
    monkeypatch.setattr(storage, "swap_active_generation", swap)

    storage.recover_interrupted_restore(settings)

    swap.assert_called_once_with(new_target if committed else old_target, settings=settings)
    assert not journal.exists()


def test_recover_interrupted_restore_preserves_journal_when_database_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.storage_root.mkdir(parents=True)
    journal = settings.storage_root / ".restore-journal.json"
    journal.write_text('{"restore_id":"r","old_target":"old","new_target":"new"}', encoding="utf-8")
    monkeypatch.setattr("app.database.SessionLocal", Mock(side_effect=OSError("database down")))
    swap = Mock()
    monkeypatch.setattr(storage, "swap_active_generation", swap)

    storage.recover_interrupted_restore(settings)

    assert journal.exists()
    swap.assert_not_called()


def test_cleanup_retained_files_removes_only_expired_safe_targets(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, backup_download_retention_hours=1)
    active = storage.active_storage_root(settings)
    generations = settings.storage_root / "generations"
    stale_generation = generations / "stale"
    recent_generation = generations / "recent"
    stale_generation.mkdir()
    recent_generation.mkdir()
    old = 1_000.0
    recent = 5_000.0
    os.utime(stale_generation, (old, old))
    os.utime(recent_generation, (recent, recent))
    settings.backup_temp_root.mkdir(parents=True)
    stale_zip = settings.backup_temp_root / "old.zip"
    stale_partial = settings.backup_temp_root / "old.partial"
    stale_restore = settings.backup_temp_root / "restore-upload-123"
    unrelated = settings.backup_temp_root / "keep.txt"
    recent_zip = settings.backup_temp_root / "recent.zip"
    for path in (stale_zip, stale_partial, stale_restore, unrelated, recent_zip):
        path.write_bytes(b"x")
    for path in (stale_zip, stale_partial, stale_restore, unrelated):
        os.utime(path, (old, old))
    os.utime(recent_zip, (recent, recent))

    original_time = storage.time.time
    storage.time.time = lambda: 5_000.0
    try:
        storage.cleanup_retained_files(settings)
    finally:
        storage.time.time = original_time

    assert active.exists()
    assert not stale_generation.exists()
    assert recent_generation.exists()
    assert not stale_zip.exists() and not stale_partial.exists() and not stale_restore.exists()
    assert unrelated.exists() and recent_zip.exists()


def test_cleanup_retained_files_keeps_generations_during_restore_journal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, backup_download_retention_hours=1)
    storage.active_storage_root(settings)
    stale = settings.storage_root / "generations" / "stale"
    stale.mkdir()
    os.utime(stale, (0, 0))
    (settings.storage_root / ".restore-journal.json").write_text("{}", encoding="utf-8")

    storage.cleanup_retained_files(settings)

    assert stale.exists()


def test_swap_active_generation_is_atomic_and_confined(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    storage.active_storage_root(settings)
    target = settings.storage_root / "generations" / "restored"
    target.mkdir()

    storage.swap_active_generation(target, settings=settings)

    assert (settings.storage_root / "current").resolve() == target.resolve()
    assert not list(settings.storage_root.glob(".current-*"))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(InvalidUpload, match="Speichergeneration"):
        storage.swap_active_generation(outside, settings=settings)


@pytest.mark.anyio
async def test_store_upload_persists_valid_image_and_pdf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    image_data = png_bytes()
    image = await storage.store_upload(
        UploadFile(filename="../Küche.png", file=BytesIO(image_data)), settings=settings
    )
    assert image.mime_type == "image/png"
    assert (image.width, image.height) == (24, 18)
    assert image.sha256 == hashlib.sha256(image_data).hexdigest()
    assert image.original_filename == "Küche.png"
    assert storage.resolve_storage_key(image.storage_key, settings).read_bytes() == image_data

    document_data = pdf_bytes()
    document = await storage.store_upload(
        UploadFile(filename="scan.pdf", file=BytesIO(document_data)), settings=settings
    )
    assert document.mime_type == "application/pdf"
    assert document.page_count == 1
    assert document.storage_key.startswith("originals/")


@pytest.mark.anyio
async def test_store_upload_enforces_allowed_type_and_header_sized_limit(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    tiny_png = png_bytes((1, 1))
    assert len(tiny_png) > 10 and len(tiny_png) <= 100

    with pytest.raises(InvalidUpload, match="erlaubte Größe"):
        await storage.store_upload(
            UploadFile(filename="tiny.png", file=BytesIO(tiny_png)),
            max_bytes=10,
            settings=settings,
        )
    with pytest.raises(InvalidUpload, match="Dateityp"):
        await storage.store_upload(
            UploadFile(filename="tiny.png", file=BytesIO(tiny_png)),
            allowed={"application/pdf"},
            settings=settings,
        )
    assert not settings.storage_root.exists()


@pytest.mark.anyio
async def test_store_upload_rejects_large_stream_and_cleans_partial_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    payload = png_bytes() + b"x" * (1024 * 1024)
    with pytest.raises(InvalidUpload, match="erlaubte Größe"):
        await storage.store_upload(
            UploadFile(filename="large.png", file=BytesIO(payload)),
            max_bytes=100,
            settings=settings,
        )
    assert not list(settings.storage_root.rglob("*.partial"))
    assert not list(settings.storage_root.rglob("*.png"))


@pytest.mark.anyio
async def test_store_upload_rejects_corrupt_image_and_cleans_partial(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(InvalidUpload, match="beschädigt"):
        await storage.store_upload(
            UploadFile(filename="broken.png", file=BytesIO(b"\x89PNG\r\n\x1a\nnot-valid")),
            settings=settings,
        )
    assert not list(settings.storage_root.rglob("*.partial"))


@pytest.mark.anyio
async def test_store_upload_rejects_pdf_page_and_password_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, max_pdf_pages=1)

    monkeypatch.setattr(
        storage,
        "inspect_pdf",
        Mock(return_value=PDFInfo(page_count=2)),
    )
    with pytest.raises(InvalidUpload, match="zu viele Seiten"):
        await storage.store_upload(
            UploadFile(filename="many.pdf", file=BytesIO(b"%PDF-page-test")), settings=settings
        )

    monkeypatch.setattr(
        storage,
        "inspect_pdf",
        Mock(side_effect=PasswordProtectedPDF("locked")),
    )
    with pytest.raises(InvalidUpload, match="Passwortgeschützte"):
        await storage.store_upload(
            UploadFile(filename="locked.pdf", file=BytesIO(b"%PDF-password-test")),
            settings=settings,
        )
    assert not list(settings.storage_root.rglob("*.partial"))


def test_resolve_storage_key_confines_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    root = storage.active_storage_root(settings)
    expected = root / "images" / "safe.png"
    assert storage.resolve_storage_key("images/safe.png", settings) == expected
    with pytest.raises(InvalidUpload, match="Speicherpfad"):
        storage.resolve_storage_key("../../outside", settings)
    with pytest.raises(InvalidUpload, match="Speicherpfad"):
        storage.resolve_storage_key(".", settings)


def test_store_bytes_validates_roles_checksum_dimensions_and_pdf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    payload = png_bytes()
    stored = storage.store_bytes(
        payload,
        filename="generated.png",
        kind="generated_image",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        settings=settings,
    )
    assert stored.storage_key.startswith("generated/")
    assert (stored.width, stored.height) == (24, 18)
    assert storage.resolve_storage_key(stored.storage_key, settings).read_bytes() == payload

    thumbnail = storage.store_bytes(
        payload, filename="thumb.png", kind="image_thumbnail", settings=settings
    )
    assert thumbnail.storage_key.startswith("derivatives/")
    document = storage.store_bytes(
        pdf_bytes(), filename="source.pdf", kind="url_snapshot_pdf", settings=settings
    )
    assert document.page_count == 1
    assert document.storage_key.startswith("imports/")


def test_store_bytes_rejects_size_role_checksum_and_corruption(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.max_upload_mb = 1
    with pytest.raises(InvalidUpload, match="erlaubte Größe"):
        storage.store_bytes(
            b"x" * (1024 * 1024 + 1), filename="big.bin", kind="original_upload", settings=settings
        )
    with pytest.raises(InvalidUpload, match="Rezeptbild"):
        storage.store_bytes(
            pdf_bytes(), filename="wrong.pdf", kind="recipe_image", settings=settings
        )
    with pytest.raises(InvalidUpload, match="URL-Snapshot"):
        storage.store_bytes(
            png_bytes(), filename="wrong.png", kind="url_snapshot_pdf", settings=settings
        )
    with pytest.raises(InvalidUpload, match="Prüfsumme"):
        storage.store_bytes(
            png_bytes(),
            filename="wrong.png",
            kind="recipe_image",
            expected_sha256="0" * 64,
            settings=settings,
        )
    with pytest.raises(InvalidUpload, match="beschädigt"):
        storage.store_bytes(
            b"\x89PNG\r\n\x1a\ninvalid",
            filename="broken.png",
            kind="recipe_image",
            settings=settings,
        )


# Media services


def test_create_asset_maps_all_metadata() -> None:
    db = FakeDB()
    user = make_user()
    stored = StoredFile(
        storage_key="images/aa/file.png",
        original_filename="file.png",
        mime_type="image/png",
        byte_size=99,
        sha256="b" * 64,
        width=40,
        height=30,
    )
    asset = media.create_asset(db, stored, user, "recipe_image")  # type: ignore[arg-type]
    assert db.added == [asset] and db.flushes == 1
    assert asset.uploaded_by_user_id == user.id
    assert asset.storage_key == stored.storage_key
    assert (asset.width, asset.height, asset.page_count) == (40, 30, None)


def test_create_thumbnail_skips_documents_and_flattens_transparency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        media.create_thumbnail_asset(
            FakeDB(),
            make_asset(mime_type="application/pdf"),
            make_user(),  # type: ignore[arg-type]
        )
        is None
    )

    source = tmp_path / "source.png"
    source.write_bytes(png_bytes((1000, 800)))
    original = make_asset(storage_key="source.png")
    generated = tmp_path / "thumbnail.jpg"
    captured: dict[str, Any] = {}

    def fake_store(data: bytes, **kwargs: Any) -> StoredFile:
        captured.update(data=data, kwargs=kwargs)
        generated.write_bytes(data)
        return StoredFile(
            storage_key="thumbnail.jpg",
            original_filename=kwargs["filename"],
            mime_type="image/jpeg",
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            width=600,
            height=480,
        )

    monkeypatch.setattr(
        media,
        "resolve_storage_key",
        lambda key: source if key == "source.png" else generated,
    )
    monkeypatch.setattr(media, "store_bytes", fake_store)
    db = FakeDB()
    thumbnail = media.create_thumbnail_asset(db, original, make_user())  # type: ignore[arg-type]

    assert thumbnail is not None and thumbnail.kind == "image_thumbnail"
    assert captured["kwargs"]["kind"] == "image_thumbnail"
    with Image.open(BytesIO(captured["data"])) as image:
        assert image.mode == "RGB"
        assert image.size == (600, 480)


def test_create_thumbnail_removes_generated_file_when_database_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(png_bytes())
    generated = tmp_path / "generated.jpg"
    generated.write_bytes(b"generated")
    monkeypatch.setattr(
        media,
        "resolve_storage_key",
        lambda key: source if key == "source.png" else generated,
    )
    monkeypatch.setattr(
        media,
        "store_bytes",
        lambda *_args, **_kwargs: StoredFile(
            "generated.jpg", "generated.jpg", "image/jpeg", 9, "a" * 64
        ),
    )
    monkeypatch.setattr(media, "create_asset", Mock(side_effect=RuntimeError("db failure")))

    with pytest.raises(RuntimeError, match="db failure"):
        media.create_thumbnail_asset(
            FakeDB(),
            make_asset(storage_key="source.png"),
            make_user(),  # type: ignore[arg-type]
        )
    assert not generated.exists()


@pytest.mark.anyio
async def test_add_recipe_image_sets_position_cover_and_clean_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = StoredFile("new.png", "new.png", "image/png", 3, "a" * 64, 1, 1)

    async def fake_upload(*_args: Any, **_kwargs: Any) -> StoredFile:
        return stored

    source_path = tmp_path / "new.png"
    thumb_path = tmp_path / "thumb.jpg"
    source_path.write_bytes(b"new")
    thumb_path.write_bytes(b"thumb")
    asset = make_asset(storage_key="new.png")
    thumbnail = make_asset(kind="image_thumbnail", storage_key="thumb.jpg")
    old_asset = make_asset()
    old_image = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=uuid.uuid4(),
        media_asset_id=old_asset.id,
        asset=old_asset,
        position=0,
        is_cover=True,
    )
    recipe = make_recipe([old_image])
    old_image.recipe_id = recipe.id
    monkeypatch.setattr(media, "store_upload", fake_upload)
    monkeypatch.setattr(
        media, "resolve_storage_key", lambda key: source_path if key == "new.png" else thumb_path
    )
    monkeypatch.setattr(media, "create_asset", lambda *_args: asset)
    monkeypatch.setattr(media, "create_thumbnail_asset", lambda *_args: thumbnail)
    db = FakeDB(scalar_results=[4])

    image = await media.add_recipe_image(
        db,  # type: ignore[arg-type]
        recipe,
        make_user(),
        UploadFile(filename="ignored.png", file=BytesIO(b"ignored")),
        caption="  Lecker  ",
        alt_text="  Teller  ",
        is_cover=True,
    )

    assert image.position == 5 and image.is_cover
    assert image.caption == "Lecker" and image.alt_text == "Teller"
    assert not old_image.is_cover
    assert image.asset is asset and image.thumbnail_asset is thumbnail
    assert db.added == [image] and db.flushes == 1


@pytest.mark.anyio
async def test_add_recipe_image_cleans_written_files_on_database_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = StoredFile("new.png", "new.png", "image/png", 3, "a" * 64)

    async def fake_upload(*_args: Any, **_kwargs: Any) -> StoredFile:
        return stored

    source = tmp_path / "new.png"
    thumbnail_path = tmp_path / "thumb.jpg"
    source.write_bytes(b"new")
    thumbnail_path.write_bytes(b"thumb")
    monkeypatch.setattr(media, "store_upload", fake_upload)
    monkeypatch.setattr(
        media, "resolve_storage_key", lambda key: source if key == "new.png" else thumbnail_path
    )
    monkeypatch.setattr(media, "create_asset", lambda *_args: make_asset(storage_key="new.png"))
    monkeypatch.setattr(
        media,
        "create_thumbnail_asset",
        lambda *_args: make_asset(kind="image_thumbnail", storage_key="thumb.jpg"),
    )
    db = FakeDB(scalar_results=[None])
    db.flush = Mock(side_effect=RuntimeError("unique collision"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="unique collision"):
        await media.add_recipe_image(
            db,  # type: ignore[arg-type]
            make_recipe(),
            make_user(),
            UploadFile(filename="ignored.png", file=BytesIO()),
        )
    assert not source.exists() and not thumbnail_path.exists()


@pytest.mark.anyio
async def test_add_original_asset_positions_and_cleans_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = StoredFile("source.pdf", "source.pdf", "application/pdf", 3, "a" * 64)

    async def fake_upload(*_args: Any, **_kwargs: Any) -> StoredFile:
        return stored

    path = tmp_path / "source.pdf"
    path.write_bytes(b"pdf")
    asset = make_asset(
        kind="original_upload", mime_type="application/pdf", storage_key="source.pdf"
    )
    monkeypatch.setattr(media, "store_upload", fake_upload)
    monkeypatch.setattr(media, "resolve_storage_key", lambda _key: path)
    monkeypatch.setattr(media, "create_asset", lambda *_args: asset)
    db = FakeDB(scalar_results=[2])
    recipe = make_recipe()
    link = await media.add_original_asset(
        db,  # type: ignore[arg-type]
        recipe,
        make_user(),
        UploadFile(filename="ignored.pdf", file=BytesIO()),
    )
    assert link.position == 3 and link.media_asset_id == asset.id
    assert db.added == [link]

    path.write_bytes(b"pdf")
    failing = FakeDB(scalar_results=[None])
    failing.flush = Mock(side_effect=RuntimeError("db failure"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="db failure"):
        await media.add_original_asset(
            failing,  # type: ignore[arg-type]
            recipe,
            make_user(),
            UploadFile(filename="ignored.pdf", file=BytesIO()),
        )
    assert not path.exists()


def test_update_image_enforces_ownership_cover_metadata_and_reordering() -> None:
    first_asset, second_asset, third_asset = make_asset(), make_asset(), make_asset()
    recipe = make_recipe()
    first = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=first_asset.id,
        asset=first_asset,
        position=0,
        is_cover=True,
    )
    second = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=second_asset.id,
        asset=second_asset,
        position=1,
        is_cover=False,
    )
    third = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=third_asset.id,
        asset=third_asset,
        position=2,
        is_cover=False,
    )
    recipe.images = [first, second, third]
    db = FakeDB()

    result = media.update_image(
        db,  # type: ignore[arg-type]
        recipe,
        third,
        ImageMetadataInput(position=0, is_cover=True, caption="  Neu ", alt_text="   "),
    )
    assert result is third and third.is_cover and not first.is_cover
    assert third.caption == "Neu" and third.alt_text is None
    assert [(item, item.position) for item in (third, first, second)] == [
        (third, 0),
        (first, 1),
        (second, 2),
    ]
    assert db.flushes == 3

    outsider = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=uuid.uuid4(),
        media_asset_id=first_asset.id,
        asset=first_asset,
        position=0,
        is_cover=False,
    )
    with pytest.raises(HTTPException) as missing:
        media.update_image(db, recipe, outsider, ImageMetadataInput())  # type: ignore[arg-type]
    assert missing.value.status_code == 404


def test_update_image_unsets_cover_and_promotes_replacement() -> None:
    recipe = make_recipe()
    first_asset, second_asset = make_asset(), make_asset()
    first = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=first_asset.id,
        asset=first_asset,
        position=0,
        is_cover=True,
    )
    second = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=second_asset.id,
        asset=second_asset,
        position=1,
        is_cover=False,
    )
    recipe.images = [first, second]

    media.update_image(
        FakeDB(),
        recipe,
        first,
        ImageMetadataInput(is_cover=False),  # type: ignore[arg-type]
    )
    assert not first.is_cover and second.is_cover


def test_remove_image_normalizes_positions_cover_and_deletes_assets() -> None:
    recipe = make_recipe()
    cover_asset = make_asset()
    thumb = make_asset(kind="image_thumbnail")
    next_asset = make_asset()
    cover = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=cover_asset.id,
        thumbnail_asset_id=thumb.id,
        asset=cover_asset,
        thumbnail_asset=thumb,
        position=0,
        is_cover=True,
    )
    remaining = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=recipe.id,
        media_asset_id=next_asset.id,
        asset=next_asset,
        position=9,
        is_cover=False,
    )
    recipe.images = [cover, remaining]
    db = FakeDB(scalars_results=[[remaining]])

    removed = media.remove_image(db, recipe, cover)  # type: ignore[arg-type]

    assert removed == [cover_asset, thumb]
    assert remaining.position == 0 and remaining.is_cover
    assert db.deleted == [cover, cover_asset, thumb]
    assert db.flushes == 4

    outsider = RecipeImage(
        id=uuid.uuid4(),
        recipe_id=uuid.uuid4(),
        media_asset_id=next_asset.id,
        asset=next_asset,
        position=0,
        is_cover=False,
    )
    with pytest.raises(HTTPException) as missing:
        media.remove_image(db, recipe, outsider)  # type: ignore[arg-type]
    assert missing.value.status_code == 404


def test_get_asset_and_attachment_checks() -> None:
    identifier = uuid.uuid4()
    asset = make_asset()
    assert media.get_asset(FakeDB(get_result=asset), identifier) is asset  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as missing:
        media.get_asset(FakeDB(), identifier)  # type: ignore[arg-type]
    assert missing.value.status_code == 404

    assert media.asset_is_attached(
        FakeDB(scalar_results=[uuid.uuid4(), None]),
        identifier,  # type: ignore[arg-type]
    )
    assert media.asset_is_attached(
        FakeDB(scalar_results=[None, uuid.uuid4()]),
        identifier,  # type: ignore[arg-type]
    )
    assert not media.asset_is_attached(
        FakeDB(scalar_results=[None, None]),
        identifier,  # type: ignore[arg-type]
    )


# Category services


def test_category_tree_and_create_category_paths() -> None:
    existing = make_category("Bestehend")
    db = FakeDB(scalars_results=[[existing]])
    assert categories.category_tree(db) == [existing]  # type: ignore[arg-type]

    missing_parent_db = FakeDB()
    with pytest.raises(HTTPException) as missing_parent:
        categories.create_category(
            missing_parent_db,  # type: ignore[arg-type]
            CategoryCreate(name="Kind", parent_id=uuid.uuid4()),
        )
    assert missing_parent.value.status_code == 404

    collision_db = FakeDB(scalar_results=[existing], get_result=existing)
    with pytest.raises(HTTPException) as collision:
        categories.create_category(
            collision_db,  # type: ignore[arg-type]
            CategoryCreate(name="  BESTEHEND  ", parent_id=existing.id),
        )
    assert collision.value.status_code == 409

    root_db = FakeDB(scalar_results=[None, 4])
    created = categories.create_category(
        root_db,
        CategoryCreate(name="  Desserts  "),
        origin="ai_import",  # type: ignore[arg-type]
    )
    assert created.name == "Desserts"
    assert created.normalized_name == "desserts"
    assert created.slug == "desserts"
    assert created.position == 5 and created.origin == "ai_import"
    assert root_db.added == [created] and root_db.flushes == 1


def test_category_tree_is_depth_first_and_keeps_each_branch_together() -> None:
    first_root = make_category("Root A")
    second_root = make_category("Root B")
    first_child = make_category("Child A", parent_id=first_root.id)
    second_child = make_category("Child B", parent_id=second_root.id)
    first_root.position = 0
    second_root.position = 1
    # Child positions deliberately overlap the root positions; a global sort
    # would interleave the branches.
    first_child.position = 0
    second_child.position = 0
    db = FakeDB(scalars_results=[[second_child, second_root, first_child, first_root]])

    assert categories.category_tree(db) == [  # type: ignore[arg-type]
        first_root,
        first_child,
        second_root,
        second_child,
    ]


def test_descendant_ids_walks_entire_tree_without_repeating() -> None:
    root, child, grandchild = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = FakeDB(scalars_results=[[child], [grandchild, child], []])
    assert categories._descendant_ids(db, root) == {child, grandchild}  # type: ignore[arg-type]


def test_update_category_rejects_cycles_and_collisions() -> None:
    category = make_category("Root")
    with pytest.raises(HTTPException) as self_parent:
        categories.update_category(
            FakeDB(),  # type: ignore[arg-type]
            category,
            CategoryUpdate(parent_id=category.id),
        )
    assert self_parent.value.status_code == 409

    descendant = uuid.uuid4()
    with pytest.raises(HTTPException) as cycle:
        categories.update_category(
            FakeDB(scalars_results=[[descendant], []]),  # type: ignore[arg-type]
            category,
            CategoryUpdate(parent_id=descendant),
        )
    assert cycle.value.status_code == 409

    collision = make_category("Collision")
    with pytest.raises(HTTPException) as duplicate:
        categories.update_category(
            FakeDB(scalar_results=[collision]),  # type: ignore[arg-type]
            category,
            CategoryUpdate(name="Collision"),
        )
    assert duplicate.value.status_code == 409


def test_update_category_moves_reorders_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    category = make_category("Alt")
    new_parent = uuid.uuid4()
    first = make_category("A", parent_id=new_parent)
    second = make_category("B", parent_id=new_parent)
    first.position = 2
    second.position = 3
    db = FakeDB(scalars_results=[[], [first, second]], scalar_results=[None])
    refresh = Mock()
    monkeypatch.setattr(categories, "_refresh_linked_recipes", refresh)

    result = categories.update_category(
        db,  # type: ignore[arg-type]
        category,
        CategoryUpdate(name="  Neu  ", parent_id=new_parent, position=1),
    )

    assert result is category
    assert (category.name, category.normalized_name, category.slug) == ("Neu", "neu", "neu")
    assert category.parent_id == new_parent
    assert [(first.position, category.position, second.position)] == [(0, 1, 2)]
    refresh.assert_called_once_with(db, category)


def test_refresh_linked_recipes_includes_descendant_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    category = make_category("Root")
    child_id = uuid.uuid4()
    recipe_ids = [uuid.uuid4(), uuid.uuid4()]
    db = FakeDB(scalars_results=[[child_id], [], recipe_ids])
    loaded = {identifier: SimpleNamespace(id=identifier) for identifier in recipe_ids}
    get_recipe = Mock(side_effect=lambda _db, identifier, **_kwargs: loaded[identifier])
    refresh = Mock()
    monkeypatch.setattr("app.services.recipes.get_recipe", get_recipe)
    monkeypatch.setattr(categories, "refresh_search_document", refresh)

    categories._refresh_linked_recipes(db, category)  # type: ignore[arg-type]

    assert get_recipe.call_count == 2 and refresh.call_count == 2
    assert {call.args[1] for call in get_recipe.call_args_list} == set(recipe_ids)
    assert all(call.kwargs == {"for_update": True} for call in get_recipe.call_args_list)


def test_delete_category_guards_children_and_refreshes_affected_recipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = make_category("Parent")
    parent.children = [make_category("Child", parent_id=parent.id)]
    with pytest.raises(HTTPException) as has_children:
        categories.delete_category(FakeDB(), parent)  # type: ignore[arg-type]
    assert has_children.value.status_code == 409

    category = make_category("Leaf")
    recipe_id = uuid.uuid4()
    category.recipe_links = [RecipeCategory(recipe_id=recipe_id, category_id=category.id)]
    recipe = SimpleNamespace(id=recipe_id)
    get_recipe = Mock(return_value=recipe)
    refresh = Mock()
    monkeypatch.setattr("app.services.recipes.get_recipe", get_recipe)
    monkeypatch.setattr(categories, "refresh_search_document", refresh)
    db = FakeDB()

    assert categories.delete_category(db, category) == 1  # type: ignore[arg-type]
    assert db.deleted == [category] and db.flushes == 1
    get_recipe.assert_called_once_with(db, recipe_id, for_update=True)
    refresh.assert_called_once_with(db, recipe)


def test_merge_category_rejects_identity_descendant_and_child_collision() -> None:
    source = make_category("Source")
    target = make_category("Target")
    with pytest.raises(HTTPException) as identity:
        categories.merge_category(FakeDB(), source, source)  # type: ignore[arg-type]
    assert identity.value.status_code == 409

    with pytest.raises(HTTPException) as descendant:
        categories.merge_category(
            FakeDB(scalars_results=[[target.id], []]),
            source,
            target,  # type: ignore[arg-type]
        )
    assert descendant.value.status_code == 409

    child = make_category("Child", parent_id=source.id)
    source.children = [child]
    collision = make_category("Child", parent_id=target.id)
    with pytest.raises(HTTPException) as duplicate_child:
        categories.merge_category(
            FakeDB(scalars_results=[[]], scalar_results=[collision]),  # type: ignore[arg-type]
            source,
            target,
        )
    assert duplicate_child.value.status_code == 409


def test_merge_category_moves_unique_links_children_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_category("Source")
    target = make_category("Target")
    first_recipe, duplicate_recipe = uuid.uuid4(), uuid.uuid4()
    moved_link = RecipeCategory(recipe_id=first_recipe, category_id=source.id)
    duplicate_link = RecipeCategory(recipe_id=duplicate_recipe, category_id=source.id)
    source.recipe_links = [moved_link, duplicate_link]
    child = make_category("Child", parent_id=source.id)
    source.children = [child]

    def get_result(_model: Any, identifier: Any) -> Any:
        return object() if identifier == (duplicate_recipe, target.id) else None

    db = FakeDB(scalars_results=[[]], scalar_results=[None], get_result=get_result)
    loaded = {
        first_recipe: SimpleNamespace(id=first_recipe),
        duplicate_recipe: SimpleNamespace(id=duplicate_recipe),
    }
    monkeypatch.setattr(
        "app.services.recipes.get_recipe",
        Mock(side_effect=lambda _db, identifier, **_kwargs: loaded[identifier]),
    )
    refresh = Mock()
    monkeypatch.setattr(categories, "refresh_search_document", refresh)

    moved = categories.merge_category(db, source, target)  # type: ignore[arg-type]

    assert moved == 1 and moved_link.category_id == target.id
    assert duplicate_link in db.deleted and source in db.deleted
    assert child.parent_id == target.id
    assert child.parent is target
    assert child.position == 0
    assert refresh.call_count == 2


# Cross-process maintenance locking


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []
        self.commits = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: Any, parameters: dict[str, int]) -> None:
        self.calls.append((str(statement), parameters))

    def commit(self) -> None:
        self.commits += 1


def test_maintenance_guard_is_noop_outside_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import maintenance

    connect = Mock()
    monkeypatch.setattr(
        maintenance,
        "engine",
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), connect=connect),
    )
    with maintenance.database_maintenance_guard():
        pass
    connect.assert_not_called()


def test_maintenance_guard_locks_and_always_unlocks_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import maintenance

    connection = FakeConnection()
    monkeypatch.setattr(
        maintenance,
        "engine",
        SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection),
    )
    with (
        pytest.raises(RuntimeError, match="worker failed"),
        maintenance.database_maintenance_guard(),
    ):
        raise RuntimeError("worker failed")

    assert len(connection.calls) == 2
    assert connection.commits == 2
    assert "pg_advisory_lock" in connection.calls[0][0]
    assert "pg_advisory_unlock" in connection.calls[1][0]
    assert (
        connection.calls[0][1]
        == connection.calls[1][1]
        == {"lock_id": maintenance.MAINTENANCE_ADVISORY_LOCK}
    )
