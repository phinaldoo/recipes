from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.assets import FrontendAssets
from app.backups.preflight import InvalidBackup, safe_archive_name
from app.config import Settings
from app.i18n import MESSAGES
from app.imports.url_security import UnsafeURL, validate_http_url_shape, validate_public_url
from app.schemas.ai import CategorySuggestion, ExtractedRecipe
from app.schemas.recipe import (
    CategoryPathInput,
    EncodedAsset,
    IngredientInput,
    NutritionInput,
    RecipeInput,
    RecipePackageData,
)
from app.services.scaling import format_amount, format_decimal, format_duration, scale_amount

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "production",
        "app_secret_key": "a" * 64,
        "app_base_url": "https://rezepte.example.test",
        "allowed_hosts": "rezepte.example.test",
        "force_https": True,
        "session_cookie_secure": True,
        "renderer_token": "b" * 64,
        "database_url": ("postgresql+psycopg://recipe:strong-production-password@db:5432/recipe"),
    }
    values.update(overrides)
    return Settings(**values)


def test_production_settings_accept_a_hardened_configuration() -> None:
    settings = _production_settings()

    assert settings.force_https is True
    assert settings.session_cookie_secure is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"force_https": False}, "FORCE_HTTPS"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({"app_base_url": "http://rezepte.example.test"}, "HTTPS"),
        ({"allowed_hosts": "other.example.test"}, "ALLOWED_HOSTS"),
        (
            {
                "database_url": (
                    "postgresql+psycopg://recipe:strong-production-password@localhost:5432/recipe"
                )
            },
            "DATABASE_URL",
        ),
    ],
)
def test_production_settings_reject_insecure_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _production_settings(**overrides)


@pytest.mark.parametrize(
    ("amount", "base_servings", "desired_servings", "expected"),
    [
        (Decimal("1"), Decimal("4"), Decimal("6"), Decimal("1.5")),
        (Decimal("0.667"), Decimal("1"), Decimal("0.5"), Decimal("0.334")),
        (Decimal("10"), Decimal("4"), Decimal("2"), Decimal("5")),
        (Decimal("0"), Decimal("4"), Decimal("12"), Decimal("0")),
    ],
)
def test_scale_amount_uses_decimal_arithmetic_and_half_up_rounding(
    amount: Decimal,
    base_servings: Decimal,
    desired_servings: Decimal,
    expected: Decimal,
) -> None:
    assert (
        scale_amount(
            amount,
            base_servings=base_servings,
            desired_servings=desired_servings,
        )
        == expected
    )


def test_scale_amount_preserves_unknown_and_non_scalable_values() -> None:
    assert (
        scale_amount(
            None,
            base_servings=Decimal("4"),
            desired_servings=Decimal("8"),
        )
        is None
    )
    assert scale_amount(
        Decimal("3"),
        base_servings=Decimal("4"),
        desired_servings=Decimal("8"),
        scalable=False,
    ) == Decimal("3")


@pytest.mark.parametrize(
    ("base_servings", "desired_servings"),
    [
        (Decimal("0"), Decimal("1")),
        (Decimal("-1"), Decimal("1")),
        (Decimal("1"), Decimal("0")),
        (Decimal("1"), Decimal("-1")),
    ],
)
def test_scale_amount_rejects_non_positive_serving_counts(
    base_servings: Decimal,
    desired_servings: Decimal,
) -> None:
    with pytest.raises(ValueError, match="größer als null"):
        scale_amount(
            Decimal("1"),
            base_servings=base_servings,
            desired_servings=desired_servings,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (Decimal("0"), "0"),
        (Decimal("0.5"), "½"),
        (Decimal("1.25"), "1¼"),
        (Decimal("10.5"), "10½"),
        (Decimal("10"), "10"),
        (Decimal("2.125"), "2,125"),
        (Decimal("3.000"), "3"),
    ],
)
def test_format_amount_uses_readable_german_notation(
    value: Decimal | None,
    expected: str,
) -> None:
    assert format_amount(value) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1/4", Decimal("0.25")),
        ("¼", Decimal("0.25")),
        ("1 1/2", Decimal("1.5")),
        ("1⁄3", Decimal("0.3333")),
    ],
)
def test_ingredient_schema_accepts_fraction_amounts(raw: str, expected: Decimal) -> None:
    ingredient = IngredientInput(amount_min=raw, unit="l", name="Schlagobers")

    assert ingredient.amount_min == expected
    assert ingredient.unit == "l"


