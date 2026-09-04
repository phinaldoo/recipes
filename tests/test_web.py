from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api import auth as auth_api
from app.api import notes as notes_api
from app.api import productivity as productivity_api
from app.auth.dependencies import current_session
from app.auth.security import DUMMY_PASSWORD_HASH
from app.database import get_db
from app.main import app
from app.models import User


class FakeQuery:
    def __init__(self) -> None:
        self.deleted = False

    def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
        return self

    def delete(self, **_kwargs: object) -> int:
        self.deleted = True
        return 1


class FakeSession:
    """Small SQLAlchemy-session stand-in used by the web boundary tests."""

    def __init__(self, scalar_result: object | None = None) -> None:
        self.scalar_result = scalar_result
        self.scalars_result: list[object] = []
        self.last_scalar_statement: object | None = None
        self.last_scalars_statement: object | None = None
        self.query_result = FakeQuery()
        self.commits = 0

    def scalar(self, statement: object) -> object | None:
        self.last_scalar_statement = statement
        return self.scalar_result

    def scalars(self, statement: object) -> list[object]:
        self.last_scalars_statement = statement
        return self.scalars_result

    def query(self, _model: object) -> FakeQuery:
        return self.query_result

    def get(self, _model: object, _identifier: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class FakeRedis:
    def __enter__(self) -> FakeRedis:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _key: str) -> None:
        return None


def make_user(role: str = "member") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{role}@example.test",
        display_name=role.title(),
        password_hash="stored-password-hash",
        role=role,
        is_active=True,
    )


def make_login_session(role: str = "member", csrf_token: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(user=make_user(role), csrf_token=csrf_token or "csrf-secret")


@pytest.fixture
def fake_db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_db: FakeSession) -> TestClient:
    app.dependency_overrides[get_db] = lambda: fake_db
    monkeypatch.setattr(main.Redis, "from_url", lambda *_args, **_kwargs: FakeRedis())
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.clear()


