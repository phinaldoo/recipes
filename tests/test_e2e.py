from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import pypdfium2 as pdfium
import pytest
from playwright.sync_api import BrowserContext, Error, Page, expect

from app.backups.schemas import DATABASE_SCHEMA_VERSION
from app.i18n import translate

REQUIRED_ENVIRONMENT = (
    "E2E_BASE_URL",
    "E2E_ADMIN_EMAIL",
    "E2E_ADMIN_PASSWORD",
    "E2E_MEMBER_EMAIL",
    "E2E_MEMBER_PASSWORD",
)
E2E_REQUIRED = os.environ.get("E2E_REQUIRED") == "1"
missing_environment = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
if missing_environment:
    message = "Echte Browser-E2E-Tests benötigen: " + ", ".join(missing_environment)
    if E2E_REQUIRED:
        raise pytest.UsageError(message)
    pytest.skip(message, allow_module_level=True)

if E2E_REQUIRED and os.environ.get("E2E_ALLOW_RESTORE") != "1":
    raise pytest.UsageError(
        "E2E_REQUIRED=1 verlangt E2E_ALLOW_RESTORE=1, damit das Restore-Gate nicht übersprungen wird."
    )

BASE_URL = os.environ["E2E_BASE_URL"].rstrip("/")
ADMIN_EMAIL = os.environ["E2E_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["E2E_ADMIN_PASSWORD"]
MEMBER_EMAIL = os.environ["E2E_MEMBER_EMAIL"]
MEMBER_PASSWORD = os.environ["E2E_MEMBER_PASSWORD"]

parsed_base_url = urlparse(BASE_URL)
if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
    raise pytest.UsageError("E2E_BASE_URL muss eine absolute HTTP(S)-URL sein.")
if ADMIN_EMAIL.casefold() == MEMBER_EMAIL.casefold():
    raise pytest.UsageError("Admin- und Mitgliedskonto müssen verschieden sein.")

pytestmark = pytest.mark.e2e
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NewContext = Callable[..., BrowserContext]


def login(page: Page, *, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> None:
    page.set_default_timeout(15_000)
    page.set_default_navigation_timeout(30_000)

    response = page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
    assert response is not None
    assert response.ok

    page.get_by_label("E-Mail-Adresse").fill(email)
    page.get_by_label("Passwort").fill(password)
    page.get_by_role("button", name="Anmelden", exact=True).click()

    page.wait_for_url(re.compile(rf"^{re.escape(BASE_URL)}/rezepte(?:\?.*)?$"))
    expect(page.get_by_role("heading", name="Rezepte", exact=True)).to_be_visible()


def create_recipe(
    page: Page,
    title: str,
    *,
    servings: str = "4",
    ingredient: str | None = None,
    amount: str = "",
    unit: str = "",
    description: str = "Ein automatisierter Browser-Test.",
    recipe_kind: str = "cooking",
) -> str:
    page.goto(f"{BASE_URL}/rezepte/neu")
    expect(page.get_by_role("heading", name="Rezept erstellen", exact=True)).to_be_visible()
    page.get_by_label(re.compile(r"^Titel")).fill(title)
    page.get_by_label("Kurzbeschreibung").fill(description)
    page.get_by_label(re.compile(r"^Ausgangsmenge")).fill(servings)
    page.locator(f".recipe-kind-choice--{recipe_kind}").click()
    if ingredient is not None:
        row = page.locator("[data-ingredient-row]").first
        expect(row).to_be_visible()
        row.locator("[data-amount-min]").fill(amount)
        row.locator("[data-unit]").fill(unit)
        row.locator("[data-name]").fill(ingredient)
    page.locator("[data-step-text]").first.fill(
        "Alle Zutaten vorbereiten und den automatisierten Systemtest abschließen."
    )
    page.get_by_role("button", name="Rezept speichern", exact=True).click()
    page.wait_for_url(re.compile(r"/rezepte/[0-9a-f-]+$"))
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    return page.url


@pytest.fixture
def logged_in_page(page: Page) -> Page:
    login(page)
    return page


def test_recipe_search_updates_without_document_navigation(logged_in_page: Page) -> None:
    page = logged_in_page
    page.goto(f"{BASE_URL}/rezepte", wait_until="domcontentloaded")
    marker = uuid.uuid4().hex
    page.evaluate("marker => { window.__recipeSearchDocumentMarker = marker; }", marker)
    search_term = f"Kein Treffer {uuid.uuid4().hex}"

    with page.expect_response(
        lambda response: response.request.headers.get("x-recipe-results") == "1"
    ) as searched:
        page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(search_term)

    assert searched.value.ok
    vary = {value.strip().casefold() for value in searched.value.headers["vary"].split(",")}
    assert "x-recipe-results" in vary
    assert "data-recipe-results-region" in searched.value.text()
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    expect(page.get_by_role("heading", name="Keine passenden Rezepte", exact=True)).to_be_visible()
    assert page.evaluate("window.__recipeSearchDocumentMarker") == marker

    with page.expect_response(
        lambda response: response.request.headers.get("x-recipe-results") == "1"
    ):
        page.get_by_role("link", name="Suche löschen", exact=True).click()
    page.wait_for_url(re.compile(r"/rezepte(?:\?.*)?$"))
    assert page.evaluate("window.__recipeSearchDocumentMarker") == marker

    with page.expect_response(
        lambda response: response.request.headers.get("x-recipe-results") == "1"
    ):
        page.evaluate("history.back()")
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    expect(page.get_by_role("heading", name="Keine passenden Rezepte", exact=True)).to_be_visible()
    assert page.evaluate("window.__recipeSearchDocumentMarker") == marker


def test_recipe_kind_switch_filters_cards_without_document_navigation(
    logged_in_page: Page,
) -> None:
    page = logged_in_page
    marker = f"Rezeptart {uuid.uuid4().hex[:10]}"
    cooking_title = f"{marker} Kochen"
    baking_title = f"{marker} Backen"
    cooking_url = create_recipe(page, cooking_title, recipe_kind="cooking")
    expect(page.locator(".recipe-kind-label")).to_have_text("Kochen")
    baking_url = create_recipe(page, baking_title, recipe_kind="baking")
    expect(page.locator(".recipe-kind-label")).to_have_text("Backen")
    recipe_ids = [cooking_url.rsplit("/", 1)[-1], baking_url.rsplit("/", 1)[-1]]

    try:
        page.goto(
            f"{BASE_URL}/rezepte?q={marker.replace(' ', '+')}",
            wait_until="domcontentloaded",
        )
        cards = page.locator("[data-recipe-card]")
        expect(cards).to_have_count(2)
        expect(cards.filter(has_text=cooking_title).locator(".recipe-kind-label")).to_have_text(
            "Kochen"
        )
        expect(cards.filter(has_text=baking_title).locator(".recipe-kind-label")).to_have_text(
            "Backen"
        )

        with page.expect_response(
            lambda response: response.request.headers.get("x-recipe-results") == "1"
        ) as baking_results:
            page.locator(".recipe-kind-option--baking").click()
        assert baking_results.value.ok
        page.wait_for_url(re.compile(r"/rezepte\?.*recipe_kind=baking"))
        expect(page.get_by_role("heading", name="Backen", exact=True)).to_be_visible()
        expect(cards).to_have_count(1)
        expect(cards).to_have_attribute("data-recipe-kind", "baking")
        expect(cards.get_by_role("link", name=baking_title, exact=True)).to_be_visible()

        with page.expect_response(
            lambda response: response.request.headers.get("x-recipe-results") == "1"
        ):
            page.locator(".recipe-kind-option--cooking").click()
        page.wait_for_url(re.compile(r"/rezepte\?.*recipe_kind=cooking"))
        expect(page.get_by_role("heading", name="Kochen", exact=True)).to_be_visible()
        expect(cards).to_have_count(1)
        expect(cards).to_have_attribute("data-recipe-kind", "cooking")
        expect(cards.get_by_role("link", name=cooking_title, exact=True)).to_be_visible()

        with page.expect_response(
            lambda response: response.request.headers.get("x-recipe-results") == "1"
        ):
            page.locator(".recipe-kind-option--all").click()
        page.wait_for_url(re.compile(r"/rezepte\?(?![^#]*recipe_kind)[^#]*q="))
        expect(page.get_by_role("heading", name="Rezepte", exact=True)).to_be_visible()
        expect(cards).to_have_count(2)
        assert "recipe_kind" not in page.url
    finally:
        page.evaluate(
            """async (ids) => {
              const csrf = document.querySelector('meta[name="csrf-token"]').content;
              for (const id of ids) {
                await fetch(`/api/v1/recipes/${id}`, {
                  method: 'DELETE',
                  credentials: 'same-origin',
                  headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrf },
                });
              }
            }""",
            recipe_ids,
        )


def test_recipe_list_loads_more_cards_while_scrolling_with_skeletons(
    logged_in_page: Page,
) -> None:
    page = logged_in_page
    marker = f"Endlos {uuid.uuid4().hex}"
    created_ids = page.evaluate(
        """async ({ marker, count }) => {
          const csrf = document.querySelector('meta[name="csrf-token"]').content;
          const ids = [];
          for (let index = 0; index < count; index += 1) {
            const response = await fetch('/api/v1/recipes', {
              method: 'POST',
              credentials: 'same-origin',
              headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrf,
              },
              body: JSON.stringify({
                title: `${marker} ${String(index + 1).padStart(2, '0')}`,
                base_servings: '4',
              }),
            });
            if (!response.ok) throw new Error(`Create failed: ${response.status}`);
            ids.push((await response.json()).recipe.id);
          }
          return ids;
        }""",
        {"marker": marker, "count": 25},
    )

    try:
        page.goto(
            f"{BASE_URL}/rezepte?q={marker.replace(' ', '+')}",
            wait_until="domcontentloaded",
        )
        cards = page.locator("[data-recipe-card]")
        expect(cards).to_have_count(24)
        assert page.locator(".pagination").count() == 0
        page.evaluate(
            """() => {
              const originalFetch = window.fetch.bind(window);
              window.fetch = (input, init = {}) => {
                if (init.headers?.['X-Recipe-Results'] !== 'append') {
                  return originalFetch(input, init);
                }
                return new Promise((resolve, reject) => {
                  window.setTimeout(
                    () => originalFetch(input, init).then(resolve, reject),
                    350,
                  );
                });
              };
            }"""
        )

        with page.expect_response(
            lambda response: response.request.headers.get("x-recipe-results") == "append"
        ) as appended:
            page.locator("[data-recipe-stream-sentinel]").scroll_into_view_if_needed()
            expect(page.locator("[data-recipe-stream-skeletons]")).to_be_visible()

        assert appended.value.ok
        assert "data-recipe-batch" in appended.value.text()
        expect(cards).to_have_count(25)
        expect(page.locator("[data-recipe-stream-end]")).to_contain_text("Alle 25 Rezepte")

        last_card = cards.last
        last_title = last_card.locator("h2").inner_text()
        last_card.locator("h2 a").click()
        expect(page.get_by_role("heading", name=last_title, exact=True)).to_be_visible()
        page.go_back(wait_until="domcontentloaded")
        expect(cards).to_have_count(25)
        page.wait_for_function("window.scrollY > 0")
    finally:
        page.evaluate(
            """async (ids) => {
              const csrf = document.querySelector('meta[name="csrf-token"]').content;
              for (const id of ids) {
                await fetch(`/api/v1/recipes/${id}`, {
                  method: 'DELETE',
                  credentials: 'same-origin',
                  headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrf },
                });
              }
            }""",
            created_ids,
        )


def test_manifest_installability_and_service_worker_offline_boundary(
    logged_in_page: Page,
) -> None:
    page = logged_in_page
    manifest_response = page.context.request.get(f"{BASE_URL}/manifest.webmanifest")
    assert manifest_response.ok
    assert manifest_response.headers["content-type"].startswith("application/manifest+json")

    manifest = manifest_response.json()
    assert manifest["lang"] == "de"
    assert manifest["start_url"] == "/rezepte"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    assert {shortcut["url"] for shortcut in manifest["shortcuts"]} == {
        "/rezepte",
        "/rezepte/neu",
        "/importieren",
    }

    worker_response = page.context.request.get(f"{BASE_URL}/service-worker.js")
    assert worker_response.ok
    assert worker_response.headers["service-worker-allowed"] == "/"
    assert worker_response.headers["cache-control"] == "no-cache"

    worker = worker_response.text()
    assert 'url.pathname.startsWith("/api/")' in worker
    assert 'url.pathname === "/login"' in worker
    assert 'fetch(request, { cache: "no-store" })' in worker
    assert "caches.match(OFFLINE_URL)" in worker
    assert '"/rezepte"' not in worker

    cache_name_match = re.search(r'const CACHE_NAME = "([^"]+)";', worker)
    precache_match = re.search(r"const STATIC_ASSETS = (\[.*?\]);", worker, flags=re.DOTALL)
    offline_url_match = re.search(r'const OFFLINE_URL = "([^"]+)";', worker)
    assert cache_name_match is not None
    assert precache_match is not None
    assert offline_url_match is not None
    cache_name = cache_name_match.group(1)
    precache = json.loads(precache_match.group(1))
    offline_url = offline_url_match.group(1)
    assert cache_name.startswith("rezepte-static-")
    assert offline_url in precache
    assert all(url.startswith("/static/assets/") or url == offline_url for url in precache)

    offline_response = page.context.request.get(f"{BASE_URL}/offline")
    assert offline_response.ok
    assert "Private Rezeptdaten werden nicht offline gespeichert" in offline_response.text()

    registration = page.evaluate(
        """async () => {
          const ready = await navigator.serviceWorker.ready;
          const active = ready.active;
          if (active && active.state !== "activated") {
            await new Promise((resolve) => {
              active.addEventListener("statechange", () => {
                if (active.state === "activated") resolve();
              });
            });
          }
          return {
            scope: ready.scope,
            activeScript: active?.scriptURL || null,
            activeState: active?.state || null,
          };
        }"""
    )
    assert registration == {
        "scope": f"{BASE_URL}/",
        "activeScript": f"{BASE_URL}/service-worker.js",
        "activeState": "activated",
    }
    page.wait_for_function("navigator.serviceWorker.controller !== null")

    cached_urls = page.evaluate(
        """async () => {
          const result = {};
          for (const cacheName of await caches.keys()) {
            const cache = await caches.open(cacheName);
            result[cacheName] = (await cache.keys()).map((request) => {
              const url = new URL(request.url);
              return `${url.pathname}${url.search}`;
            }).sort();
          }
          return result;
        }"""
    )
    assert set(cached_urls) == {cache_name}
    assert set(cached_urls[cache_name]) == set(precache)
    assert not any(
        path.startswith(("/api/", "/rezepte", "/einstellungen", "/login"))
        for paths in cached_urls.values()
        for path in paths
    )

    cdp = page.context.new_cdp_session(page)
    cdp.send("Page.enable")
    app_manifest = cdp.send("Page.getAppManifest")
    build_id = cache_name.removeprefix("rezepte-static-")
    assert app_manifest["url"] == f"{BASE_URL}/manifest.webmanifest?v={build_id}&lang=de"
    assert not [error for error in app_manifest.get("errors", []) if error.get("critical")]
    assert json.loads(app_manifest["data"])["name"] == "Rezepte"
    try:
        installability = cdp.send("Page.getInstallabilityErrors")
    except Error as exc:  # pragma: no cover - only older Chromium protocols
        if "wasn't found" not in str(exc) and "Method not found" not in str(exc):
            raise
    else:
        environment_only = {
            item["errorId"]
            for item in installability["installabilityErrors"]
            if "incognito" in item["errorId"].casefold()
        }
        assert {
            item["errorId"] for item in installability["installabilityErrors"]
        } == environment_only

    page.context.set_offline(True)
    try:
        page.goto(f"{BASE_URL}/rezepte", wait_until="domcontentloaded")
        expect(
            page.get_by_role("heading", name="Du bist gerade offline", exact=True)
        ).to_be_visible()
        api_result = page.evaluate(
            """async () => {
              try {
                const response = await fetch('/api/v1/settings/system-summary');
                return { resolved: true, status: response.status };
              } catch (error) {
                return { resolved: false, name: error.name };
              }
            }"""
        )
        assert api_result == {"resolved": False, "name": "TypeError"}
    finally:
        page.context.set_offline(False)


def test_complete_recipe_and_comment_lifecycle(logged_in_page: Page) -> None:
    page = logged_in_page
    suffix = uuid.uuid4().hex[:10]
    title = f"E2E Zitronenkuchen {suffix}"
    edited_title = f"{title} – bearbeitet"
    comment = f"E2E-Notiz {suffix}"
    edited_comment = f"{comment} – ergänzt"

    page.goto(f"{BASE_URL}/rezepte/neu")
    expect(page.get_by_role("heading", name="Rezept erstellen", exact=True)).to_be_visible()

    page.get_by_label(re.compile(r"^Titel")).fill(title)
    page.get_by_label("Kurzbeschreibung").fill("Ein automatisierter Browser-Test.")
    page.get_by_label(re.compile(r"^Ausgangsmenge")).fill("4")

    first_ingredient = page.locator("[data-ingredient-row]").first
    expect(first_ingredient).to_be_visible()
    first_ingredient.locator("[data-amount-min]").fill("125")
    first_ingredient.locator("[data-unit]").fill("g")
    first_ingredient.locator("[data-name]").fill("Mehl")

    first_step = page.locator("[data-step-text]").first
    expect(first_step).to_be_visible()
    first_step.fill("Alle Zutaten verrühren und backen.")

    nutrition = page.locator('[data-nutrition-row][data-basis="per_serving"]')
    nutrition.locator("summary").click()
    nutrition.get_by_label("Energie (kcal)").fill("347")
    nutrition.get_by_label("Fett (g)").fill("13")
    nutrition.get_by_label("Kohlenhydrate (g)").fill("47")
    nutrition.get_by_label("Eiweiß (g)").fill("10")
    nutrition.get_by_label("Hinweis zur Bezugsgröße").fill(
        "Eine Portion entspricht einem Viertel des Rezepts."
    )

    page.get_by_role("button", name="Rezept speichern", exact=True).click()
    page.wait_for_url(re.compile(r"/rezepte/[0-9a-f-]+$"))
    recipe_url = page.url

    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(page.get_by_text("Mehl", exact=True)).to_be_visible()
    nutrition_section = page.locator(".nutrition-section")
    expect(nutrition_section).to_contain_text("347 kcal")
    expect(nutrition_section).to_contain_text("10 g")
    expect(nutrition_section).to_contain_text("Eine Portion entspricht einem Viertel des Rezepts.")

    servings = page.locator("[data-servings-input]")
    servings.fill("8")
    expect(page.locator("[data-scaled-min]").first).to_have_text("250")
    expect(page.locator("[data-servings-note]")).to_contain_text("Für 8 Personen")
    expect(nutrition_section).to_contain_text("347 kcal")

    page.goto(f"{BASE_URL}/rezepte")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(title)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    recipe_link = page.get_by_role("link", name=title, exact=True)
    expect(recipe_link).to_be_visible()
    recipe_link.click()
    page.wait_for_url(recipe_url)

    page.get_by_role("link", name="Bearbeiten", exact=True).click()
    page.wait_for_url(f"{recipe_url}/bearbeiten")
    page.get_by_label(re.compile(r"^Titel")).fill(edited_title)
    serving_nutrition = page.locator('[data-nutrition-row][data-basis="per_serving"]')
    expect(serving_nutrition).to_have_attribute("open", "")
    expect(serving_nutrition.get_by_label("Energie (kcal)")).to_have_value("347")
    serving_nutrition.get_by_label("Eiweiß (g)").fill("11")
    page.get_by_role("button", name="Änderungen speichern", exact=True).click()
    page.wait_for_url(recipe_url)
    expect(page.get_by_role("heading", name=edited_title, exact=True)).to_be_visible()
    expect(page.locator(".nutrition-section")).to_contain_text("11 g")

    page.get_by_label("Notiz hinzufügen").fill(comment)
    page.get_by_role("button", name="Notiz hinzufügen", exact=True).click()
    comment_card = page.locator("[data-comment-id]").filter(has_text=comment)
    expect(comment_card).to_have_count(1)
    comment_id = comment_card.get_attribute("data-comment-id")
    assert comment_id is not None
    comment_card = page.locator(f'[data-comment-id="{comment_id}"]')

    comment_card.get_by_role("button", name="Bearbeiten", exact=True).click()
    comment_card.get_by_label("Notiz bearbeiten").fill(edited_comment)
    comment_card.get_by_role("button", name="Speichern", exact=True).click()
    expect(comment_card.locator("[data-comment-text]")).to_have_text(edited_comment)

    page.once("dialog", lambda dialog: dialog.accept())
    comment_card.get_by_role("button", name="Löschen", exact=True).click()
    expect(page.locator(f'[data-comment-id="{comment_id}"]')).to_have_count(0)
    expect(page.locator("[data-comments-empty]")).to_have_text("Noch keine gemeinsamen Notizen.")

    page.get_by_text("Weitere Aktionen", exact=True).click()
    page.locator("[data-delete-recipe]").click()
    delete_dialog = page.locator("[data-delete-dialog]")
    expect(delete_dialog).to_be_visible()
    delete_dialog.get_by_role("button", name="Rezept löschen", exact=True).click()
    page.wait_for_url(re.compile(r"/rezepte$"))

    trash_response = page.context.request.get(f"{BASE_URL}/papierkorb", params={"q": edited_title})
    assert trash_response.ok
    assert edited_title in trash_response.text()


def test_main_recipe_cards_toggle_favorites(logged_in_page: Page) -> None:
    page = logged_in_page
    title = f"E2E Kartenfavorit {uuid.uuid4().hex[:10]}"
    recipe_url = create_recipe(page, title)

    page.goto(f"{BASE_URL}/rezepte")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(title)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    recipe_card = page.locator(".recipe-card").filter(has_text=title)
    favorite = recipe_card.locator("[data-favorite-button]")
    expect(favorite).to_be_visible()
    expect(favorite).to_have_attribute("data-favorite-variant", "icon")
    expect(favorite).to_have_attribute("aria-pressed", "false")
    expect(favorite).to_have_attribute("aria-label", f"{title} zu Favoriten hinzufügen")

    offsets = favorite.evaluate(
        """button => {
          const buttonRect = button.getBoundingClientRect();
          const cardRect = button.closest('.recipe-card').getBoundingClientRect();
          return {
            top: buttonRect.top - cardRect.top,
            right: cardRect.right - buttonRect.right,
          };
        }"""
    )
    assert 8 <= offsets["top"] <= 20
    assert 8 <= offsets["right"] <= 20

    with page.expect_response(lambda response: "/api/v1/favorites/" in response.url) as added:
        favorite.click()
    assert added.value.ok
    assert added.value.request.method == "PUT"
    expect(favorite).to_have_attribute("aria-pressed", "true")
    expect(favorite).to_have_attribute("aria-label", f"{title} aus Favoriten entfernen")

    page.reload(wait_until="domcontentloaded")
    expect(favorite).to_have_attribute("aria-pressed", "true")
    page.goto(f"{BASE_URL}/favoriten")
    favorite_card = page.locator(".recipe-card").filter(has_text=title)
    favorite = favorite_card.locator("[data-favorite-button]")
    expect(favorite_card.get_by_role("link", name=title, exact=True)).to_be_visible()
    expect(favorite).to_be_visible()
    expect(favorite).to_have_attribute("data-favorite-variant", "icon")
    expect(favorite).to_have_attribute("aria-pressed", "true")
    expect(favorite).to_have_attribute("aria-label", f"{title} aus Favoriten entfernen")
    expect(favorite_card.locator(".button--text")).to_have_count(0)

    with page.expect_response(lambda response: "/api/v1/favorites/" in response.url) as removed:
        favorite.click()
    assert removed.value.ok
    assert removed.value.request.method == "DELETE"
    expect(favorite_card).to_have_count(0)

    page.goto(f"{BASE_URL}/rezepte")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(title)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    favorite = page.locator(".recipe-card").filter(has_text=title).locator("[data-favorite-button]")
    expect(favorite).to_have_attribute("aria-pressed", "false")

    with page.expect_response(lambda response: "/api/v1/favorites/" in response.url) as readded:
        favorite.click()
    assert readded.value.ok
    assert readded.value.request.method == "PUT"
    expect(favorite).to_have_attribute("aria-pressed", "true")

    with page.expect_response(
        lambda response: "/api/v1/favorites/" in response.url
    ) as removed_again:
        favorite.click()
    assert removed_again.value.ok
    assert removed_again.value.request.method == "DELETE"
    expect(favorite).to_have_attribute("aria-pressed", "false")
    expect(favorite).to_have_attribute("aria-label", f"{title} zu Favoriten hinzufügen")
    page.goto(f"{BASE_URL}/favoriten")
    expect(page.get_by_role("link", name=title, exact=True)).to_have_count(0)

    page.goto(recipe_url)
    page.get_by_text("Weitere Aktionen", exact=True).click()
    page.locator("[data-delete-recipe]").click()
    page.locator("[data-delete-dialog]").get_by_role(
        "button", name="Rezept löschen", exact=True
    ).click()
    page.wait_for_url(re.compile(r"/rezepte$"))


def test_two_users_share_inventory_comments_and_role_boundaries(
    logged_in_page: Page, new_context: NewContext
) -> None:
    admin_page = logged_in_page
    suffix = uuid.uuid4().hex[:10]
    title = f"E2E Gemeinschaftsgericht {suffix}"
    admin_comment = f"Admin-Hinweis {suffix}"
    member_comment = f"Mitglied-Hinweis {suffix}"
    recipe_url = create_recipe(
        admin_page,
        title,
        ingredient="Gemeinschaftszutat",
        amount="3",
        unit="Stück",
    )

    admin_page.get_by_label("Notiz hinzufügen").fill(admin_comment)
    admin_page.get_by_role("button", name="Notiz hinzufügen", exact=True).click()
    expect(admin_page.locator("[data-comment-id]").filter(has_text=admin_comment)).to_have_count(1)

    member_context = new_context()
    try:
        member_page = member_context.new_page()
        login(member_page, email=MEMBER_EMAIL, password=MEMBER_PASSWORD)
        member_page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(title)
        member_page.wait_for_url(re.compile(r"/rezepte\?.*q="))
        expect(member_page.get_by_role("link", name=title, exact=True)).to_be_visible()
        member_page.get_by_role("link", name=title, exact=True).click()
        member_page.wait_for_url(recipe_url)
        expect(member_page.get_by_text("Gemeinschaftszutat", exact=True)).to_be_visible()

        foreign_comment = member_page.locator("[data-comment-id]").filter(has_text=admin_comment)
        expect(foreign_comment).to_have_count(1)
        expect(foreign_comment.get_by_role("button")).to_have_count(0)

        member_page.get_by_label("Notiz hinzufügen").fill(member_comment)
        member_page.get_by_role("button", name="Notiz hinzufügen", exact=True).click()
        own_comment = member_page.locator("[data-comment-id]").filter(has_text=member_comment)
        expect(own_comment).to_have_count(1)
        expect(own_comment.get_by_role("button", name="Bearbeiten", exact=True)).to_be_visible()

        settings_response = member_page.goto(
            f"{BASE_URL}/einstellungen", wait_until="domcontentloaded"
        )
        assert settings_response is not None
        assert settings_response.status == 403
        settings_api = member_context.request.get(
            f"{BASE_URL}/api/v1/settings/system-summary", fail_on_status_code=False
        )
        assert settings_api.status == 403
        assert settings_api.json()["error"]["message"] == (
            "Diese Funktion ist Administratoren vorbehalten."
        )
    finally:
        member_context.close()

    admin_page.goto(recipe_url)
    shared_comment = admin_page.locator("[data-comment-id]").filter(has_text=member_comment)
    expect(shared_comment).to_have_count(1)
    expect(shared_comment.get_by_role("button", name="Löschen", exact=True)).to_be_visible()
    admin_page.once("dialog", lambda dialog: dialog.accept())
    shared_comment.get_by_role("button", name="Löschen", exact=True).click()
    expect(shared_comment).to_have_count(0)

    admin_page.get_by_text("Weitere Aktionen", exact=True).click()
    admin_page.locator("[data-delete-recipe]").click()
    admin_page.locator("[data-delete-dialog]").get_by_role(
        "button", name="Rezept löschen", exact=True
    ).click()
    admin_page.wait_for_url(re.compile(r"/rezepte$"))


def test_v1_organizing_and_share_lifecycles(logged_in_page: Page, new_context: NewContext) -> None:
    page = logged_in_page
    suffix = uuid.uuid4().hex[:10]
    title = f"E2E Organisationsgericht {suffix}"
    tag_name = f"E2E Ordnung {suffix}"
    renamed_tag = f"E2E Ordnung Neu {suffix}"
    search_alias = f"Haferalias{suffix}"
    search_target = f"Hafer {suffix}"
    recipe_url = create_recipe(
        page,
        title,
        servings="2",
        ingredient=f"E2E Hafer {suffix}",
        amount="200",
        unit="g",
        description="Erste Fassung für den Produktivitätstest.",
    )

    page.goto(f"{BASE_URL}/schlagwoerter")
    page.get_by_label("Neues Schlagwort").fill(tag_name)
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("[data-tag-form]").get_by_role("button", name="Anlegen").click()
    tag_row = page.locator("[data-tag-id]").filter(has_text=tag_name)
    expect(tag_row).to_have_count(1)
    page.once("dialog", lambda dialog: dialog.accept(renamed_tag))
    with page.expect_navigation(wait_until="domcontentloaded"):
        tag_row.locator("[data-tag-rename]").click()
    tag_row = page.locator("[data-tag-id]").filter(has_text=renamed_tag)
    expect(tag_row).to_have_count(1)
    page.once("dialog", lambda dialog: dialog.accept())
    tag_row.locator("[data-tag-delete]").click()
    expect(tag_row).to_have_count(0)

    page.get_by_label("Begriff").fill(search_alias)
    page.get_by_role("textbox", name="Synonym", exact=True).fill(search_target)
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("[data-synonym-form]").get_by_role("button", name="Verbinden").click()
    synonym_row = page.locator("[data-synonym-id]").filter(has_text=search_alias)
    expect(synonym_row).to_have_count(1)
    page.goto(f"{BASE_URL}/rezepte")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(search_alias)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    expect(page.get_by_role("link", name=title, exact=True)).to_be_visible()

    with page.expect_response(lambda response: "/api/v1/favorites/" in response.url) as state:
        page.goto(recipe_url)
    assert state.value.ok
    favorite = page.locator("[data-favorite-button]")
    expect(favorite).to_have_attribute("aria-pressed", "false")
    favorite.click()
    expect(favorite).to_have_attribute("aria-pressed", "true")
    page.goto(f"{BASE_URL}/favoriten")
    expect(page.get_by_role("link", name=title, exact=True)).to_be_visible()

    member_context = new_context()
    try:
        member_page = member_context.new_page()
        login(member_page, email=MEMBER_EMAIL, password=MEMBER_PASSWORD)
        member_page.goto(f"{BASE_URL}/favoriten")
        expect(member_page.get_by_role("link", name=title, exact=True)).to_have_count(0)
    finally:
        member_context.close()

    page.goto(f"{recipe_url}/bearbeiten")
    page.get_by_label("Kurzbeschreibung").fill("Zweite Fassung mit dokumentierter Änderung.")
    page.get_by_role("button", name="Änderungen speichern", exact=True).click()
    page.wait_for_url(recipe_url)
    page.goto(f"{recipe_url}/verlauf")
    history = page.locator("[data-version-history]")
    expect(history.locator(".version-card")).to_have_count(2)
    expect(history.get_by_text("Version 2", exact=True)).to_be_visible()
    history.locator(".version-card").first.locator("details").evaluate(
        "element => element.open = true"
    )
    expect(history.locator(".version-card").first).to_contain_text("Beschreibung")
    expect(history.locator(".version-card").first).to_contain_text(
        "Zweite Fassung mit dokumentierter Änderung."
    )
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_navigation(wait_until="domcontentloaded"):
        history.get_by_role("button", name="Diesen Stand wiederherstellen", exact=True).click()
    page.wait_for_url(recipe_url)
    expect(
        page.get_by_text("Erste Fassung für den Produktivitätstest.", exact=True)
    ).to_be_visible()
    page.goto(f"{recipe_url}/verlauf")
    expect(page.locator("[data-version-history] .version-card")).to_have_count(3)

    page.goto(f"{recipe_url}/teilen")
    page.locator("[data-share-form]").get_by_role("button", name="Link erstellen").click()
    share_input = page.locator("[data-share-url]")
    expect(share_input).to_be_visible()
    share_url = share_input.input_value()
    assert share_url.startswith(f"{BASE_URL}/freigabe/")

    public_context = new_context()
    try:
        public_page = public_context.new_page()
        public_response = public_page.goto(share_url, wait_until="domcontentloaded")
        assert public_response is not None
        assert public_response.ok
        expect(public_page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(public_page.get_by_text("Datenschutz:", exact=False)).to_have_count(0)
        expect(public_page.get_by_text("Gemeinsame Notizen", exact=True)).to_have_count(0)

        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator("[data-share-revoke]").click()
        expect(page.get_by_text("Widerrufen", exact=True)).to_be_visible()
        revoked_response = public_page.reload(wait_until="domcontentloaded")
        assert revoked_response is not None
        assert revoked_response.status == 404
    finally:
        public_context.close()

    page.goto(f"{BASE_URL}/schlagwoerter")
    synonym_row = page.locator("[data-synonym-id]").filter(has_text=search_alias)
    expect(synonym_row).to_have_count(1)
    synonym_row.locator("[data-synonym-delete]").click()
    expect(synonym_row).to_have_count(0)

    page.goto(recipe_url)
    page.get_by_text("Weitere Aktionen", exact=True).click()
    page.locator("[data-delete-recipe]").click()
    page.locator("[data-delete-dialog]").get_by_role(
        "button", name="Rezept löschen", exact=True
    ).click()
    page.wait_for_url(re.compile(r"/rezepte$"))


def test_mobile_and_desktop_navigation_fit_the_viewport(logged_in_page: Page) -> None:
    page = logged_in_page

    for width, height in ((320, 568), (390, 844), (768, 1024)):
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{BASE_URL}/rezepte")
        expect(page.locator(".mobile-nav")).to_be_visible()
        expect(page.locator(".desktop-nav")).to_be_hidden()
        expect(
            page.locator(".mobile-nav").get_by_role("link", name="Neu", exact=True)
        ).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    for width in (896, 1280):
        page.set_viewport_size({"width": width, "height": 900})
        page.reload()
        expect(page.locator(".desktop-nav")).to_be_visible()
        expect(page.locator(".mobile-nav")).to_be_hidden()
        add_recipe_menu = page.locator(".desktop-nav .add-recipe-menu")
        expect(add_recipe_menu.locator("summary")).to_contain_text("Rezept hinzufügen")
        add_recipe_menu.locator("summary").click()
        expect(add_recipe_menu.get_by_role("link", name="Importieren", exact=True)).to_be_visible()
        expect(
            add_recipe_menu.get_by_role("link", name="Frei erstellen", exact=True)
        ).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_mobile_import_appends_repeated_photo_selections(logged_in_page: Page) -> None:
    page = logged_in_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE_URL}/importieren")

    file_input = page.locator("#import-files")
    selection = page.locator("[data-file-selection]")
    submit = page.get_by_role("button", name="Dateien importieren", exact=True)
    expect(submit).to_be_disabled()

    file_input.set_input_files(PROJECT_ROOT / "app/static/pwa/icon-192.png")
    expect(selection.locator(".file-selection__item")).to_have_count(1)
    expect(selection.locator(".file-selection__summary")).to_have_text(
        "1 von 20 Dateien ausgewählt"
    )

    file_input.set_input_files(PROJECT_ROOT / "app/static/pwa/icon-512.png")
    expect(selection.locator(".file-selection__item")).to_have_count(2)
    expect(selection.locator(".file-selection__summary")).to_have_text(
        "2 von 20 Dateien ausgewählt"
    )
    expect(submit).to_be_enabled()

    page.route(
        "**/api/v1/imports/files",
        lambda route: route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({"redirect": "/importieren"}),
        ),
    )
    with page.expect_request("**/api/v1/imports/files") as upload:
        submit.click()

    upload_body = upload.value.post_data_buffer
    assert upload_body is not None
    assert upload_body.count(b'name="files"') == 2
    assert b'filename="icon-192.png"' in upload_body
    assert b'filename="icon-512.png"' in upload_body


def test_image_upload_json_export_and_pdf_render(logged_in_page: Page) -> None:
    page = logged_in_page
    title = f"E2E Exportgericht {uuid.uuid4().hex[:10]}"

    page.goto(f"{BASE_URL}/rezepte/neu")
    page.get_by_label(re.compile(r"^Titel")).fill(title)
    page.get_by_label(re.compile(r"^Ausgangsmenge")).fill("2")
    nutrition = page.locator('[data-nutrition-row][data-basis="per_100g_ml"]')
    nutrition.locator("summary").click()
    nutrition.get_by_label("Energie (kJ)").fill("167")
    nutrition.get_by_label("Energie (kcal)").fill("40")
    nutrition.get_by_label("Kohlenhydrate (g)").fill("5")
    page.locator("[data-image-files]").set_input_files(PROJECT_ROOT / "app/static/pwa/icon-192.png")
    page.get_by_role("button", name="Rezept speichern", exact=True).click()
    page.wait_for_url(re.compile(r"/rezepte/[0-9a-f-]+$"))
    recipe_url = page.url

    gallery_image = page.locator(".recipe-gallery__main img")
    expect(gallery_image).to_be_visible()
    image_path = gallery_image.get_attribute("src")
    assert image_path is not None
    image_response = page.context.request.get(f"{BASE_URL}{image_path}")
    assert image_response.ok
    assert image_response.headers["content-type"].startswith("image/png")
    assert len(image_response.body()) > 100

    json_path = page.locator("[data-json-export]").get_attribute("href")
    assert json_path is not None
    json_response = page.context.request.get(f"{BASE_URL}{json_path}")
    assert json_response.ok
    package = json_response.json()
    assert package["schema_version"] == "1.3"
    assert package["recipe"]["title"] == title
    assert package["recipe"]["nutrition"] == [
        {
            "basis": "per_100g_ml",
            "energy_kj": "167.0000",
            "energy_kcal": "40.0000",
            "fat_g": None,
            "saturated_fat_g": None,
            "carbohydrates_g": "5.0000",
            "sugars_g": None,
            "fiber_g": None,
            "protein_g": None,
            "salt_g": None,
            "note": None,
        }
    ]
    assert len(package["recipe"]["images"]) == 1
    assert package["recipe"]["images"][0]["data_base64"]

    pdf_path = page.locator("[data-pdf-export]").get_attribute("href")
    assert pdf_path is not None
    pdf_response = page.context.request.get(f"{BASE_URL}{pdf_path}", timeout=30_000)
    assert pdf_response.ok
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    assert pdf_response.body().startswith(b"%PDF")
    pdf_text_parts: list[str] = []
    pdf_image_count = 0
    with pdfium.PdfDocument(pdf_response.body()) as document:
        for page_index in range(len(document)):
            pdf_page = document[page_index]
            try:
                text_page = pdf_page.get_textpage()
                try:
                    pdf_text_parts.append(text_page.get_text_range())
                finally:
                    text_page.close()
                pdf_image_count += sum(
                    1 for _ in pdf_page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE])
                )
            finally:
                pdf_page.close()
    pdf_text = "\n".join(pdf_text_parts)
    assert "Brenn- und Nährwerte" in pdf_text
    assert "40 kcal" in pdf_text
    assert pdf_image_count >= 1

    print_path = page.locator("[data-print-link]").get_attribute("href")
    assert print_path is not None
    page.goto(f"{BASE_URL}{print_path}")
    expect(page.locator(".recipe-images img")).to_be_visible()
    print_image_path = page.locator(".recipe-images img").get_attribute("src")
    assert print_image_path is not None
    print_image_response = page.context.request.get(f"{BASE_URL}{print_image_path}")
    assert print_image_response.ok
    assert print_image_response.headers["content-type"].startswith("image/")
    page.goto(recipe_url)

    page.get_by_text("Weitere Aktionen", exact=True).click()
    page.locator("[data-delete-recipe]").click()
    page.locator("[data-delete-dialog]").get_by_role(
        "button", name="Rezept löschen", exact=True
    ).click()
    page.wait_for_url(re.compile(r"/rezepte$"))