@pytest.mark.parametrize("qualifier", ["etwas", "einige", "Etwas."])
def test_ingredient_schema_preserves_qualitative_amounts(qualifier: str) -> None:
    ingredient = IngredientInput(amount_min=qualifier, name="Staubzucker")

    assert ingredient.amount_min is None
    assert ingredient.unit == qualifier
    assert ingredient.is_scalable is False


def test_ingredient_schema_rejects_unknown_non_numeric_amounts() -> None:
    with pytest.raises(ValidationError, match="gültige Zahl"):
        IngredientInput(amount_min="unbekannt", name="Zutat")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (Decimal("0"), "0"),
        (Decimal("0.5"), "0,5"),
        (Decimal("1451.0000"), "1451"),
        (Decimal("12.3400"), "12,34"),
    ],
)
def test_format_decimal_keeps_nutrition_values_numeric(
    value: Decimal | None, expected: str
) -> None:
    assert format_decimal(value) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (None, "–"),
        (0, "–"),
        (35, "35 Min."),
        (60, "1 Std."),
        (95, "1 Std. 35 Min."),
    ],
)
def test_format_duration_uses_readable_german_notation(minutes: int | None, expected: str) -> None:
    assert format_duration(minutes) == expected


def _category_paths(count: int) -> list[CategoryPathInput]:
    return [
        CategoryPathInput(path=["Freie Taxonomie", f"Kategorie {index}"]) for index in range(count)
    ]


def _suggestions(count: int) -> list[CategorySuggestion]:
    return [
        CategorySuggestion(path=["Vorschlag", f"Kategorie {index}"], confidence=0.9, reason="Passt")
        for index in range(count)
    ]


def test_recipe_schema_accepts_twenty_categories() -> None:
    recipe = RecipeInput(title="Testrezept", base_servings="4", categories=_category_paths(20))

    assert len(recipe.categories) == 20


def test_recipe_schema_rejects_twenty_one_categories() -> None:
    with pytest.raises(ValidationError):
        RecipeInput(title="Testrezept", base_servings="4", categories=_category_paths(21))


def test_ai_schema_accepts_twenty_but_rejects_twenty_one_suggestions() -> None:
    assert (
        len(
            ExtractedRecipe(
                title="Testrezept", category_suggestions=_suggestions(20)
            ).category_suggestions
        )
        == 20
    )

    with pytest.raises(ValidationError):
        ExtractedRecipe(title="Testrezept", category_suggestions=_suggestions(21))


def test_recipe_schema_rejects_duplicate_category_paths_case_insensitively() -> None:
    categories = [
        CategoryPathInput(path=["Küche", "Italienisch"]),
        CategoryPathInput(path=[" küche ", "ITALIENISCH"]),
    ]

    with pytest.raises(ValidationError, match="nur einmal"):
        RecipeInput(title="Testrezept", base_servings="4", categories=categories)


def test_recipe_schema_accepts_both_nutrition_bases_and_german_decimals() -> None:
    recipe = RecipeInput(
        title="Testrezept",
        base_servings="4",
        nutrition=[
            NutritionInput(
                basis="per_serving",
                energy_kcal="652",
                protein_g="16,5",
                note="Eine Portion entspricht einem Viertel.",
            ),
            NutritionInput(basis="per_100g_ml", energy_kj="167", carbohydrates_g="5"),
        ],
    )

    assert recipe.nutrition[0].protein_g == Decimal("16.5")
    assert {value.basis for value in recipe.nutrition} == {"per_serving", "per_100g_ml"}


def test_recipe_schema_rejects_empty_negative_and_duplicate_nutrition() -> None:
    with pytest.raises(ValidationError, match="Mindestens ein Nährwert"):
        NutritionInput(basis="per_serving")
    with pytest.raises(ValidationError):
        NutritionInput(basis="per_serving", fat_g="-1")
    with pytest.raises(ValidationError, match="nur einmal"):
        RecipeInput(
            title="Testrezept",
            base_servings="4",
            nutrition=[
                NutritionInput(basis="per_serving", energy_kcal="100"),
                NutritionInput(basis="per_serving", protein_g="5"),
            ],
        )