def test_liveness_is_public_and_has_request_id(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "browser-check-42"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-request-id"] == "browser-check-42"
    assert response.headers["cache-control"] == "private, no-store"


def test_manifest_describes_installable_german_pwa(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.headers["cache-control"] == "public, max-age=3600"
    manifest = response.json()
    assert manifest["lang"] == "de"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/rezepte"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    assert {icon["src"] for icon in manifest["icons"]} == {
        main.frontend_assets.url("pwa/icon-192.png"),
        main.frontend_assets.url("pwa/icon-512.png"),
        main.frontend_assets.url("pwa/icon-maskable-512.png"),
    }
    assert {shortcut["url"] for shortcut in manifest["shortcuts"]} == {
        "/rezepte",
        "/rezepte/neu",
        "/importieren",
    }


def test_service_worker_has_root_scope_and_does_not_cache_private_api(client: TestClient) -> None:
    response = client.get("/service-worker.js")

    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["cache-control"] == "no-cache"
    assert 'url.pathname.startsWith("/api/")' in response.text
    assert 'url.pathname === "/login"' in response.text
    assert f"rezepte-static-{main.frontend_assets.build_id}" in response.text
    assert "__STATIC_ASSETS__" not in response.text


def test_offline_fallback_is_public_but_contains_no_recipe_data(client: TestClient) -> None:
    response = client.get("/offline")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "Du bist gerade offline" in response.text
    assert "Private Rezeptdaten werden nicht offline gespeichert" in response.text
    assert main.frontend_assets.url("css/app.css") in response.text


def test_fingerprinted_assets_are_immutable_and_source_paths_are_not_served(
    client: TestClient,
) -> None:
    asset_url = main.frontend_assets.url("js/app.js")
    response = client.get(asset_url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    not_modified = client.get(asset_url, headers={"If-None-Match": response.headers["etag"]})
    assert not_modified.status_code == 304
    assert not_modified.headers["cache-control"] == "public, max-age=31536000, immutable"
    missing = client.get("/static/js/app.js")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "private, no-store"


def test_disabled_pwa_returns_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "pwa_enabled", False)

    assert client.get("/manifest.webmanifest").status_code == 404
    assert client.get("/service-worker.js").status_code == 404


def test_login_page_rejects_protocol_relative_redirect_target(client: TestClient) -> None:
    response = client.get("/login", params={"next": "//evil.example/steal"})

    assert response.status_code == 200
    assert 'name="next_url" value="/rezepte"' in response.text
    assert "evil.example" not in response.text


def test_html_login_rejects_foreign_origin_and_accepts_valid_preauth_token(
    client: TestClient,
    fake_db: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = client.get("/login")
    cookie_name = main.settings.login_csrf_cookie_name
    token = client.cookies.get(cookie_name)
    assert token
    assert page.headers["cache-control"] == "no-store"
    assert re.search(rf'name="login_csrf_token" value="{re.escape(token)}"', page.text)
    rate_limit = Mock()
    monkeypatch.setattr(main, "check_login_rate_limit", rate_limit)

    rejected = client.post(
        "/login",
        data={
            "email": "member@example.test",
            "password": "password",
            "login_csrf_token": token,
        },
        headers={"Origin": "https://evil.example"},
    )
    assert rejected.status_code == 403
    rate_limit.assert_not_called()

    user = make_user()
    fake_db.scalar_result = user
    monkeypatch.setattr(main, "verify_password", Mock(return_value=True))
    monkeypatch.setattr(main, "create_session", Mock())
    accepted = client.post(
        "/login",
        data={
            "email": user.email,
            "password": "password",
            "next_url": "/rezepte",
            "login_csrf_token": token,
        },
        headers={"Origin": main.settings.app_base_url},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/rezepte"
    assert client.cookies.get(cookie_name) is None
    rate_limit.assert_called_once()


def test_html_login_rejects_missing_origin_or_mismatched_preauth_token(
    client: TestClient,
) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    token = client.cookies.get(main.settings.login_csrf_cookie_name)
    assert token
    missing_origin = client.post(
        "/login",
        data={"email": "x@y.de", "password": "x", "login_csrf_token": token},
    )
    assert missing_origin.status_code == 403
    mismatched = client.post(
        "/login",
        data={"email": "x@y.de", "password": "x", "login_csrf_token": "wrong"},
        headers={"Origin": main.settings.app_base_url},
    )
    assert mismatched.status_code == 403


def test_unauthenticated_api_request_has_structured_401(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["x-login-url"] == "/login"
    assert response.json()["error"] == {
        "code": "http_401",
        "message": "Bitte melde dich an.",
    }
    assert response.json()["request_id"]


def test_unauthenticated_html_request_redirects_to_login(client: TestClient) -> None:
    response = client.get("/rezepte", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/rezepte"


def test_recipe_filter_explains_and_preserves_parent_category_selection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    category_id = uuid.uuid4()
    category = SimpleNamespace(
        id=category_id,
        name="Hauptgerichte",
        path="Hauptgerichte",
    )
    listing = Mock(return_value=([], 0, 1, 1))
    monkeypatch.setattr(main, "list_recipes", listing)
    monkeypatch.setattr(main, "category_tree", lambda _db: [category])

    response = client.get(
        "/rezepte",
        params={"category_ids": str(category_id)},
        headers={"Host": "localhost"},
    )

    assert response.status_code == 200
    assert "Oberkategorien schließen alle Unterkategorien ein." in response.text
    assert re.search(
        rf'name="category_ids" value="{category_id}" data-auto-submit checked',
        response.text,
    )
    assert ">Anwenden</button>" not in response.text
    assert "Hauptgerichte" in response.text
    assert listing.call_args.kwargs["category_ids"] == [category_id]


def test_recipe_search_can_return_only_the_replaceable_results_region(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(
        id=recipe_id,
        title="Kartoffelsuppe",
        cover_image=None,
        categories=[],
        total_time_minutes=None,
        comments=[],
    )
    listing = Mock(return_value=([recipe], 1, 1, 1))
    monkeypatch.setattr(main, "list_recipes", listing)
    monkeypatch.setattr(main, "category_tree", lambda _db: [])
    monkeypatch.setattr(main, "favorite_recipe_ids", Mock(return_value=set()))

    response = client.get(
        "/rezepte",
        params={"q": "Kartoffel"},
        headers={"Host": "localhost", "X-Recipe-Results": "1"},
    )

    assert response.status_code == 200
    assert response.headers["vary"] == "X-Recipe-Results"
    assert 'id="recipe-results-region"' in response.text
    assert "data-recipe-results-summary>1 Rezept gefunden" in response.text
    assert f'href="/rezepte/{recipe_id}"' in response.text
    assert "Kartoffelsuppe" in response.text
    assert "<html" not in response.text
    assert "data-recipe-search>" not in response.text
    assert listing.call_args.kwargs["q"] == "Kartoffel"

    trash_response = client.get(
        "/papierkorb",
        params={"q": "Kartoffel"},
        headers={"Host": "localhost", "X-Recipe-Results": "1"},
    )

    assert trash_response.status_code == 200
    assert "<html" not in trash_response.text
    assert f'data-restore-recipe="{recipe_id}"' in trash_response.text
    assert listing.call_args.kwargs["only_deleted"] is True


def test_recipe_kind_switch_filters_cards_and_preserves_infinite_scroll_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(
        id=recipe_id,
        title="Zitronenkuchen",
        recipe_kind="baking",
        cover_image=None,
        categories=[],
        total_time_minutes=60,
        comments=[],
    )
    listing = Mock(return_value=([recipe], 25, 2, 1))
    monkeypatch.setattr(main, "list_recipes", listing)
    monkeypatch.setattr(main, "category_tree", lambda _db: [])
    monkeypatch.setattr(main, "favorite_recipe_ids", Mock(return_value=set()))

    response = client.get(
        "/rezepte",
        params={"recipe_kind": "baking", "q": "Zitrone"},
        headers={"Host": "localhost"},
    )

    assert response.status_code == 200
    assert "<h1 data-recipe-kind-heading>Backen</h1>" in response.text
    assert re.search(
        r'name="recipe_kind" value="baking" data-auto-submit checked',
        response.text,
    )
    assert f'data-recipe-id="{recipe_id}" data-recipe-kind="baking"' in response.text
    assert 'class="recipe-kind-label recipe-kind-label--baking recipe-card__kind"' in response.text
    assert "recipe_kind=baking" in response.text
    assert listing.call_args.kwargs["recipe_kind"] == "baking"


def test_invalid_recipe_kind_query_is_rejected(client: TestClient) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session

    response = client.get(
        "/rezepte",
        params={"recipe_kind": "grilling"},
        headers={"Host": "localhost"},
    )

    assert response.status_code == 422


def test_recipe_list_renders_an_infinite_stream_with_skeletons_instead_of_pages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(
        id=recipe_id,
        title="Kartoffelsuppe",
        cover_image=None,
        categories=[],
        total_time_minutes=45,
        comments=[],
    )
    monkeypatch.setattr(main, "list_recipes", Mock(return_value=([recipe], 49, 3, 1)))
    monkeypatch.setattr(main, "category_tree", lambda _db: [])
    monkeypatch.setattr(main, "favorite_recipe_ids", Mock(return_value=set()))

    response = client.get("/rezepte", headers={"Host": "localhost"})

    assert response.status_code == 200
    assert "data-recipe-stream" in response.text
    assert 'data-page="1"' in response.text
    assert 'data-pages="3"' in response.text
    assert 'data-recipe-card data-recipe-id="' + str(recipe_id) + '"' in response.text
    assert "data-recipe-search-skeleton hidden" in response.text
    assert "data-recipe-stream-skeletons hidden" in response.text
    assert "Weitere Rezepte laden" in response.text
    assert "page=2" in response.text
    assert 'class="pagination"' not in response.text


def test_recipe_append_response_contains_only_the_requested_card_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    category_id = uuid.uuid4()
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(
        id=recipe_id,
        title="Ofengemüse",
        cover_image=None,
        categories=[],
        total_time_minutes=None,
        comments=[],
    )
    listing = Mock(return_value=([recipe], 49, 3, 2))
    categories = Mock(return_value=[])
    monkeypatch.setattr(main, "list_recipes", listing)
    monkeypatch.setattr(main, "category_tree", categories)
    monkeypatch.setattr(main, "favorite_recipe_ids", Mock(return_value=set()))

    response = client.get(
        "/rezepte",
        params={"q": "Ofen", "sort": "title_asc", "page": 2, "category_ids": category_id},
        headers={"Host": "localhost", "X-Recipe-Results": "append"},
    )

    assert response.status_code == 200
    assert response.headers["vary"] == "X-Recipe-Results"
    assert "data-recipe-batch" in response.text
    assert 'data-page="2"' in response.text
    assert 'data-pages="3"' in response.text
    assert 'data-total="49"' in response.text
    assert 'data-recipe-id="' + str(recipe_id) + '"' in response.text
    assert "page=3" in response.text
    assert "data-recipe-results-region" not in response.text
    assert "data-recipe-search-skeleton" not in response.text
    assert "<html" not in response.text
    assert listing.call_args.kwargs["page"] == 2
    assert listing.call_args.kwargs["category_ids"] == [category_id]
    categories.assert_not_called()


def test_recipe_cards_render_user_favorite_state_and_toggle_controls(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    favorite_id = uuid.uuid4()
    other_id = uuid.uuid4()
    recipes = [
        SimpleNamespace(
            id=favorite_id,
            title="Kartoffelsuppe",
            cover_image=None,
            categories=[],
            total_time_minutes=None,
            comments=[],
        ),
        SimpleNamespace(
            id=other_id,
            title="Ofengemüse",
            cover_image=None,
            categories=[],
            total_time_minutes=45,
            comments=[],
        ),
    ]
    monkeypatch.setattr(main, "list_recipes", Mock(return_value=(recipes, 2, 1, 1)))
    monkeypatch.setattr(main, "category_tree", lambda _db: [])
    favorite_lookup = Mock(return_value={favorite_id})
    monkeypatch.setattr(main, "favorite_recipe_ids", favorite_lookup)

    response = client.get("/rezepte", headers={"Host": "localhost"})

    assert response.status_code == 200
    favorite_button = re.search(
        rf'<button\s+class="favorite-toggle".*?data-recipe-id="{favorite_id}".*?</button>',
        response.text,
        flags=re.DOTALL,
    )
    other_button = re.search(
        rf'<button\s+class="favorite-toggle".*?data-recipe-id="{other_id}".*?</button>',
        response.text,
        flags=re.DOTALL,
    )
    assert favorite_button is not None
    assert 'aria-pressed="true"' in favorite_button.group()
    assert 'aria-label="Kartoffelsuppe aus Favoriten entfernen"' in favorite_button.group()
    assert other_button is not None
    assert 'aria-pressed="false"' in other_button.group()
    assert 'aria-label="Ofengemüse zu Favoriten hinzufügen"' in other_button.group()
    assert main.frontend_assets.url("js/productivity.js") in response.text
    assert favorite_lookup.call_args.args[:2] == (fake_db, session.user)
    assert list(favorite_lookup.call_args.args[2]) == [favorite_id, other_id]


def test_favorites_page_uses_star_toggle_without_separate_remove_button(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    recipe_id = uuid.uuid4()
    recipe = SimpleNamespace(
        id=recipe_id,
        title="Kartoffelsuppe",
        cover_image=None,
        tags=[],
        total_time_minutes=None,
    )
    favorites_lookup = Mock(return_value=[recipe])
    monkeypatch.setattr(productivity_api, "list_favorites", favorites_lookup)

    response = client.get("/favoriten", headers={"Host": "localhost"})

    assert response.status_code == 200
    favorite_card = re.search(
        r'<article class="recipe-card"[^>]*>.*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert favorite_card is not None
    favorite_button = re.search(
        rf'<button\s+class="favorite-toggle".*?data-recipe-id="{recipe_id}".*?</button>',
        favorite_card.group(),
        flags=re.DOTALL,
    )
    assert favorite_button is not None
    assert 'data-favorite-variant="icon"' in favorite_button.group()
    assert 'data-favorite-known="true"' in favorite_button.group()
    assert 'aria-pressed="true"' in favorite_button.group()
    assert 'aria-label="Kartoffelsuppe aus Favoriten entfernen"' in favorite_button.group()
    assert "★ Aus Favoriten entfernen" not in response.text
    assert 'class="button button--text"' not in favorite_card.group()
    favorites_lookup.assert_called_once_with(fake_db, session.user, recipe_kind=None)


def test_favorites_page_filters_by_recipe_kind_and_marks_the_active_area(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    recipe = SimpleNamespace(
        id=uuid.uuid4(),
        title="Marmorkuchen",
        recipe_kind="baking",
        cover_image=None,
        categories=[],
        comments=[],
        total_time_minutes=70,
    )
    favorites_lookup = Mock(return_value=[recipe])
    monkeypatch.setattr(productivity_api, "list_favorites", favorites_lookup)

    response = client.get(
        "/favoriten",
        params={"recipe_kind": "baking"},
        headers={"Host": "localhost"},
    )

    assert response.status_code == 200
    assert re.search(
        r'class="recipe-kind-option recipe-kind-option--baking"[^>]*aria-current="page"',
        response.text,
    )
    assert 'data-recipe-kind="baking"' in response.text
    assert "Marmorkuchen" in response.text
    favorites_lookup.assert_called_once_with(fake_db, session.user, recipe_kind="baking")


def test_notes_page_renders_private_recipe_links_and_editing_controls(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    note_id = uuid.uuid4()
    now = datetime.now(UTC)
    notes_lookup = Mock(
        return_value=[
            SimpleNamespace(
                id=note_id,
                title="Zitronenpasta",
                url="https://example.test/rezepte/zitronenpasta",
                content="Am Wochenende ausprobieren.",
                created_at=now,
                updated_at=now,
            )
        ]
    )
    monkeypatch.setattr(notes_api, "list_notes", notes_lookup)

    response = client.get("/notizen", headers={"Host": "localhost"})

    assert response.status_code == 200
    assert "Meine Notizen" in response.text
    assert "Nur für dich" in response.text
    assert f'data-note-id="{note_id}"' in response.text
    assert 'href="https://example.test/rezepte/zitronenpasta"' in response.text
    assert 'target="_blank" rel="noopener noreferrer"' in response.text
    assert "Am Wochenende ausprobieren." in response.text
    assert "data-note-edit" in response.text
    assert "data-note-delete" in response.text
    assert 'href="/notizen" aria-current="page"' in response.text
    assert main.frontend_assets.url("js/notes.js") in response.text
    notes_lookup.assert_called_once_with(fake_db, session.user)


def test_notes_api_list_query_is_scoped_to_the_current_user(
    client: TestClient, fake_db: FakeSession
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    now = datetime.now(UTC)
    note_id = uuid.uuid4()
    fake_db.scalars_result = [
        SimpleNamespace(
            id=note_id,
            title=None,
            url="https://example.test/recipe",
            content=None,
            created_at=now,
            updated_at=now,
        )
    ]

    response = client.get("/api/v1/notes", headers={"Host": "localhost"})

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(note_id)
    assert response.json()["items"][0]["url"] == "https://example.test/recipe"
    assert fake_db.last_scalars_statement is not None
    assert session.user.id in fake_db.last_scalars_statement.compile().params.values()


def test_notes_api_rejects_cross_user_note_identifier(
    client: TestClient, fake_db: FakeSession
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    note_id = uuid.uuid4()

    response = client.delete(
        f"/api/v1/notes/{note_id}",
        headers={
            "Host": "localhost",
            "X-CSRF-Token": "csrf-secret",
            "Origin": main.settings.app_base_url,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Die Notiz wurde nicht gefunden."
    assert fake_db.last_scalar_statement is not None
    parameters = set(fake_db.last_scalar_statement.compile().params.values())
    assert {note_id, session.user.id} <= parameters
    assert fake_db.commits == 0


def test_import_page_does_not_render_originals_banner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)

    response = client.get("/importieren")

    assert response.status_code == 200
    assert "Rezepte importieren" in response.text
    assert (
        '<a class="button button--secondary" href="/importieren/verlauf">'
        "Importverlauf anzeigen</a>" in response.text
    )
    assert 'href="/importieren/laufend">Laufende Importe und offene Auswahl</a>' in response.text
    assert response.text.index('href="/importieren/verlauf"') > response.text.index(
        "data-json-import"
    )
    assert '<aside class="privacy-note">' not in response.text
    assert "Rezept hinzufügen" in response.text
    assert 'href="/importieren" aria-current="page">Importieren</a>' in response.text
    assert 'href="/rezepte/neu" >Frei erstellen</a>' in response.text
    assert "Deine Originale bleiben erhalten" not in response.text
    assert "Import auf iPhone und iPad" not in response.text


def test_recipe_form_offers_clipboard_paste_drop_and_file_selection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    monkeypatch.setattr(main, "category_tree", lambda _db: [])

    response = client.get("/rezepte/neu")

    assert response.status_code == 200
    assert 'data-image-paste-zone tabindex="0"' in response.text
    assert "Bild einfügen oder hier ablegen" in response.text
    assert "Strg" in response.text and "⌘" in response.text
    assert 'data-image-files type="file"' in response.text
    assert 'data-image-message role="status"' in response.text
    assert main.frontend_assets.url("js/recipe-form.js") in response.text


def test_running_imports_page_lists_active_batches(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    batch_id = uuid.uuid4()
    fake_db.scalars_result = [
        SimpleNamespace(
            id=batch_id,
            created_at=datetime.now(UTC),
            status="processing",
            total_jobs=2,
            completed_jobs=1,
            failed_jobs=0,
        )
    ]

    response = client.get("/importieren/laufend")

    assert response.status_code == 200
    assert "Laufende Importe" in response.text
    assert f'href="/importieren/{batch_id}"' in response.text
    assert "Fortschritt anzeigen" in response.text
    assert "1 fertig" in response.text


def test_running_imports_page_surfaces_multi_recipe_selection(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    batch_id = uuid.uuid4()
    fake_db.scalars_result = [
        SimpleNamespace(
            id=batch_id,
            created_at=datetime.now(UTC),
            status="review",
            total_jobs=1,
            completed_jobs=0,
            failed_jobs=0,
            jobs=[
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(status="ready"),
                        SimpleNamespace(status="ready"),
                    ]
                )
            ],
        )
    ]

    response = client.get("/importieren/laufend")

    assert response.status_code == 200
    assert "Auswahl erforderlich" in response.text
    assert "Rezepte auswählen" in response.text
    assert f'href="/importieren/{batch_id}"' in response.text


def _ready_import_candidate(title: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        title=title,
        confidence="high",
        recipe_payload={
            "description": "Cremig",
            "base_servings": "4",
            "serving_label": "Personen",
            "ingredient_groups": [
                {
                    "title": None,
                    "ingredients": [
                        {
                            "name": "Kartoffeln",
                            "unit": "g",
                            "amount_min": "500",
                            "amount_max": None,
                            "note": None,
                        }
                    ],
                }
            ],
            "instruction_steps": [{"text": "Kartoffeln kochen."}],
        },
        source_regions_json=[{"page": 1}],
        warnings_json=[],
        error_message=None,
        image_asset_id=None,
        image_region_json={"page": 1},
        result_recipe_id=None,
    )


def test_import_batch_renders_full_multi_recipe_review_and_preselects_ready_candidates(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    batch_id = uuid.uuid4()
    first_candidate = _ready_import_candidate("Kartoffelsuppe")
    second_candidate = _ready_import_candidate("Kartoffelgratin")
    source_asset = SimpleNamespace(
        original_filename="doppelseite.png",
        mime_type="image/png",
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        input_type="image",
        source_url=None,
        source_asset=source_asset,
        status="review",
        current_stage="2 Rezepte bereit zur Auswahl",
        progress=100,
        error_message=None,
        result_recipe_id=None,
        candidates=[first_candidate, second_candidate],
    )
    fake_db.scalar_result = SimpleNamespace(
        id=batch_id,
        created_by_user_id=session.user.id,
        created_at=datetime.now(UTC),
        status="review",
        total_jobs=1,
        completed_jobs=0,
        failed_jobs=0,
        jobs=[job],
    )

    response = client.get(f"/importieren/{batch_id}")

    assert response.status_code == 200
    assert "Erkannte Rezepte auswählen" in response.text
    assert f'data-candidate-id="{first_candidate.id}"' in response.text
    assert f'value="{first_candidate.id}" data-candidate-select checked' in response.text
    assert f'value="{second_candidate.id}" data-candidate-select checked' in response.text
    assert f"/api/v1/imports/candidates/{first_candidate.id}/image" in response.text
    assert "Kartoffeln" in response.text and "Kartoffeln kochen." in response.text
    assert "Ausgewählte Rezepte übernehmen" in response.text


def test_completed_single_recipe_has_no_review_or_import_action(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    batch_id = uuid.uuid4()
    recipe_id = uuid.uuid4()
    candidate = _ready_import_candidate("Kartoffelsuppe")
    candidate.status = "imported"
    candidate.result_recipe_id = recipe_id
    job = SimpleNamespace(
        id=uuid.uuid4(),
        input_type="image",
        source_url=None,
        source_asset=SimpleNamespace(
            original_filename="rezept.png",
            mime_type="image/png",
        ),
        status="completed",
        current_stage="Rezept importiert",
        progress=100,
        error_message=None,
        result_recipe_id=recipe_id,
        candidates=[candidate],
    )
    fake_db.scalar_result = SimpleNamespace(
        id=batch_id,
        created_by_user_id=session.user.id,
        created_at=datetime.now(UTC),
        status="completed",
        total_jobs=1,
        completed_jobs=1,
        failed_jobs=0,
        jobs=[job],
    )

    response = client.get(f"/importieren/{batch_id}")

    assert response.status_code == 200
    assert "Import abgeschlossen" in response.text
    assert "page-heading__lede" not in response.text
    assert "Rezepte sind jetzt in deiner Sammlung verfügbar" not in response.text
    assert "data-candidate-review" not in response.text
    assert "data-candidate-select" not in response.text
    assert "data-candidate-selected-count" not in response.text
    assert "data-candidate-toggle-all" not in response.text
    assert "Rezept übernehmen" not in response.text
    assert f'href="/rezepte/{recipe_id}"' in response.text


def test_running_imports_page_has_empty_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)

    response = client.get("/importieren/laufend")

    assert response.status_code == 200
    assert "Keine laufenden Importe" in response.text


def test_import_history_lists_completed_and_failed_jobs_with_retry(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_login_session()
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(main, "get_session", lambda *_args: session)
    batch_id = uuid.uuid4()
    failed_job_id = uuid.uuid4()
    recipe_id = uuid.uuid4()
    fake_db.scalars_result = [
        SimpleNamespace(
            id=batch_id,
            created_at=datetime.now(UTC),
            status="completed_with_errors",
            total_jobs=2,
            completed_jobs=1,
            failed_jobs=1,
            jobs=[
                SimpleNamespace(
                    id=failed_job_id,
                    input_type="url",
                    source_url="https://example.test/kaputt",
                    source_asset=None,
                    status="failed",
                    current_stage="Fehlgeschlagen",
                    progress=25,
                    error_message="Die Webseite hat nicht rechtzeitig geladen.",
                    result_recipe_id=None,
                ),
                SimpleNamespace(
                    id=uuid.uuid4(),
                    input_type="image",
                    source_url=None,
                    source_asset=None,
                    status="completed",
                    current_stage="Import abgeschlossen",
                    progress=100,
                    error_message=None,
                    result_recipe_id=recipe_id,
                ),
            ],
        )
    ]

    response = client.get("/importieren/verlauf")

    assert response.status_code == 200
    assert "Importverlauf" in response.text
    assert "Mit Fehlern beendet" in response.text
    assert "Die Webseite hat nicht rechtzeitig geladen." in response.text
    assert f'data-job-id="{failed_job_id}"' in response.text
    assert "data-job-retry" in response.text
    assert f'href="/rezepte/{recipe_id}"' in response.text
    assert "completed_with_errors" not in response.text


def test_invalid_login_is_generic_and_uses_dummy_password_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify = Mock(return_value=False)
    monkeypatch.setattr(auth_api, "check_login_rate_limit", Mock())
    monkeypatch.setattr(auth_api, "verify_password", verify)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "unknown@example.test", "password": "definitely-wrong"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "E-Mail-Adresse oder Passwort ist falsch."
    verify.assert_called_once_with("definitely-wrong", DUMMY_PASSWORD_HASH)


def test_successful_login_normalizes_email_and_preserves_other_device_sessions(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user()
    user.email = "alice@example.test"
    user.display_name = "Alice"
    fake_db.scalar_result = user
    monkeypatch.setattr(auth_api, "check_login_rate_limit", Mock())
    clear_rate_limit = Mock()
    monkeypatch.setattr(auth_api, "clear_login_account_rate_limit", clear_rate_limit)
    monkeypatch.setattr(auth_api, "verify_password", Mock(return_value=True))
    monkeypatch.setattr(auth_api, "password_needs_rehash", Mock(return_value=False))
    monkeypatch.setattr(
        auth_api,
        "create_session",
        Mock(return_value=SimpleNamespace(csrf_token="new-csrf-token")),
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "  ALICE@EXAMPLE.TEST ", "password": "correct-password"},
    )

    assert response.status_code == 200
    assert response.json()["user"] == {
        "id": str(user.id),
        "email": "alice@example.test",
        "name": "Alice",
        "role": "member",
        "language": "de",
    }
    assert response.json()["csrf_token"] == "new-csrf-token"
    clear_rate_limit.assert_called_once_with("alice@example.test")
    assert fake_db.query_result.deleted is False
    assert fake_db.commits == 1
    assert fake_db.last_scalar_statement is not None
    compiled_params = fake_db.last_scalar_statement.compile().params
    assert "alice@example.test" in compiled_params.values()


def test_login_validation_errors_are_german_and_field_specific(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"email": "x"})

    assert response.status_code == 422
    payload = response.json()["error"]
    assert payload["code"] == "validation_error"
    assert payload["message"] == "Bitte prüfe die markierten Eingaben."
    fields = {item["field"]: item["message"] for item in payload["fields"]}
    assert fields["email"] == "Der Text ist zu kurz."
    assert fields["password"] == "Dieses Feld ist erforderlich."


def test_mutation_without_csrf_token_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[current_session] = lambda: make_login_session()
    delete_session = Mock()
    monkeypatch.setattr(auth_api, "delete_session", delete_session)

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 403
    assert "Sicherheitsprüfung" in response.json()["error"]["message"]
    delete_session.assert_not_called()


def test_csrf_token_and_same_origin_allow_mutation(
    client: TestClient, fake_db: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[current_session] = lambda: make_login_session()
    delete_session = Mock()
    monkeypatch.setattr(auth_api, "delete_session", delete_session)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "csrf-secret", "Origin": main.settings.app_base_url},
    )

    assert response.status_code == 200
    assert response.json() == {"redirect": "/login"}
    delete_session.assert_called_once()
    assert fake_db.commits == 1


def test_csrf_rejects_foreign_origin_even_with_valid_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[current_session] = lambda: make_login_session()
    delete_session = Mock()
    monkeypatch.setattr(auth_api, "delete_session", delete_session)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "csrf-secret", "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Ungültige Anfragequelle"
    delete_session.assert_not_called()


def test_member_cannot_access_admin_backup_status(client: TestClient) -> None:
    app.dependency_overrides[current_session] = lambda: make_login_session("member")

    response = client.get(f"/api/v1/settings/backups/{uuid.uuid4()}")

    assert response.status_code == 403
    assert response.json()["error"]["message"] == (
        "Diese Funktion ist Administratoren vorbehalten."
    )


def test_admin_passes_role_check_and_reaches_backup_lookup(client: TestClient) -> None:
    app.dependency_overrides[current_session] = lambda: make_login_session("admin")

    response = client.get(f"/api/v1/settings/backups/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Der Auftrag wurde nicht gefunden."