def test_recipe_editor_accepts_an_image_pasted_from_the_clipboard(
    logged_in_page: Page,
) -> None:
    page = logged_in_page
    title = f"E2E Zwischenablage {uuid.uuid4().hex[:10]}"
    recipe_url = create_recipe(page, title)
    page.goto(f"{recipe_url}/bearbeiten")

    encoded_image = base64.b64encode(
        (PROJECT_ROOT / "app/static/pwa/icon-192.png").read_bytes()
    ).decode("ascii")
    paste_zone = page.locator("[data-image-paste-zone]")
    paste_zone.evaluate(
        """(zone, encoded) => {
          const binary = atob(encoded);
          const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
          const transfer = new DataTransfer();
          transfer.items.add(new File([bytes], "clipboard-source.png", { type: "image/png" }));
          const paste = new Event("paste", { bubbles: true, cancelable: true });
          Object.defineProperty(paste, "clipboardData", { value: transfer });
          zone.focus();
          zone.dispatchEvent(paste);
        }""",
        encoded_image,
    )

    preview = page.locator("[data-image-preview] figure")
    expect(preview).to_have_count(1)
    expect(preview).to_contain_text("Neues Titelbild")
    expect(preview).to_contain_text("Zwischenablage")
    expect(page.locator("[data-image-message]")).to_contain_text(
        translate("de", "form.images_added.one")
    )

    page.get_by_role("button", name="Änderungen speichern", exact=True).click()
    page.wait_for_url(recipe_url)
    expect(page.locator(".recipe-gallery__main img")).to_be_visible()
    page.get_by_text("Weitere Aktionen", exact=True).click()
    page.locator("[data-delete-recipe]").click()
    page.locator("[data-delete-dialog]").get_by_role(
        "button", name="Rezept löschen", exact=True
    ).click()
    page.wait_for_url(re.compile(r"/rezepte$"))