def test_recipe_kind_is_inferred_for_legacy_baking_categories_and_can_be_explicit() -> None:
    inferred = RecipeInput(
        title="Zitronenkuchen",
        base_servings="12",
        categories=[{"path": ["Backen", "Kuchen"]}],
    )
    explicit = RecipeInput(
        title="Ofengemüse",
        base_servings="4",
        recipe_kind="cooking",
        categories=[{"path": ["Backen", "Herzhaft"]}],
    )

    assert inferred.recipe_kind == "baking"
    assert explicit.recipe_kind == "cooking"

    with pytest.raises(ValidationError):
        RecipeInput(title="Unklar", base_servings="4", recipe_kind="other")


def _asset(
    *,
    kind: str,
    mime_type: str,
    payload: bytes = b"test-asset",
    generation_metadata: dict[str, str] | None = None,
) -> EncodedAsset:
    return EncodedAsset.model_validate(
        {
            "filename": "asset.bin",
            "mime_type": mime_type,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "data_base64": base64.b64encode(payload).decode("ascii"),
            "kind": kind,
            "generation_metadata": generation_metadata,
        }
    )


def test_recipe_package_accepts_assets_only_in_their_valid_context() -> None:
    generated = _asset(
        kind="generated_image",
        mime_type="image/png",
        generation_metadata={"model": "image-model", "prompt": "Ein Teller Suppe"},
    )
    snapshot = _asset(kind="url_snapshot_pdf", mime_type="application/pdf")

    package = RecipePackageData(
        title="Suppe",
        base_servings="4",
        images=[generated],
        original_assets=[snapshot],
    )

    assert package.images[0].generation_metadata == {
        "model": "image-model",
        "prompt": "Ein Teller Suppe",
    }
    assert package.original_assets[0].kind == "url_snapshot_pdf"


@pytest.mark.parametrize(
    ("images", "original_assets"),
    [
        ([_asset(kind="original_upload", mime_type="image/jpeg")], []),
        ([_asset(kind="recipe_image", mime_type="application/pdf")], []),
        ([], [_asset(kind="recipe_image", mime_type="image/jpeg")]),
        ([], [_asset(kind="generated_image", mime_type="image/png")]),
    ],
)
def test_recipe_package_rejects_assets_in_the_wrong_context(
    images: list[EncodedAsset],
    original_assets: list[EncodedAsset],
) -> None:
    with pytest.raises(ValidationError):
        RecipePackageData(
            title="Suppe",
            base_servings="4",
            images=images,
            original_assets=original_assets,
        )


def test_encoded_asset_decodes_strict_base64_and_enforces_the_byte_limit() -> None:
    asset = _asset(kind="recipe_image", mime_type="image/png", payload=b"1234")
    assert asset.decoded(max_bytes=4) == b"1234"

    with pytest.raises(ValueError, match="zu groß"):
        asset.decoded(max_bytes=3)

    invalid = asset.model_copy(update={"data_base64": "not base64!"})
    with pytest.raises(ValueError, match="Base64"):
        invalid.decoded(max_bytes=100)


