from __future__ import annotations

import uuid
from datetime import UTC, datetime
from string import Formatter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from jinja2 import Environment
from starlette.requests import Request

from app import main
from app.ai.prompts import extraction_system_prompt
from app.api import auth as auth_api
from app.api import imports as imports_api
from app.api import notes as notes_api
from app.api import productivity as productivity_api
from app.api import recipes as recipes_api
from app.auth.dependencies import current_user
from app.database import get_db
from app.i18n import (
    LOCALES,
    MESSAGES,
    SUPPORTED_LOCALES,
    Locale,
    catalog_is_complete,
    detect_browser_locale,
    format_datetime_locale,
    translate,
    translate_known_text,
)
from app.models import ImportBatch, ImportJob, User
from app.schemas.recipe import RecipeInput
from app.services.recipes import slugify


def _request(*, accept_language: str = "de") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/auth/login",
            "query_string": b"",
            "headers": [(b"accept-language", accept_language.encode("ascii"))],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        }
    )


class FakeDB:
    def __init__(self, scalar_result: object | None = None) -> None:
        self.scalar_result = scalar_result
        self.added: list[object] = []
        self.commits = 0

    def scalar(self, _statement: object) -> object | None:
        return self.scalar_result

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()  # type: ignore[attr-defined]

    def commit(self) -> None:
        self.commits += 1


def test_every_supported_locale_has_a_complete_catalog() -> None:
    assert catalog_is_complete()
    assert set(MESSAGES) == set(SUPPORTED_LOCALES)
    expected_keys = set(MESSAGES["en"])
    assert expected_keys
    for locale in SUPPORTED_LOCALES:
        assert set(MESSAGES[locale]) == expected_keys
        assert all(message.strip() for message in MESSAGES[locale].values())
        for key in expected_keys:
            expected_fields = {
                field for _, field, _, _ in Formatter().parse(MESSAGES["en"][key]) if field
            }
            actual_fields = {
                field for _, field, _, _ in Formatter().parse(MESSAGES[locale][key]) if field
            }
            assert actual_fields == expected_fields, (locale, key)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("es-MX,es;q=0.9,en;q=0.8", "es"),
        ("fr-FR;q=1,en-GB;q=0.9,es;q=0.8", "en"),
        ("zh-TW, en;q=0.5", "zh-CN"),
        ("hi-IN;q=0.7,de-DE;q=0.6", "hi"),
        ("fr-FR,ja;q=0.8", "de"),
        ("en;q=0,es;q=0.4", "es"),
        ("en;q=2,hi;q=0.5", "hi"),
    ],
)
def test_browser_language_detection_honors_quality_and_regional_variants(
    header: str,
    expected: str,
) -> None:
    assert detect_browser_locale(header) == expected


def test_first_api_login_persists_the_browser_language(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(
        id=uuid.uuid4(),
        email="ada@example.test",
        display_name="Ada",
        password_hash="stored-hash",
        role="member",
        is_active=True,
        language=None,
    )
    db = FakeDB(user)
    monkeypatch.setattr(auth_api, "check_login_rate_limit", Mock())
    monkeypatch.setattr(auth_api, "clear_login_account_rate_limit", Mock())
    monkeypatch.setattr(auth_api, "verify_password", Mock(return_value=True))
    monkeypatch.setattr(auth_api, "password_needs_rehash", Mock(return_value=False))
    monkeypatch.setattr(
        auth_api,
        "create_session",
        Mock(return_value=SimpleNamespace(csrf_token="csrf-token")),
    )

    result = auth_api.login(
        auth_api.LoginPayload(email=user.email, password="secret"),
        _request(accept_language="es-MX, en;q=0.8"),
        Response(),
        db=db,  # type: ignore[arg-type]
    )

    assert user.language == "es"
    assert result["user"]["language"] == "es"  # type: ignore[index]
    assert db.commits == 1


def test_login_does_not_overwrite_an_existing_account_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=uuid.uuid4(),
        email="ada@example.test",
        display_name="Ada",
        password_hash="stored-hash",
        role="member",
        is_active=True,
        language="hi",
    )
    db = FakeDB(user)
    monkeypatch.setattr(auth_api, "check_login_rate_limit", Mock())
    monkeypatch.setattr(auth_api, "clear_login_account_rate_limit", Mock())
    monkeypatch.setattr(auth_api, "verify_password", Mock(return_value=True))
    monkeypatch.setattr(auth_api, "password_needs_rehash", Mock(return_value=False))
    monkeypatch.setattr(
        auth_api,
        "create_session",
        Mock(return_value=SimpleNamespace(csrf_token="csrf-token")),
    )

    auth_api.login(
        auth_api.LoginPayload(email=user.email, password="secret"),
        _request(accept_language="es"),
        Response(),
        db=db,  # type: ignore[arg-type]
    )

    assert user.language == "hi"