def inspect_backup_archive(archive_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert {"manifest.json", "application-data.json", "checksums.sha256"} <= set(names)
        assert all(
            name in {"manifest.json", "application-data.json", "checksums.sha256"}
            or name.startswith("media/")
            for name in names
        )
        assert all(
            not name.startswith("/") and ".." not in Path(name).parts and "\\" not in name
            for name in names
        )

        checksum_lines = archive.read("checksums.sha256").decode("utf-8").splitlines()
        checksums: dict[str, str] = {}
        for line in checksum_lines:
            digest, name = line.split("  ", 1)
            assert re.fullmatch(r"[0-9a-f]{64}", digest)
            assert name not in checksums
            checksums[name] = digest
        expected_checksum_names = {
            name for name in names if name != "checksums.sha256" and not name.endswith("/")
        }
        assert set(checksums) == expected_checksum_names
        for name, expected_digest in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected_digest

        manifest = json.loads(archive.read("manifest.json"))
        application_data = json.loads(archive.read("application-data.json"))
        assert manifest["backup_format_version"] == "1.0"
        assert manifest["database_schema_version"] == DATABASE_SCHEMA_VERSION
        assert manifest["archive_contents"] == [
            "manifest.json",
            "application-data.json",
            "checksums.sha256",
            "media/",
        ]
        assert application_data["format"] == "rezeptverwaltung-application-data"
        assert application_data["version"] == "1.0"
        assert manifest["counts"] == {
            table: len(rows) for table, rows in application_data["tables"].items()
        }
        media_names = [
            name for name in names if name.startswith("media/") and not name.endswith("/")
        ]
        assert manifest["media_file_count"] == len(media_names)
        assert manifest["media_total_bytes"] == sum(
            archive.getinfo(name).file_size for name in media_names
        )

        sentinel_values = {
            ADMIN_PASSWORD,
            MEMBER_PASSWORD,
            *filter(None, os.environ.get("E2E_SECRET_SENTINELS", "").split("|")),
        }
        forbidden_names = {
            b"app_secret_key",
            b"renderer_token",
            b"ai_api_key",
            b"postgres_password",
            b"database_url",
            b"redis_url",
        }
        for name in names:
            if name.endswith("/"):
                continue
            content = archive.read(name)
            assert all(value.encode("utf-8") not in content for value in sentinel_values)
            assert all(field not in content.lower() for field in forbidden_names)
    return cast(dict[str, object], manifest)


def test_full_backup_and_restore_round_trip(
    logged_in_page: Page, new_context: NewContext, tmp_path: Path
) -> None:
    if os.environ.get("E2E_ALLOW_RESTORE") != "1":
        if E2E_REQUIRED:
            pytest.fail(
                "Das verpflichtende Restore-Gate darf nicht übersprungen werden.", pytrace=False
            )
        pytest.skip("Der destruktive Restore-E2E-Test braucht E2E_ALLOW_RESTORE=1.")

    page = logged_in_page
    suffix = uuid.uuid4().hex[:10]
    preserved_title = f"E2E Backupbestand {suffix}"
    discarded_title = f"E2E Nach-Backup-Änderung {suffix}"
    preserved_comment = f"E2E Backup-Notiz {suffix}"
    preserved_ingredient = f"E2E Backup-Zutat {suffix}"

    preserved_url = create_recipe(
        page,
        preserved_title,
        servings="2",
        ingredient=preserved_ingredient,
        amount="1",
        unit="kg",
    )
    page.goto(f"{preserved_url}/bearbeiten")
    page.locator("[data-image-files]").set_input_files(PROJECT_ROOT / "app/static/pwa/icon-192.png")
    page.get_by_role("button", name="Änderungen speichern", exact=True).click()
    page.wait_for_url(preserved_url)
    with page.expect_response(lambda response: "/api/v1/favorites/" in response.url) as state:
        page.reload(wait_until="domcontentloaded")
    assert state.value.ok
    preserved_image_path = page.locator(".recipe-gallery__main img").get_attribute("src")
    assert preserved_image_path is not None
    preserved_image_response = page.context.request.get(f"{BASE_URL}{preserved_image_path}")
    assert preserved_image_response.ok
    preserved_image_digest = hashlib.sha256(preserved_image_response.body()).hexdigest()
    page.get_by_label("Notiz hinzufügen").fill(preserved_comment)
    page.get_by_role("button", name="Notiz hinzufügen", exact=True).click()
    expect(page.locator("[data-comment-id]").filter(has_text=preserved_comment)).to_have_count(1)
    page.locator("[data-favorite-button]").click()
    expect(page.locator("[data-favorite-button]")).to_have_attribute("aria-pressed", "true")

    summary_before_response = page.context.request.get(f"{BASE_URL}/api/v1/settings/system-summary")
    assert summary_before_response.ok
    summary_before = summary_before_response.json()

    page.goto(f"{BASE_URL}/einstellungen")
    expect(page.get_by_role("heading", name="Einstellungen", exact=True)).to_be_visible()
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.get_by_role("button", name="Backup erstellen", exact=True).click()

    backup_card = page.locator('[data-maintenance-job][data-operation="export"]').first
    expect(backup_card).to_be_visible()
    expect(backup_card.locator(".status-badge")).to_have_text(
        "Backup wurde vollständig geprüft", timeout=60_000
    )
    download_path = backup_card.get_by_role("link", name="Herunterladen").get_attribute("href")
    assert download_path is not None
    download_response = page.context.request.get(f"{BASE_URL}{download_path}")
    assert download_response.ok
    assert download_response.headers["content-type"].startswith("application/zip")
    archive = tmp_path / "round-trip-backup.zip"
    archive.write_bytes(download_response.body())
    assert archive.stat().st_size > 100
    manifest = inspect_backup_archive(archive)
    manifest_counts = cast(dict[str, int], manifest["counts"])
    assert manifest_counts["users"] == 2
    assert manifest_counts["recipe_comments"] >= 1
    assert cast(int, manifest["media_file_count"]) >= 1

    create_recipe(page, discarded_title, servings="2")

    page.goto(f"{BASE_URL}/einstellungen")
    page.locator("#restore-file").set_input_files(archive)
    expect(page.locator("[data-restore-file-summary]")).to_contain_text(archive.name)
    upload_button = page.get_by_role("button", name="Backup hochladen und prüfen", exact=True)
    expect(upload_button).to_be_enabled()
    page.evaluate(
        """() => {
          window.__restoreStatusHistory = [];
          const title = document.querySelector('[data-restore-status-title]');
          const record = () => {
            const value = title?.textContent?.trim();
            if (value && !window.__restoreStatusHistory.includes(value)) {
              window.__restoreStatusHistory.push(value);
            }
          };
          new MutationObserver(record).observe(title, { childList: true, subtree: true });
        }"""
    )
    upload_button.click()
    preflight = page.locator("[data-restore-preflight]")
    expect(preflight.get_by_role("heading", name="Vorabprüfung bestanden")).to_be_visible(
        timeout=30_000
    )
    status_history = page.evaluate("window.__restoreStatusHistory")
    assert "Backup wird hochgeladen" in status_history
    assert "Upload abgeschlossen – Backup wird geprüft" in status_history
    preflight.get_by_role("button", name="Wiederherstellung vorbereiten").click()

    dialog = page.locator("[data-restore-dialog]")
    expect(dialog).to_be_visible()
    replace_button = dialog.get_by_role("button", name="Serverbestand ersetzen", exact=True)
    expect(replace_button).to_be_disabled()
    dialog.get_by_label("Passwort").fill(ADMIN_PASSWORD)
    dialog.get_by_label("Bestätigung").fill("WIEDERHERSTELLEN")
    expect(replace_button).to_be_enabled()
    replace_button.click()

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status_response = page.context.request.get(
            f"{BASE_URL}/api/v1/settings/system-summary", fail_on_status_code=False
        )
        if status_response.status == 401:
            break
        time.sleep(0.5)
    else:
        pytest.fail("Die Wiederherstellung hat die alte Sitzung nicht rechtzeitig invalidiert.")

    login(page)
    page.goto(f"{BASE_URL}/rezepte", wait_until="domcontentloaded")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(preserved_title)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    preserved_link = page.get_by_role("link", name=preserved_title, exact=True)
    expect(preserved_link).to_be_visible()
    preserved_link.click()
    page.wait_for_url(preserved_url)
    expect(page.get_by_text(preserved_ingredient, exact=True)).to_be_visible()
    expect(page.locator("[data-comment-id]").filter(has_text=preserved_comment)).to_have_count(1)
    restored_image_path = page.locator(".recipe-gallery__main img").get_attribute("src")
    assert restored_image_path is not None
    restored_image = page.context.request.get(f"{BASE_URL}{restored_image_path}")
    assert restored_image.ok
    assert hashlib.sha256(restored_image.body()).hexdigest() == preserved_image_digest

    page.goto(f"{BASE_URL}/favoriten")
    expect(page.get_by_role("link", name=preserved_title, exact=True)).to_be_visible()

    summary_after_response = page.context.request.get(f"{BASE_URL}/api/v1/settings/system-summary")
    assert summary_after_response.ok
    assert summary_after_response.json() == summary_before

    page.goto(f"{BASE_URL}/rezepte", wait_until="domcontentloaded")
    page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(discarded_title)
    page.wait_for_url(re.compile(r"/rezepte\?.*q="))
    expect(page.get_by_role("link", name=discarded_title, exact=True)).to_have_count(0)

    member_context = new_context()
    try:
        member_page = member_context.new_page()
        login(member_page, email=MEMBER_EMAIL, password=MEMBER_PASSWORD)
        member_page.get_by_role("searchbox", name="Rezepte durchsuchen").fill(preserved_title)
        member_page.wait_for_url(re.compile(r"/rezepte\?.*q="))
        expect(member_page.get_by_role("link", name=preserved_title, exact=True)).to_be_visible()
        forbidden = member_context.request.get(
            f"{BASE_URL}/api/v1/settings/system-summary", fail_on_status_code=False
        )
        assert forbidden.status == 403
    finally:
        member_context.close()