def _mock_dns(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    def getaddrinfo(
        host: str,
        port: int,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[object, ...]]]:
        del host
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, type, socket.IPPROTO_TCP, "", (address, port)) for address in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104 -- deliberate SSRF rejection fixture
        "224.0.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
    ],
)
def test_url_validation_rejects_non_public_dns_targets(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _mock_dns(monkeypatch, address)

    with pytest.raises(UnsafeURL, match="Interne oder lokale"):
        validate_public_url("https://example.test/rezept")


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_url_validation_accepts_only_fully_public_resolution_results(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _mock_dns(monkeypatch, address)
    url = "https://example.test/rezept?portionen=4"

    assert validate_public_url(url) == url


def test_url_validation_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, "93.184.216.34", "127.0.0.1")

    with pytest.raises(UnsafeURL, match="Interne oder lokale"):
        validate_public_url("https://example.test/rezept")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/rezept",
        "https://user@example.test/rezept",
        "https://user:secret@example.test/rezept",
        "https://example.test:8443/rezept",
        "https:///ohne-host",
    ],
)
def test_url_validation_rejects_unsafe_url_shapes_before_dns(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    def unexpected_dns(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("Unsichere URL-Formen dürfen keine DNS-Auflösung auslösen")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)

    with pytest.raises(UnsafeURL):
        validate_public_url(url)


def test_url_validation_rejects_dns_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_dns(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", failed_dns)

    with pytest.raises(UnsafeURL, match="nicht aufgelöst"):
        validate_public_url("https://does-not-exist.invalid/rezept")


def test_url_shape_validation_does_not_resolve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_dns(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("Die proxyseitige Formprüfung darf DNS nicht selbst auflösen")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)

    url = "https://www.lecker.de/rezept?portionen=4"
    assert validate_http_url_shape(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.test/rezept",
        "http://example.test:8080/rezept",
        "https://example.test/rezept\n",
    ],
)
def test_url_shape_validation_retains_non_dns_safety_rules(url: str) -> None:
    with pytest.raises(UnsafeURL):
        validate_http_url_shape(url)


def test_url_validation_uses_http_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, int]] = []

    def getaddrinfo(
        host: str,
        port: int,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[object, ...]]]:
        observed.append((host, port))
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    assert validate_public_url("http://example.test/rezept") == "http://example.test/rezept"
    assert observed == [("example.test", 80)]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test:kein-port/rezept",
        " https://example.test/rezept",
        "https://example.test/rezept\n",
        "https://example.test/rezept\x00",
    ],
)
def test_url_validation_rejects_invalid_ports_and_control_characters_before_dns(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    def unexpected_dns(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("Ungültige URLs dürfen keine DNS-Auflösung auslösen")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)

    with pytest.raises(UnsafeURL):
        validate_public_url(url)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "/etc/passwd",
        "../etc/passwd",
        "media/../../etc/passwd",
        "media\\..\\etc\\passwd",
        "media/image.jpg\x00.zip",
    ],
)
def test_safe_archive_name_rejects_empty_absolute_and_traversal_paths(name: str) -> None:
    with pytest.raises(InvalidBackup):
        safe_archive_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "media/ab/cd/image-01.webp",
        "media/Résumé (1).jpg",
    ],
)
def test_safe_archive_name_preserves_safe_relative_posix_paths(name: str) -> None:
    assert safe_archive_name(name) == name


def _service_worker_source() -> str:
    return (PROJECT_ROOT / "app" / "static" / "dist" / "service-worker.js").read_text(
        encoding="utf-8"
    )


def _asset_manifest() -> dict[str, object]:
    return json.loads(
        (PROJECT_ROOT / "app" / "static" / "dist" / "asset-manifest.json").read_text(
            encoding="utf-8"
        )
    )


def _service_worker_precache(source: str) -> list[str]:
    match = re.search(r"const STATIC_ASSETS\s*=\s*\[(.*?)\];", source, flags=re.DOTALL)
    assert match is not None, "Service Worker muss eine explizite Precache-Allowlist besitzen"
    return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', match.group(1))


def test_service_worker_precache_is_a_small_public_allowlist() -> None:
    source = _service_worker_source()
    assets = _service_worker_precache(source)
    manifest = _asset_manifest()

    assert assets
    assert len(assets) == len(set(assets))
    assert assets == manifest["precache"]
    assert manifest["offline_url"] in assets
    assert manifest["assets"]["pwa/og.png"] not in assets
    assert all(
        asset.startswith("/static/assets/") or asset == manifest["offline_url"] for asset in assets
    )
    assert not any(
        asset.startswith(("/api/", "/assets/", "/login", "/einstellungen"))
        or asset.casefold().endswith((".pdf", ".zip"))
        for asset in assets
    )

    for asset in assets:
        if asset.startswith("/static/assets/"):
            relative_path = asset.removeprefix("/static/")
            assert (PROJECT_ROOT / "app" / "static" / "dist" / relative_path).is_file()


def test_frontend_manifest_fingerprints_every_source_asset() -> None:
    manifest = _asset_manifest()
    mappings = manifest["assets"]
    assert isinstance(mappings, dict)

    expected = {
        f"{directory.name}/{path.name}"
        for directory in (
            PROJECT_ROOT / "app" / "static" / "css",
            PROJECT_ROOT / "app" / "static" / "js",
            PROJECT_ROOT / "app" / "static" / "pwa",
        )
        for path in directory.iterdir()
        if path.is_file()
    }
    assert set(mappings) == expected
    template_references = {
        logical_name
        for template in (PROJECT_ROOT / "app" / "templates").rglob("*.html")
        for logical_name in re.findall(
            r"asset\(['\"]([^'\"]+)['\"]\)", template.read_text(encoding="utf-8")
        )
    }
    assert template_references <= set(mappings)
    assert re.fullmatch(r"[a-f0-9]{16}", str(manifest["build_id"]))
    assert re.fullmatch(r"[a-f0-9]{64}", str(manifest["source_digest"]))
    assert manifest["manifest_url"] == f"/manifest.webmanifest?v={manifest['build_id']}"

    for url in mappings.values():
        assert isinstance(url, str)
        assert re.fullmatch(
            r"/static/assets/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
            r"-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+",
            url,
        )
        assert "?" not in url
        assert (PROJECT_ROOT / "app" / "static" / "dist" / url.removeprefix("/static/")).is_file()