def test_account_language_update_is_exact_and_immediately_localized() -> None:
    user = SimpleNamespace(language="de")
    db = FakeDB()

    result = auth_api.update_language(
        auth_api.LanguagePayload(language="zh-CN"),
        _request(),
        user=user,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
    )

    assert user.language == "zh-CN"
    assert result["message"] == MESSAGES["zh-CN"]["account.saved"]
    assert db.commits == 1

    with pytest.raises(HTTPException) as error:
        auth_api.update_language(
            auth_api.LanguagePayload(language="es-MX"),
            _request(),
            user=user,  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 422


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_ai_prompt_explicitly_targets_each_account_language(locale: str) -> None:
    prompt = extraction_system_prompt(locale)  # type: ignore[arg-type]
    assert LOCALES[locale].ai_language in prompt  # type: ignore[index]
    assert translate(locale, "ai.import_language", language=LOCALES[locale].ai_language) in prompt  # type: ignore[index,arg-type]


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_new_import_batch_snapshots_the_users_language(
    locale: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), language=locale)
    db = FakeDB()
    monkeypatch.setattr(imports_api, "_ensure_import_capacity", Mock())
    monkeypatch.setattr(imports_api, "validate_public_url", lambda value: value)
    send = Mock()
    monkeypatch.setattr(imports_api.import_batch_task, "send", send)

    result = imports_api.import_urls(
        imports_api.URLImportPayload(urls=["https://example.test/recipe"]),
        user=user,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
    )

    batch = next(value for value in db.added if isinstance(value, ImportBatch))
    job = next(value for value in db.added if isinstance(value, ImportJob))
    assert batch.target_language == locale
    assert job.current_stage == translate(locale, "job.waiting")
    assert result["batch_id"] == str(batch.id)
    send.assert_called_once_with(str(batch.id))


def test_unicode_recipe_titles_produce_stable_searchable_slugs() -> None:
    assert slugify("宫保鸡丁") == "宫保鸡丁"
    assert slugify("आलू गोभी") == "आलू-गोभी"
    assert slugify("Tortilla española") == "tortilla-espanola"


def test_api_recipe_defaults_follow_the_account_language_without_overwriting_explicit_values() -> (
    None
):
    user = SimpleNamespace(language="hi")
    omitted = RecipeInput(title="आलू गोभी", base_servings="4")
    explicit = RecipeInput(title="आलू गोभी", base_servings="4", serving_label="लोग")

    assert (
        recipes_api._with_default_serving_label(
            omitted,
            user=user,  # type: ignore[arg-type]
        ).serving_label
        == LOCALES["hi"].default_serving_label
    )
    assert (
        recipes_api._with_default_serving_label(
            omitted,
            user=user,  # type: ignore[arg-type]
            existing_label="कटोरे",
        ).serving_label
        == "कटोरे"
    )
    assert (
        recipes_api._with_default_serving_label(
            explicit,
            user=user,  # type: ignore[arg-type]
        ).serving_label
        == "लोग"
    )


def test_stable_worker_keys_and_legacy_german_statuses_localize() -> None:
    assert translate_known_text("es", "job.worker_resume") == MESSAGES["es"]["job.worker_resume"]
    assert translate_known_text("hi", "Wartet") == MESSAGES["hi"]["job.waiting"]


@pytest.mark.parametrize(
    ("environment", "timezone"),
    [
        (main.templates.env, main.settings.display_timezone),
        (notes_api.templates.env, notes_api.settings.display_timezone),
        (productivity_api.templates.env, productivity_api.settings.display_timezone),
    ],
)
@pytest.mark.parametrize("locale", ["en", "es"])
def test_template_datetime_filter_uses_the_render_locale(
    environment: Environment,
    timezone: str,
    locale: Locale,
) -> None:
    value = datetime(2026, 8, 31, 12, 30, tzinfo=UTC)

    rendered = environment.from_string("{{ value|datetime }}").render(
        value=value,
        locale=locale,
    )

    assert rendered == format_datetime_locale(value, locale, timezone)


@pytest.mark.parametrize(
    ("browser_language", "locale"),
    [("en-US", "en"), ("zh-CN", "zh-CN"), ("hi-IN", "hi"), ("es-ES", "es"), ("de-DE", "de")],
)
def test_login_page_renders_in_the_detected_browser_language(
    browser_language: str,
    locale: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "get_session", lambda *_args: None)
    main.app.dependency_overrides[get_db] = lambda: FakeDB()
    client = TestClient(main.app, base_url="http://localhost", raise_server_exceptions=False)
    try:
        response = client.get("/login", headers={"Accept-Language": browser_language})
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert f'<html lang="{locale}">' in response.text
    assert MESSAGES[locale]["login.welcome"] in response.text


def test_account_page_uses_the_saved_language_instead_of_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=uuid.uuid4(),
        email="ada@example.test",
        display_name="Ada",
        password_hash="stored-hash",
        role="member",
        is_active=True,
        language="hi",
    )
    session = SimpleNamespace(user=user, csrf_token="csrf-token")
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    main.app.dependency_overrides[get_db] = lambda: FakeDB()
    main.app.dependency_overrides[current_user] = lambda: user
    client = TestClient(main.app, base_url="http://localhost", raise_server_exceptions=False)
    try:
        response = client.get("/konto", headers={"Accept-Language": "es-ES"})
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert '<html lang="hi">' in response.text
    assert MESSAGES["hi"]["account.title"] in response.text
    assert '<option value="hi" selected>' in response.text