def test_frontend_sources_and_templates_have_no_manual_cache_versions() -> None:
    source_files = [
        *sorted((PROJECT_ROOT / "app" / "static" / "js").glob("*.js")),
        *sorted((PROJECT_ROOT / "app" / "templates").rglob("*.html")),
    ]

    for path in source_files:
        source = path.read_text(encoding="utf-8")
        assert "?v=" not in source
        assert "/static/js/" not in source
        assert "/static/css/" not in source


def test_page_heading_descriptions_are_absent_in_every_locale() -> None:
    source_files = [
        *sorted((PROJECT_ROOT / "app" / "templates").rglob("*.html")),
        PROJECT_ROOT / "app" / "static" / "css" / "app.css",
        PROJECT_ROOT / "app" / "static" / "js" / "recipe-search.js",
    ]
    assert all(
        "page-heading__lede" not in path.read_text(encoding="utf-8") for path in source_files
    )

    removed_keys = {
        "account.lede",
        "categories.lede",
        "favorites.lede",
        "form.lede",
        "history.lede",
        "import.completed_lede",
        "import.history_lede",
        "import.lede",
        "import.preparing_lede",
        "import.review_lede",
        "import.running_lede",
        "notes.lede",
        "recipes.lede.baking",
        "recipes.lede.cooking",
        "recipes.trash_lede",
        "settings.lede",
        "share.lede",
        "tags.lede",
    }
    for catalog in MESSAGES.values():
        assert removed_keys.isdisjoint(catalog)


def test_frontend_manifest_rejects_stale_source_files(tmp_path: Path) -> None:
    static_directory = tmp_path / "app" / "static"
    templates_directory = tmp_path / "app" / "templates"
    shutil.copytree(PROJECT_ROOT / "app" / "static", static_directory)
    templates_directory.mkdir(parents=True)
    shutil.copy2(
        PROJECT_ROOT / "app" / "templates" / "offline.html",
        templates_directory / "offline.html",
    )

    FrontendAssets.load(static_directory / "dist")
    offline_template = templates_directory / "offline.html"
    original_template = offline_template.read_text(encoding="utf-8")
    offline_template.write_text(
        f"{original_template}\n{{{{ asset('js/missing.js') }}}}\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="unknown frontend asset"):
        FrontendAssets.load(static_directory / "dist")
    offline_template.write_text(original_template, encoding="utf-8")

    with (static_directory / "js" / "app.js").open("a", encoding="utf-8") as source:
        source.write("\n// stale build\n")

    with pytest.raises(RuntimeError, match="Frontend build is stale"):
        FrontendAssets.load(static_directory / "dist")


def test_service_worker_never_persists_runtime_or_sensitive_responses() -> None:
    source = _service_worker_source()

    assert 'request.method !== "GET"' in source
    assert "url.origin !== self.location.origin" in source
    for protected_path in ("/api/", "/assets/", "/login", "/einstellungen"):
        assert json.dumps(protected_path) in source

    assert "cache.put(" not in source
    assert "cache.add(" not in source
    assert source.count("caches.open(") == 1
    assert 'fetch(request, { cache: "no-store" })' in source
    assert "caches.match(OFFLINE_URL)" in source


def test_pwa_has_no_custom_install_controls() -> None:
    template = (PROJECT_ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    app_script = (PROJECT_ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "data-pwa-install" not in template
    assert "App installieren" not in template
    assert "beforeinstallprompt" not in app_script
    assert "pwa.js" not in app_script
    assert not (PROJECT_ROOT / "app" / "static" / "js" / "pwa.js").exists()


def test_account_menu_shows_name_in_panel_not_next_to_avatar() -> None:
    template = (PROJECT_ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

    assert "account-menu__name" not in template
    assert '<p class="account-menu__identity">{{ current_user.visible_name }}</p>' in template
