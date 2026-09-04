from __future__ import annotations

import base64
import json
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import dramatiq
import httpx
import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.ai import extraction_client, image_client
from app.ai.extraction_client import AIExtractionError, AIUnavailable
from app.ai.extraction_repair import MISSING_SERVINGS_WARNING, RANGE_SERVINGS_WARNING
from app.ai.image_client import AIImageError
from app.config import Settings
from app.imports import pipeline
from app.models import BackupRestoreJob, ImportBatch, ImportCandidate, ImportJob, User
from app.schemas.ai import (
    DetectedRecipe,
    DetectedRecipeDocument,
    ExtractedRecipe,
    ExtractedRecipeDraft,
    NormalizedBoundingBox,
    RecipeImageCandidate,
    RecipeImageMatch,
    RecipeSourceRegion,
)
from app.schemas.recipe import RecipeInput
from app.workers import tasks


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "ai_api_key": "test-api-key",
        "ai_base_url": "https://api.openai.com/v1",
        "ai_extraction_reasoning_effort": "high",
        "ai_image_model": "gpt-image-1",
        "ai_image_quality": "high",
        "ai_max_retries": 0,
        "ai_timeout_seconds": 10,
        "ai_image_generation_enabled": True,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _extracted(**overrides: object) -> ExtractedRecipe:
    values: dict[str, object] = {
        "title": "Kartoffelsuppe",
        "base_servings": "4",
        "description": "Cremig und warm",
        "nutrition": [
            {
                "basis": "per_serving",
                "energy_kcal": "347",
                "protein_g": "10",
                "note": "Eine Portion entspricht einem Viertel.",
            }
        ],
        "category_suggestions": [
            {"path": ["Suppen"], "confidence": 0.9, "reason": "Ist eine Suppe"}
        ],
        "warnings": ["Menge bei Salz unklar"],
        "extraction_confidence": "high",
    }
    values.update(overrides)
    return ExtractedRecipe.model_validate(values)


def _detected_document(*recipes: DetectedRecipe) -> DetectedRecipeDocument:
    detected = recipes or (
        DetectedRecipe(
            title_hint="Kartoffelsuppe",
            source_regions=[RecipeSourceRegion(page=1)],
            detection_confidence="high",
        ),
    )
    return DetectedRecipeDocument(recipes=list(detected), warnings=[])


class _Response:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes = b"",
        error: Exception | None = None,
    ) -> None:
        self.payload = payload or {}
        self.content = content
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> dict[str, Any]:
        return self.payload


class _Client:
    def __init__(self, outcomes: list[_Response | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    def factory(*args: object, **kwargs: object) -> _Client:
        del args, kwargs
        return client

    monkeypatch.setattr(httpx, "Client", factory)


def test_extract_output_text_accepts_both_response_shapes_and_rejects_empty() -> None:
    assert extraction_client._extract_output_text({"output_text": "direkt"}) == "direkt"
    assert (
        extraction_client._extract_output_text(
            {"output": [{"content": [{"type": "output_text", "text": "verschachtelt"}]}]}
        )
        == "verschachtelt"
    )
    assert (
        extraction_client._extract_output_text(
            {"output": [{"content": [{"type": "text", "text": "alternativ"}]}]}
        )
        == "alternativ"
    )
    with pytest.raises(AIExtractionError, match="abgelehnt"):
        extraction_client._extract_output_text({"output": [{"content": [{"type": "refusal"}]}]})


def test_extract_output_text_reports_refusal_and_incomplete_response() -> None:
    with pytest.raises(AIExtractionError, match="abgelehnt"):
        extraction_client._extract_output_text(
            {"output": [{"content": [{"type": "refusal", "refusal": "nein"}]}]}
        )
    with pytest.raises(AIExtractionError, match="max_output_tokens"):
        extraction_client._extract_output_text(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        )


def test_strict_response_schema_requires_and_closes_every_object() -> None:
    schema = extraction_client.strict_response_schema()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        assert "default" not in value
        assert "format" not in value
        assert "pattern" not in value
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert value["additionalProperties"] is False
            assert value["required"] == list(properties)
        for child in value.values():
            visit(child)

    assert schema["type"] == "object"
    assert "title" in schema["required"]
    assert "description" in schema["required"]
    assert "recipe_kind" in schema["required"]
    assert schema["properties"]["recipe_kind"]["enum"] == ["cooking", "baking"]
    assert "base_servings" not in schema["properties"]
    visit(schema)

    servings_min = schema["properties"]["servings_min"]
    assert servings_min["anyOf"] == [
        {"exclusiveMinimum": 0, "maximum": 100_000, "type": "number"},
        {"type": "null"},
    ]

    source_url = schema["properties"]["source_url"]
    assert source_url == {
        "anyOf": [
            {"maxLength": 2083, "minLength": 1, "type": "string"},
            {"type": "null"},
        ],
        "title": "Source Url",
    }


def test_extracted_recipe_still_validates_source_url_after_schema_normalization() -> None:
    extraction_client.strict_response_schema()

    extracted = ExtractedRecipe.model_validate(
        {"title": "Toast", "source_url": "https://example.test/rezept"}
    )
    assert str(extracted.source_url) == "https://example.test/rezept"
    with pytest.raises(ValueError, match="URL"):
        ExtractedRecipe.model_validate({"title": "Toast", "source_url": "keine-url"})


def test_repair_categories_normalizes_deduplicates_sorts_and_limits() -> None:
    suggestions: list[object] = [
        {"path": ["  Deutsche   Küche ", " Suppen "], "confidence": 0.5, "reason": "a"},
        {"path": ["deutsche küche", "SUPPEN"], "confidence": 0.9, "reason": "duplicate"},
        {"path": "nicht-eine-liste", "confidence": 1.0},
        "kein Objekt",
        {"path": ["   "], "confidence": 0.8},
    ]
    suggestions.extend(
        {"path": [f"Kategorie {index}"], "confidence": index / 100, "reason": "x"}
        for index in range(30)
    )

    result = extraction_client._repair_categories({"category_suggestions": suggestions})

    repaired = cast(list[dict[str, Any]], result["category_suggestions"])
    assert len(repaired) == 20
    assert repaired[0]["path"] == ["deutsche küche", "SUPPEN"]
    assert sum(item["path"][-1].casefold() == "suppEN".casefold() for item in repaired) == 1
    assert extraction_client._repair_categories({})["category_suggestions"] == []


def test_extract_recipe_uses_whole_recipe_basis_when_servings_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "title": "Sacher-Torte",
        "servings_min": None,
        "servings_max": None,
        "serving_label": None,
        "serving_text": None,
        "extraction_confidence": "high",
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"rezept",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    assert (result.base_servings, result.serving_label) == (Decimal("1"), "Rezept")
    assert MISSING_SERVINGS_WARNING in result.warnings
    assert result.extraction_confidence == "medium"
    assert len(client.requests) == 1


def test_extract_recipe_preserves_serving_range_without_inventing_a_midpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "title": "Schokoladen-Schnitten",
        "servings_min": 12,
        "servings_max": 16,
        "serving_label": "Stück",
        "serving_text": "12–16 Stück",
        "notes": "Kühl lagern.",
        "extraction_confidence": "high",
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"rezept",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    assert (result.base_servings, result.serving_label) == (Decimal("1"), "Rezept")
    assert "Originalausbeute laut Quelle: 12–16 Stück." in (result.notes or "")
    assert RANGE_SERVINGS_WARNING in result.warnings
    assert result.extraction_confidence == "medium"
    assert len(client.requests) == 1


def test_extract_recipe_repairs_legacy_textual_serving_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _Response(
                {"output_text": json.dumps({"title": "Schnitten", "base_servings": "12-16 Stück"})}
            )
        ]
    )
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"rezept",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    assert (result.base_servings, result.serving_label) == (Decimal("1"), "Rezept")
    assert "Originalausbeute laut Quelle: 12-16 Stück." in (result.notes or "")
    assert RANGE_SERVINGS_WARNING in result.warnings


def test_extract_recipe_repairs_optional_fields_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "title": "Robustes Rezept",
        "servings_min": 4,
        "servings_max": None,
        "serving_label": "Personen",
        "source_url": "keine-url",
        "prep_time_minutes": "irgendwann",
        "nutrition": [
            {"basis": "pro Portion", "energy_kcal": "250", "fat_g": "unbekannt"},
            {"basis": "falsch", "energy_kcal": 100},
        ],
        "ingredient_groups": [
            {
                "ingredients": [
                    {"name": "Salz", "amount_min": "nach Geschmack"},
                    {"name": " ", "amount_min": 2},
                ]
            }
        ],
        "instruction_steps": ["Alles verrühren.", {"text": " "}],
        "category_suggestions": [{"path": [" Alltag "], "confidence": "unbekannt", "reason": None}],
        "extraction_confidence": "high",
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"rezept",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    assert result.base_servings == 4
    assert result.source_url is None
    assert result.prep_time_minutes is None
    assert result.nutrition[0].energy_kcal == 250
    assert result.nutrition[0].fat_g is None
    ingredient = result.ingredient_groups[0].ingredients[0]
    assert ingredient.name == "Salz"
    assert ingredient.amount_min is None and ingredient.is_scalable is False
    assert "Mengenangabe laut Quelle: nach Geschmack." in (ingredient.note or "")
    assert [step.text for step in result.instruction_steps] == ["Alles verrühren."]
    assert result.category_suggestions[0].path == ["Alltag"]
    assert result.extraction_confidence == "medium"
    assert len(result.warnings) >= 5


def test_extract_recipe_can_use_detected_title_when_only_extracted_title_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "title": "",
        "ingredient_groups": [{"ingredients": [{"name": "Mehl", "amount_min": 100}]}],
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"rezept",
        mime_type="image/png",
        existing_category_paths=[],
        title_hint="Erkannter Kuchen",
        settings=_settings(),
    )

    assert result.title == "Erkannter Kuchen"
    assert any("Dokumenttitel" in warning for warning in result.warnings)


def test_draft_schema_rejects_text_in_numeric_serving_fields() -> None:
    with pytest.raises(ValueError, match="servings_min"):
        ExtractedRecipeDraft.model_validate(
            {"title": "Schnitten", "servings_min": "12–16", "servings_max": None}
        )


def test_extract_recipe_rejects_missing_api_key_before_network() -> None:
    with pytest.raises(AIUnavailable, match="AI_API_KEY"):
        extraction_client.extract_recipe(
            content=b"bild",
            mime_type="image/png",
            existing_category_paths=[],
            settings=_settings(ai_api_key=""),
        )


@pytest.mark.parametrize(
    ("mime_type", "expected_type"),
    [("image/jpeg", "input_image"), ("application/pdf", "input_file")],
)
def test_extract_recipe_sends_typed_data_and_validates_response(
    monkeypatch: pytest.MonkeyPatch,
    mime_type: str,
    expected_type: str,
) -> None:
    raw = {
        "title": "  Linseneintopf  ",
        "ingredient_groups": [
            {
                "title": "Dekoration",
                "ingredients": [
                    {"amount_min": "1/4", "unit": "l", "name": "Schlagobers"},
                    {"amount_min": "etwas", "unit": None, "name": "Staubzucker"},
                    {"amount_min": "einige", "unit": None, "name": "Melisseblätter"},
                ],
            }
        ],
        "nutrition": [
            {
                "basis": "per_serving",
                "energy_kcal": "347",
                "protein_g": "10",
            }
        ],
        "category_suggestions": [
            {"path": [" Eintöpfe "], "confidence": 0.7, "reason": "Passt"},
            {"path": ["eintöpfe"], "confidence": 0.6, "reason": "Duplikat"},
        ],
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"quellmaterial",
        mime_type=mime_type,
        existing_category_paths=["Alltag › Eintöpfe"],
        settings=_settings(),
    )

    assert result.title == "  Linseneintopf  "
    assert [item.path for item in result.category_suggestions] == [["Eintöpfe"]]
    assert result.nutrition[0].energy_kcal == 347
    ingredients = result.ingredient_groups[0].ingredients
    assert ingredients[0].amount_min == Decimal("0.25")
    assert ingredients[1].unit == "etwas"
    assert ingredients[2].unit == "einige"
    request = client.requests[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert request["json"]["store"] is False
    assert request["json"]["reasoning"] == {"effort": "high"}
    assert request["headers"]["Authorization"] == "Bearer test-api-key"
    content = request["json"]["input"][0]["content"]
    assert content[1]["type"] == expected_type
    encoded = content[1]["file_data" if expected_type == "input_file" else "image_url"]
    assert base64.b64decode(encoded.split(",", 1)[1]) == b"quellmaterial"
    assert "Alltag › Eintöpfe" in content[0]["text"]


def test_detect_recipes_requests_all_recipe_regions_and_image_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "recipes": [
            {
                "title_hint": "Suppe",
                "source_regions": [
                    {
                        "page": 1,
                        "bounding_box": {"left": 0, "top": 0, "right": 500, "bottom": 1000},
                    }
                ],
                "recipe_image_candidates": [
                    {
                        "page": 1,
                        "bounding_box": {
                            "left": 50,
                            "top": 50,
                            "right": 450,
                            "bottom": 350,
                        },
                        "description": "Schüssel Suppe",
                        "confidence": 0.92,
                    }
                ],
                "warnings": [],
                "detection_confidence": "high",
            },
            {
                "title_hint": "Kuchen",
                "source_regions": [
                    {
                        "page": 1,
                        "bounding_box": {"left": 500, "top": 0, "right": 1000, "bottom": 1000},
                    }
                ],
                "recipe_image_candidates": [],
                "warnings": [],
                "detection_confidence": "medium",
            },
        ],
        "warnings": [],
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.detect_recipes(
        content=b"seite",
        mime_type="image/png",
        settings=_settings(),
    )

    assert [recipe.title_hint for recipe in result.recipes] == ["Suppe", "Kuchen"]
    request = client.requests[0]["json"]
    assert request["text"]["format"]["name"] == "detected_recipe_document"
    assert request["input"][0]["content"][1]["detail"] == "high"
    schema = request["text"]["format"]["schema"]
    assert "recipes" in schema["required"]


def test_detect_recipes_retries_unreliable_result_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _detected_document().model_dump(mode="json")
    client = _Client(
        [
            _Response({"output_text": '{"recipes":[]}'}),
            _Response({"output_text": json.dumps(valid)}),
        ]
    )
    _install_client(monkeypatch, client)

    result = extraction_client.detect_recipes(
        content=b"seite",
        mime_type="image/png",
        settings=_settings(),
    )

    assert result.recipes[0].title_hint == "Kartoffelsuppe"
    assert len(client.requests) == 2


def test_extract_recipe_accepts_multiple_isolated_region_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([_Response({"output_text": '{"title":"Mehrseitig"}'})])
    _install_client(monkeypatch, client)

    extraction_client.extract_recipe(
        images=[b"erste region", b"zweite region"],
        existing_category_paths=[],
        settings=_settings(),
    )

    content = client.requests[0]["json"]["input"][0]["content"]
    image_inputs = [item for item in content if item["type"] == "input_image"]
    assert len(image_inputs) == 2
    assert all(item["detail"] == "high" for item in image_inputs)


def test_extraction_prompt_translates_foreign_recipes_into_german() -> None:
    extraction_prompt = extraction_client.extraction_system_prompt("de")
    detection_prompt = extraction_client.detection_system_prompt("de")

    assert "in jeder Sprache" in extraction_prompt
    assert "German" in extraction_prompt
    assert "Fahrenheit" in extraction_prompt
    assert "Cups" in extraction_prompt
    assert "servings_min" in extraction_prompt
    assert "erfinde niemals eine Portionszahl" in extraction_prompt
    assert "in jeder Sprache" in detection_prompt
    assert "German" in detection_prompt


def test_extract_recipe_normalizes_foreign_units_and_fahrenheit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "title": "Pfannkuchen",
        "description": "Ofen auf 350°F vorheizen.",
        "base_servings": 4,
        "serving_label": "Portionen",
        "ingredient_groups": [
            {
                "title": "Teig",
                "ingredients": [
                    {"amount_min": 2, "unit": "cups", "name": "Mehl"},
                    {"amount_min": "1.5", "unit": "tbsp.", "name": "Zucker"},
                    {"amount_min": 8, "unit": "oz", "name": "Frischkäse"},
                    {"amount_min": 3, "unit": "lb", "name": "Äpfel"},
                ],
            }
        ],
        "instruction_steps": [{"text": "Bei 350–375 degrees Fahrenheit backen."}],
        "source_title": "American Pancakes",
        "warnings": [],
    }
    client = _Client([_Response({"output_text": json.dumps(raw)})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"english recipe",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    ingredients = result.ingredient_groups[0].ingredients
    assert (ingredients[0].amount_min, ingredients[0].unit) == (Decimal("473"), "ml")
    assert (ingredients[1].amount_min, ingredients[1].unit) == (Decimal("1.5"), "EL")
    assert (ingredients[2].amount_min, ingredients[2].unit) == (Decimal("227"), "g")
    assert (ingredients[3].amount_min, ingredients[3].unit) == (Decimal("1.36"), "kg")
    assert result.description == "Ofen auf 175 °C vorheizen."
    assert result.instruction_steps[0].text == "Bei 175–190 °C backen."
    assert result.source_title == "American Pancakes"
    assert any("Nichtmetrische Einheiten" in warning for warning in result.warnings)
    assert any("Fahrenheit-Angaben" in warning for warning in result.warnings)


def test_verify_recipe_image_uses_recipe_identity_and_strict_match_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _Response(
                {
                    "output_text": json.dumps(
                        {
                            "matches_recipe": True,
                            "confidence": 0.94,
                            "reason": "Sichtbare Kartoffelsuppe",
                        }
                    )
                }
            )
        ]
    )
    _install_client(monkeypatch, client)
    extracted = _extracted(
        ingredient_groups=[{"ingredients": [{"name": "Kartoffeln"}, {"name": "Lauch"}]}]
    )

    result = extraction_client.verify_recipe_image(
        extracted=extracted,
        image=b"ausschnitt",
        settings=_settings(),
    )

    assert result.matches_recipe is True and result.confidence == 0.94
    request = client.requests[0]["json"]
    assert request["text"]["format"]["name"] == "recipe_image_match"
    prompt = request["input"][0]["content"][0]["text"]
    assert "Kartoffelsuppe" in prompt and "Kartoffeln" in prompt and "Lauch" in prompt


def test_extract_recipe_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    client = _Client(
        [
            httpx.ConnectError("kurz offline", request=request),
            _Response({"output": [{"content": [{"type": "text", "text": '{"title":"Toast"}'}]}]}),
        ]
    )
    _install_client(monkeypatch, client)
    sleeps: list[int] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    result = extraction_client.extract_recipe(
        content=b"x",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(ai_max_retries=1),
    )

    assert result.title == "Toast"
    assert sleeps == [1]
    assert len(client.requests) == 2


def test_extract_recipe_retries_unreliable_result_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _Response({"output_text": '{"title":""}'}),
            _Response({"output_text": '{"title":"Gerettetes Rezept"}'}),
        ]
    )
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"x",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(ai_max_retries=3),
    )

    assert result.title == "Gerettetes Rezept"
    assert len(client.requests) == 2


def test_extract_recipe_does_not_retry_non_transient_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(400, request=request)
    client = _Client(
        [_Response(error=httpx.HTTPStatusError("bad", request=request, response=response))]
    )
    _install_client(monkeypatch, client)

    with pytest.raises(AIExtractionError, match="abgelehnt"):
        extraction_client.extract_recipe(
            content=b"x",
            mime_type="image/png",
            existing_category_paths=[],
            settings=_settings(ai_max_retries=3),
        )

    assert len(client.requests) == 1


def test_extract_recipe_includes_bounded_provider_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "Invalid schema\nfor response format"}},
    )
    client = _Client(
        [_Response(error=httpx.HTTPStatusError("bad", request=request, response=response))]
    )
    _install_client(monkeypatch, client)

    with pytest.raises(
        AIExtractionError,
        match="Extraktionsanfrage abgelehnt: Invalid schema for response format",
    ):
        extraction_client.extract_recipe(
            content=b"x",
            mime_type="image/png",
            existing_category_paths=[],
            settings=_settings(),
        )


@pytest.mark.parametrize(
    "response_text",
    ["kein json", '{"title":""}'],
)
def test_extract_recipe_translates_invalid_ai_output_to_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    response_text: str,
) -> None:
    client = _Client(
        [
            _Response({"output_text": response_text}),
            _Response({"output_text": response_text}),
        ]
    )
    _install_client(monkeypatch, client)

    with pytest.raises(AIExtractionError, match="nicht zuverlässig"):
        extraction_client.extract_recipe(
            content=b"x",
            mime_type="image/png",
            existing_category_paths=[],
            settings=_settings(),
        )

    assert len(client.requests) == 2


def test_extract_recipe_treats_nonpositive_legacy_servings_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([_Response({"output_text": '{"title":"X","base_servings":0}'})])
    _install_client(monkeypatch, client)

    result = extraction_client.extract_recipe(
        content=b"x",
        mime_type="image/png",
        existing_category_paths=[],
        settings=_settings(),
    )

    assert (result.base_servings, result.serving_label) == (Decimal("1"), "Rezept")
    assert MISSING_SERVINGS_WARNING in result.warnings
    assert len(client.requests) == 1


def test_image_reference_keeps_images_and_selects_best_pdf_page() -> None:
    assert image_client._image_reference(_extracted(), b"jpeg", "image/jpeg") == (
        b"jpeg",
        "image/jpeg",
    )
    output = BytesIO()
    with pdfium.PdfDocument.new() as document:
        for width, height in ((40, 40), (100, 50)):
            pdf_page = document.new_page(width=width, height=height)
            pdf_page.close()
        document.save(output)
    pdf = output.getvalue()
    extracted = _extracted(
        recipe_image_candidates=[
            RecipeImageCandidate(page=1, description="erste Seite", confidence=0.2),
            RecipeImageCandidate(page=99, description="beste, begrenzt", confidence=0.9),
        ]
    )

    image, mime = image_client._image_reference(extracted, pdf, "application/pdf")

    assert mime == "image/png"
    assert image.startswith(b"\x89PNG")
    with Image.open(BytesIO(image)) as rendered:
        assert rendered.size == (250, 125)


def test_source_regions_crop_the_exact_normalized_image_area() -> None:
    source = Image.new("RGB", (200, 100), "red")
    source.paste("blue", (100, 0, 200, 100))
    output = BytesIO()
    source.save(output, format="PNG")
    normalized = pipeline.normalize_image_source(output.getvalue())

    left = pipeline.crop_source_region(
        normalized,
        "image/png",
        RecipeSourceRegion(
            page=1,
            bounding_box=NormalizedBoundingBox(left=0, top=0, right=500, bottom=1000),
        ),
    )
    right = pipeline.crop_source_region(
        normalized,
        "image/png",
        RecipeSourceRegion(
            page=1,
            bounding_box=NormalizedBoundingBox(left=500, top=0, right=1000, bottom=1000),
        ),
    )

    with Image.open(BytesIO(left)) as left_image, Image.open(BytesIO(right)) as right_image:
        assert left_image.size == (100, 100)
        assert right_image.size == (100, 100)
        assert left_image.getpixel((50, 50)) == (255, 0, 0)
        assert right_image.getpixel((50, 50)) == (0, 0, 255)


def test_global_image_assignment_prefers_semantic_match_and_never_reuses_crop() -> None:
    shared = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=20, top=20, right=480, bottom=420),
        description="ein Bild",
        confidence=0.9,
    )
    other = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=520, top=20, right=980, bottom=420),
        description="anderes Bild",
        confidence=0.8,
    )
    assignments = pipeline._assign_verified_images(
        [
            pipeline.VerifiedImageOption(0, shared, b"shared", 0.99, "sehr sicher"),
            pipeline.VerifiedImageOption(1, shared, b"shared", 0.97, "eindeutig"),
            pipeline.VerifiedImageOption(0, other, b"other", 0.98, "ebenfalls eindeutig"),
        ]
    )

    assert assignments[1].image == b"shared"
    assert assignments[0].image == b"other"
    assert len(assignments) == 2


def test_sanitize_keeps_cross_recipe_options_for_semantic_assignment_but_caps_noise() -> None:
    box = NormalizedBoundingBox(left=0, top=0, right=500, bottom=500)
    noisy_candidates = [
        RecipeImageCandidate(
            page=1,
            bounding_box=NormalizedBoundingBox(
                left=index * 100,
                top=500,
                right=index * 100 + 80,
                bottom=900,
            ),
            description=f"Bild {index}",
            confidence=0.95 - index * 0.01,
        )
        for index in range(5)
    ]
    shared = RecipeImageCandidate(
        page=1,
        bounding_box=box,
        description="geteilt",
        confidence=0.99,
    )
    recipes = [
        DetectedRecipe(
            title_hint="A",
            source_regions=[RecipeSourceRegion(page=1)],
            recipe_image_candidates=[shared, *noisy_candidates],
        ),
        DetectedRecipe(
            title_hint="B",
            source_regions=[RecipeSourceRegion(page=1)],
            recipe_image_candidates=[shared],
        ),
    ]

    sanitized = pipeline._sanitize_detections(recipes, page_count=1)

    assert shared in sanitized[0].recipe_image_candidates
    assert shared in sanitized[1].recipe_image_candidates
    assert len(sanitized[0].recipe_image_candidates) == 3


def test_image_reference_rejects_empty_and_invalid_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(image_client, "source_page_count", lambda *_args: 0)
        with pytest.raises(AIImageError, match="keine Seite"):
            image_client._image_reference(_extracted(), b"pdf", "application/pdf")
    with pytest.raises(AIImageError, match="nicht vorbereitet"):
        image_client._image_reference(_extracted(), b"kein pdf", "application/pdf")


def test_generate_image_short_circuits_disabled_cases() -> None:
    assert (
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=False), b"x", "image/png", settings=_settings()
        )
        is None
    )
    assert (
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=True),
            b"x",
            "image/png",
            settings=_settings(ai_image_generation_enabled=False),
        )
        is None
    )
    with pytest.raises(AIImageError, match="nicht konfiguriert"):
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=True),
            b"x",
            "image/png",
            settings=_settings(ai_api_key=""),
        )


def test_generate_image_sends_reference_and_decodes_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = b"fertiges png"

    def post(url: str, **kwargs: Any) -> _Response:
        captured.update({"url": url, **kwargs})
        return _Response({"data": [{"b64_json": base64.b64encode(expected).decode("ascii")}]})

    monkeypatch.setattr(httpx, "post", post)

    result = image_client.maybe_generate_recipe_image(
        _extracted(has_recipe_image=True),
        b"referenz",
        "image/jpeg",
        settings=_settings(ai_base_url="https://images.example/v1"),
    )

    assert result == expected
    assert captured["url"] == "https://images.example/v1/images/edits"
    assert captured["headers"]["Authorization"] == "Bearer test-api-key"
    assert captured["data"]["model"] == "gpt-image-1"
    assert captured["data"]["quality"] == "high"
    assert captured["files"]["image"] == ("reference", b"referenz", "image/jpeg")


def test_generate_image_retries_transient_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/images/edits")
    outcomes: list[_Response | Exception] = [
        httpx.ConnectError("offline", request=request),
        _Response({"data": [{"b64_json": base64.b64encode(b"bild").decode()}]}),
    ]
    calls = 0

    def post(*args: object, **kwargs: object) -> _Response:
        nonlocal calls
        del args, kwargs
        calls += 1
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps: list[int] = []
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert (
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=True),
            b"x",
            "image/png",
            settings=_settings(ai_max_retries=1),
        )
        == b"bild"
    )
    assert calls == 2
    assert sleeps == [1]


def test_generate_image_does_not_retry_non_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/images/edits")
    response = httpx.Response(400, request=request)
    calls = 0

    def post(*args: object, **kwargs: object) -> _Response:
        nonlocal calls
        del args, kwargs
        calls += 1
        return _Response(error=httpx.HTTPStatusError("bad", request=request, response=response))

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(AIImageError, match="abgelehnt"):
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=True),
            b"x",
            "image/png",
            settings=_settings(ai_max_retries=3),
        )
    assert calls == 1


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": [{"b64_json": "%%%"}]}])
def test_generate_image_rejects_unusable_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Response(payload))

    with pytest.raises(AIImageError, match="keine verwendbare"):
        image_client.maybe_generate_recipe_image(
            _extracted(has_recipe_image=True), b"x", "image/png", settings=_settings()
        )


class _Session:
    def __init__(self) -> None:
        self.scalar_results: list[Any] = []
        self.scalars_results: list[list[Any]] = []
        self.get_results: dict[type[Any], Any] = {}
        self.execute_results: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.get_calls: list[type[Any]] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def scalar(self, statement: object) -> Any:
        del statement
        return self.scalar_results.pop(0)

    def scalars(self, statement: object) -> list[Any]:
        del statement
        return self.scalars_results.pop(0)

    def get(self, model: type[Any], identifier: object) -> Any:
        self.get_calls.append(model)
        if model is ImportCandidate:
            return next(
                (
                    item
                    for item in self.added
                    if isinstance(item, ImportCandidate) and item.id == identifier
                ),
                None,
            )
        return self.get_results.get(model)

    def execute(self, statement: object) -> Any:
        del statement
        if self.execute_results:
            return self.execute_results.pop(0)
        return SimpleNamespace(rowcount=1)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def flush(self) -> None:
        self.flushes += 1

    def add(self, value: Any) -> None:
        if isinstance(value, ImportCandidate) and value.id is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    def delete(self, value: Any) -> None:
        self.deleted.append(value)


def _pipeline_objects(
    source_path: Path,
    *,
    input_type: str = "image",
    source_url: str | None = None,
) -> tuple[Any, Any, Any, Any]:
    user = SimpleNamespace(id=uuid.uuid4(), visible_name="Ada")
    batch = SimpleNamespace(
        id=uuid.uuid4(),
        created_by_user_id=user.id,
        status="queued",
        jobs=[],
        total_jobs=1,
        completed_jobs=0,
        failed_jobs=0,
        target_language="de",
    )
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        storage_key="source-key",
        mime_type="application/pdf" if input_type in {"pdf", "url"} else "image/png",
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        batch_id=batch.id,
        batch=batch,
        input_type=input_type,
        source_url=source_url,
        source_asset=None if input_type == "url" else asset,
        source_asset_id=None if input_type == "url" else asset.id,
        status="queued",
        progress=0,
        current_stage="Wartet",
        suggestions_json=None,
        result_recipe_id=None,
        finished_at=None,
        lease_token=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        candidates=[],
    )
    batch.jobs = [job]
    source_path.write_bytes(b'{"title":"Quelle"}')
    return user, batch, asset, job


def _install_pipeline_success_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    input_type: str = "image",
    source_url: str | None = None,
    extracted: ExtractedRecipe | None = None,
    detected: DetectedRecipeDocument | None = None,
    settings: Settings | None = None,
    use_real_crops: bool = False,
) -> tuple[_Session, Any, Any, Any, list[str], Any]:
    source_path = tmp_path / "source.bin"
    user, batch, asset, job = _pipeline_objects(
        source_path, input_type=input_type, source_url=source_url
    )
    db = _Session()
    db.get_results = {User: user, ImportJob: job, ImportBatch: batch}
    stages: list[str] = []
    recipe = SimpleNamespace(
        id=uuid.uuid4(),
        slug="kartoffelsuppe",
        title="Kartoffelsuppe",
        original_assets=[],
        images=[],
    )
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: settings or _settings(ai_image_generation_enabled=False),
    )
    monkeypatch.setattr(pipeline, "_maintenance_check", lambda: None)
    monkeypatch.setattr(pipeline, "_claim_job", lambda *_args: job)
    monkeypatch.setattr(pipeline, "_stage", lambda _db, _job, status, _lease: stages.append(status))
    monkeypatch.setattr(
        pipeline,
        "_progress",
        lambda _db, _job_id, _lease, *, status, **_kwargs: stages.append(status),
    )
    monkeypatch.setattr(pipeline, "resolve_storage_key", lambda _key: source_path)
    monkeypatch.setattr(pipeline, "_category_paths", lambda _db: ["Alltag › Suppe"])
    monkeypatch.setattr(pipeline, "_prepare_detection_source", lambda data, mime: (data, mime))
    monkeypatch.setattr(pipeline, "source_page_count", lambda *_args: 1)
    monkeypatch.setattr(
        pipeline,
        "detect_recipes",
        lambda **_kwargs: detected or _detected_document(),
    )
    if not use_real_crops:
        monkeypatch.setattr(pipeline, "crop_source_region", lambda *_args, **_kwargs: b"crop")
    monkeypatch.setattr(pipeline, "extract_recipe", lambda **_kwargs: extracted or _extracted())
    monkeypatch.setattr(pipeline, "create_recipe", lambda *_args: recipe)
    monkeypatch.setattr(pipeline, "enforce_recipe_quota", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "recompute_batch", lambda *_args: None)
    return db, user, asset, job, stages, recipe


def test_selected_candidates_are_promoted_and_unselected_media_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), visible_name="Ada")
    source_asset = SimpleNamespace(id=uuid.uuid4(), storage_key="source.png")
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status="review",
        source_asset=source_asset,
        result_recipe_id=None,
        candidates=[],
    )
    selected_image = SimpleNamespace(id=uuid.uuid4(), storage_key="selected.png")
    selected_thumbnail = SimpleNamespace(id=uuid.uuid4(), storage_key="selected-thumb.jpg")
    skipped_image = SimpleNamespace(id=uuid.uuid4(), storage_key="skipped.png")
    skipped_thumbnail = SimpleNamespace(id=uuid.uuid4(), storage_key="skipped-thumb.jpg")

    def candidate(
        title: str,
        image: SimpleNamespace,
        thumbnail: SimpleNamespace,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            job_id=job.id,
            job=job,
            status="ready",
            title=title,
            recipe_payload=RecipeInput(title=title, base_servings=Decimal("4")).model_dump(
                mode="json"
            ),
            image_asset_id=image.id,
            thumbnail_asset_id=thumbnail.id,
            image_asset=image,
            thumbnail_asset=thumbnail,
            image_metadata_json={"origin": "verified_import_crop"},
            result_recipe_id=None,
            finished_at=None,
        )

    selected = candidate("Suppe", selected_image, selected_thumbnail)
    skipped = candidate("Kuchen", skipped_image, skipped_thumbnail)
    job.candidates = [selected, skipped]
    batch = SimpleNamespace(id=uuid.uuid4(), status="review", jobs=[job], target_language="de")
    db = _Session()
    db.scalars_results = [[selected, skipped]]
    created = SimpleNamespace(
        id=uuid.uuid4(),
        title="Suppe",
        original_assets=[],
        images=[],
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline,
        "create_recipe",
        lambda _db, payload, actor: captured.update(payload=payload, actor=actor) or created,
    )
    monkeypatch.setattr(pipeline, "enforce_recipe_quota", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "recompute_batch", lambda *_args: None)
    monkeypatch.setattr(pipeline, "resolve_storage_key", lambda key: tmp_path / key)

    recipes, cleanup_paths = pipeline.import_selected_candidates(
        db,
        batch=batch,
        selected_ids={selected.id},
        user=user,
    )

    assert recipes == [created]
    assert captured["payload"].title == "Suppe"
    assert captured["actor"] is user
    assert selected.status == "imported" and selected.result_recipe_id == created.id
    assert skipped.status == "discarded"
    assert created.original_assets[0].media_asset_id == source_asset.id
    assert created.images[0].media_asset_id == selected_image.id
    assert created.images[0].thumbnail_asset_id == selected_thumbnail.id
    assert selected.image_asset_id is None and selected.thumbnail_asset_id is None
    assert skipped.image_asset_id is None and skipped.thumbnail_asset_id is None
    assert cleanup_paths == [tmp_path / "skipped.png", tmp_path / "skipped-thumb.jpg"]
    assert db.deleted == [skipped_image, skipped_thumbnail]
    assert job.status == "completed" and job.result_recipe_id == created.id


def test_empty_multi_recipe_selection_discards_the_only_ready_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), visible_name="Ada")
    source_asset = SimpleNamespace(id=uuid.uuid4(), storage_key="source.png")
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status="review",
        progress=100,
        current_stage="Bereit",
        source_asset=source_asset,
        result_recipe_id=None,
        finished_at=None,
    )
    candidate = SimpleNamespace(
        id=uuid.uuid4(),
        job_id=job.id,
        job=job,
        status="ready",
        title="Suppe",
        recipe_payload=RecipeInput(title="Suppe", base_servings=Decimal("4")).model_dump(
            mode="json"
        ),
        image_asset_id=None,
        thumbnail_asset_id=None,
        image_asset=None,
        thumbnail_asset=None,
        image_metadata_json=None,
        result_recipe_id=None,
        finished_at=None,
    )
    batch = SimpleNamespace(id=uuid.uuid4(), status="review", jobs=[job], target_language="de")
    db = _Session()
    db.scalars_results = [[candidate]]
    monkeypatch.setattr(
        pipeline,
        "create_recipe",
        lambda *_args: pytest.fail("An empty selection must not create a recipe"),
    )
    monkeypatch.setattr(pipeline, "recompute_batch", lambda *_args: None)

    recipes, cleanup_paths = pipeline.import_selected_candidates(
        db,
        batch=batch,
        selected_ids=set(),
        user=user,
    )

    assert recipes == []
    assert cleanup_paths == []
    assert candidate.status == "discarded"
    assert candidate.result_recipe_id is None
    assert job.status == "completed"
    assert job.current_stage == "Import ohne Rezeptauswahl abgeschlossen"


def test_process_single_recipe_imports_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, asset, job, stages, recipe = _install_pipeline_success_fakes(
        monkeypatch, tmp_path, input_type="image"
    )
    captured: dict[str, Any] = {}

    def extract(**kwargs: Any) -> ExtractedRecipe:
        captured.update(kwargs)
        return _extracted()

    monkeypatch.setattr(pipeline, "extract_recipe", extract)
    pipeline._process_import_job(job.id)

    assert stages == ["preparing", "extracting", "extracting", "checking_images"]
    assert captured["images"] == [b"crop"]
    assert job.status == "completed"
    assert job.current_stage == "Rezept importiert"
    assert job.progress == 100
    assert job.result_recipe_id == recipe.id
    assert recipe.original_assets[0].media_asset_id == asset.id
    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    payload = candidate.recipe_payload
    assert candidate.status == "imported"
    assert candidate.result_recipe_id == recipe.id
    assert candidate.title == "Kartoffelsuppe"
    assert payload is not None and payload["status"] == "active"
    assert [(item["path"], item["origin"]) for item in payload["categories"]] == [
        (["Suppen"], "ai_import")
    ]
    assert candidate.source_regions_json[0]["page"] == 1
    assert job.suggestions_json["ready_recipes"] == 1
    assert asset.id is not None


def test_process_url_renders_and_persists_source_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, user, _asset, job, stages, recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        input_type="url",
        source_url="https://example.test/rezept",
    )
    snapshot_path = tmp_path / "snapshot.pdf"
    snapshot_path.write_bytes(b"gerenderte webseite")
    snapshot_asset = SimpleNamespace(
        id=uuid.uuid4(), storage_key="snapshot-key", mime_type="application/pdf"
    )
    stored = SimpleNamespace(storage_key="snapshot-key")
    monkeypatch.setattr(pipeline, "_render_url", lambda url: b"pdf:" + url.encode())
    monkeypatch.setattr(pipeline, "store_bytes", lambda *args, **kwargs: stored)
    monkeypatch.setattr(
        pipeline,
        "resolve_storage_key",
        lambda key: snapshot_path if key == "snapshot-key" else tmp_path / "source.bin",
    )
    create_calls: list[tuple[Any, str]] = []

    def create_asset(_db: Any, _stored: Any, actor: Any, kind: str) -> Any:
        create_calls.append((actor, kind))
        return snapshot_asset

    monkeypatch.setattr(pipeline, "create_asset", create_asset)
    pipeline._process_import_job(job.id)

    assert create_calls == [(user, "url_snapshot_pdf")]
    assert job.source_asset_id == snapshot_asset.id
    assert recipe.original_assets[0].media_asset_id == snapshot_asset.id
    assert stages == ["preparing", "extracting", "extracting", "checking_images"]
    assert job.status == "completed"
    assert job.result_recipe_id == recipe.id
    assert snapshot_path.exists(), "Committed URL snapshots must not be cleanup candidates"
    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert candidate.status == "imported"
    assert candidate.recipe_payload is not None
    assert candidate.recipe_payload["source"]["title"] == "example.test"
    assert candidate.recipe_payload["source"]["url"] == "https://example.test/rezept"
    assert candidate.recipe_payload["nutrition"][0]["energy_kcal"] == "347"


def test_url_render_failure_is_actionable_job_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, _asset, job, stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        input_type="url",
        source_url="https://example.test/rezept",
    )

    def fail(_url: str) -> bytes:
        raise pipeline.URLRenderError("Die Webseite hat nicht rechtzeitig geladen.")

    monkeypatch.setattr(pipeline, "_render_url", fail)

    pipeline._process_import_job(job.id)

    assert stages == ["preparing"]
    assert job.status == "failed"
    assert job.error_code == "url_render_failed"
    assert job.error_message == "Die Webseite hat nicht rechtzeitig geladen."
    assert job.finished_at is not None
    assert db.rollbacks == 1
    assert db.commits == 1


def test_process_import_generates_cover_and_thumbnail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_candidate = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=50, top=50, right=450, bottom=450),
        description="Suppenschüssel",
        confidence=0.94,
    )
    detected = _detected_document(
        DetectedRecipe(
            title_hint="Kartoffelsuppe",
            source_regions=[RecipeSourceRegion(page=1)],
            recipe_image_candidates=[image_candidate],
            detection_confidence="high",
        )
    )
    db, user, _asset, job, stages, recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        extracted=_extracted(),
        detected=detected,
        settings=_settings(ai_image_generation_enabled=True),
    )
    image_path = tmp_path / "generated.png"
    thumbnail_path = tmp_path / "thumbnail.webp"
    image_path.write_bytes(b"png")
    thumbnail_path.write_bytes(b"thumb")
    stored = SimpleNamespace(storage_key="image-key")
    image_asset = SimpleNamespace(id=uuid.uuid4(), storage_key="image-key")
    thumbnail = SimpleNamespace(id=uuid.uuid4(), storage_key="thumbnail-key")
    monkeypatch.setattr(pipeline, "maybe_generate_recipe_image", lambda *_args, **_kwargs: b"png")
    monkeypatch.setattr(pipeline, "store_bytes", lambda *args, **kwargs: stored)
    monkeypatch.setattr(
        pipeline,
        "resolve_storage_key",
        lambda key: thumbnail_path if key == "thumbnail-key" else image_path,
    )
    monkeypatch.setattr(pipeline, "create_asset", lambda *_args: image_asset)
    monkeypatch.setattr(pipeline, "create_thumbnail_asset", lambda *_args: thumbnail)
    monkeypatch.setattr(
        pipeline,
        "verify_recipe_image",
        lambda **_kwargs: RecipeImageMatch(
            matches_recipe=True,
            confidence=0.96,
            reason="Gericht passt eindeutig",
        ),
    )

    pipeline._process_import_job(job.id)

    assert stages == [
        "preparing",
        "extracting",
        "extracting",
        "checking_images",
        "generating_image",
    ]
    assert job.status == "completed"
    assert len(recipe.images) == 1
    assert recipe.images[0].media_asset_id == image_asset.id
    assert recipe.images[0].thumbnail_asset_id == thumbnail.id
    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert candidate.status == "imported"
    assert candidate.image_asset_id is None
    assert candidate.thumbnail_asset_id is None
    assert candidate.image_region_json == image_candidate.model_dump(mode="json")
    assert candidate.image_metadata_json["origin"] == "ai_prepared_import"
    assert candidate.image_metadata_json["verification_confidence"] == 0.96
    assert candidate.image_metadata_json["generated_verification_confidence"] == 0.96
    assert candidate.image_metadata_json["model"] == _settings().ai_image_model
    assert user.id is not None


def test_generated_candidate_image_is_rejected_when_it_no_longer_matches_recipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_candidate = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=0, top=0, right=600, bottom=600),
        description="Suppe",
        confidence=0.95,
    )
    detected = _detected_document(
        DetectedRecipe(
            title_hint="Kartoffelsuppe",
            source_regions=[RecipeSourceRegion(page=1)],
            recipe_image_candidates=[image_candidate],
        )
    )
    db, _user, _asset, job, _stages, recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        detected=detected,
        settings=_settings(ai_image_generation_enabled=True),
    )
    matches = iter(
        [
            RecipeImageMatch(matches_recipe=True, confidence=0.96, reason="Original passt"),
            RecipeImageMatch(matches_recipe=False, confidence=0.2, reason="Falsches Gericht"),
        ]
    )
    stored_images: list[bytes] = []
    monkeypatch.setattr(pipeline, "verify_recipe_image", lambda **_kwargs: next(matches))
    monkeypatch.setattr(
        pipeline, "maybe_generate_recipe_image", lambda *_args, **_kwargs: b"anderes gericht"
    )
    monkeypatch.setattr(
        pipeline,
        "store_bytes",
        lambda data, **_kwargs: (
            stored_images.append(data) or SimpleNamespace(storage_key="verified.png")
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "create_asset",
        lambda *_args: SimpleNamespace(id=uuid.uuid4(), storage_key="verified.png"),
    )
    monkeypatch.setattr(pipeline, "create_thumbnail_asset", lambda *_args: None)

    pipeline._process_import_job(job.id)

    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert job.status == "completed"
    assert stored_images == [b"crop"]
    assert len(recipe.images) == 1
    assert candidate.image_metadata_json["origin"] == "verified_import_crop"
    assert any("passte nicht mehr eindeutig" in warning for warning in candidate.warnings_json)


def test_image_generation_failure_keeps_verified_crop_on_imported_recipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_candidate = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=100, top=100, right=500, bottom=500),
        description="Suppe",
        confidence=0.9,
    )
    detected = _detected_document(
        DetectedRecipe(
            title_hint="Kartoffelsuppe",
            source_regions=[RecipeSourceRegion(page=1)],
            recipe_image_candidates=[image_candidate],
        )
    )
    db, _user, _asset, job, stages, recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        extracted=_extracted(),
        detected=detected,
        settings=_settings(ai_image_generation_enabled=True),
    )
    image_asset = SimpleNamespace(id=uuid.uuid4(), storage_key="image-key")
    monkeypatch.setattr(
        pipeline,
        "verify_recipe_image",
        lambda **_kwargs: RecipeImageMatch(
            matches_recipe=True,
            confidence=0.91,
            reason="passt",
        ),
    )
    monkeypatch.setattr(
        pipeline, "store_bytes", lambda *_args, **_kwargs: SimpleNamespace(storage_key="image-key")
    )
    monkeypatch.setattr(pipeline, "create_asset", lambda *_args: image_asset)
    monkeypatch.setattr(pipeline, "create_thumbnail_asset", lambda *_args: None)

    def fail_image(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AIImageError("Bildmodell vorübergehend nicht verfügbar")

    monkeypatch.setattr(pipeline, "maybe_generate_recipe_image", fail_image)

    pipeline._process_import_job(job.id)

    assert job.status == "completed"
    assert len(recipe.images) == 1
    assert recipe.images[0].media_asset_id == image_asset.id
    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert candidate.status == "imported"
    assert candidate.image_asset_id is None
    assert candidate.image_metadata_json["origin"] == "verified_import_crop"
    assert any("Originalausschnitt" in warning for warning in candidate.warnings_json)
    assert "generating_image" in stages


def test_two_recipes_are_extracted_and_receive_only_their_matching_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    left_box = NormalizedBoundingBox(left=0, top=0, right=500, bottom=1000)
    right_box = NormalizedBoundingBox(left=500, top=0, right=1000, bottom=1000)
    left_image = RecipeImageCandidate(
        page=1,
        bounding_box=left_box,
        description="rotes Gericht",
        confidence=0.92,
    )
    right_image = RecipeImageCandidate(
        page=1,
        bounding_box=right_box,
        description="blaues Gericht",
        confidence=0.91,
    )
    detected = _detected_document(
        DetectedRecipe(
            title_hint="Tomatensuppe",
            source_regions=[RecipeSourceRegion(page=1, bounding_box=left_box)],
            recipe_image_candidates=[left_image, right_image],
            detection_confidence="high",
        ),
        DetectedRecipe(
            title_hint="Blaubeerdessert",
            source_regions=[RecipeSourceRegion(page=1, bounding_box=right_box)],
            recipe_image_candidates=[left_image, right_image],
            detection_confidence="high",
        ),
    )
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        detected=detected,
        use_real_crops=True,
    )
    source = Image.new("RGB", (400, 200), "red")
    source.paste("blue", (200, 0, 400, 200))
    source.save(tmp_path / "source.bin", format="PNG")

    def dominant_color(data: bytes) -> str:
        with Image.open(BytesIO(data)) as image:
            red, _green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        return "red" if red > blue else "blue"

    def extract(*, images: list[bytes], **_kwargs: object) -> ExtractedRecipe:
        color = dominant_color(images[0])
        if color == "red":
            return _extracted(
                title="Tomatensuppe",
                ingredient_groups=[{"ingredients": [{"name": "Tomaten"}]}],
            )
        return _extracted(
            title="Blaubeerdessert",
            ingredient_groups=[{"ingredients": [{"name": "Blaubeeren"}]}],
        )

    verification_calls: list[tuple[str, str]] = []

    def verify(*, extracted: ExtractedRecipe, image: bytes, **_kwargs: object) -> RecipeImageMatch:
        color = dominant_color(image)
        verification_calls.append((extracted.title, color))
        matches = (extracted.title == "Tomatensuppe" and color == "red") or (
            extracted.title == "Blaubeerdessert" and color == "blue"
        )
        return RecipeImageMatch(
            matches_recipe=matches,
            confidence=0.97 if matches else 0.08,
            reason="passt" if matches else "anderes Gericht",
        )

    stored_images: list[bytes] = []
    stored_paths: dict[str, Path] = {}

    def store(data: bytes, **_kwargs: object) -> SimpleNamespace:
        key = f"candidate-{len(stored_images)}.png"
        path = tmp_path / key
        path.write_bytes(data)
        stored_images.append(data)
        stored_paths[key] = path
        return SimpleNamespace(storage_key=key)

    monkeypatch.setattr(pipeline, "extract_recipe", extract)
    monkeypatch.setattr(pipeline, "verify_recipe_image", verify)
    monkeypatch.setattr(pipeline, "store_bytes", store)
    monkeypatch.setattr(
        pipeline,
        "resolve_storage_key",
        lambda key: tmp_path / "source.bin" if key == "source-key" else stored_paths[key],
    )
    monkeypatch.setattr(
        pipeline,
        "create_asset",
        lambda _db, stored, *_args: SimpleNamespace(
            id=uuid.uuid4(), storage_key=stored.storage_key
        ),
    )
    monkeypatch.setattr(pipeline, "create_thumbnail_asset", lambda *_args: None)

    pipeline._process_import_job(job.id)

    candidates = sorted(
        (item for item in db.added if isinstance(item, ImportCandidate)),
        key=lambda item: item.position,
    )
    assert job.status == "review"
    assert [candidate.status for candidate in candidates] == ["ready", "ready"]
    assert [candidate.recipe_payload["title"] for candidate in candidates] == [
        "Tomatensuppe",
        "Blaubeerdessert",
    ]
    assert candidates[0].image_region_json["bounding_box"] == left_box.model_dump(mode="json")
    assert candidates[1].image_region_json["bounding_box"] == right_box.model_dump(mode="json")
    assert [dominant_color(image) for image in stored_images] == ["red", "blue"]
    assert set(verification_calls) == {
        ("Tomatensuppe", "red"),
        ("Tomatensuppe", "blue"),
        ("Blaubeerdessert", "red"),
        ("Blaubeerdessert", "blue"),
    }


def test_single_candidate_is_only_finalized_after_second_extraction_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(ai_image_generation_enabled=False, ai_max_retries=3)
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        settings=settings,
    )
    client = _Client(
        [
            _Response({"output_text": '{"title":""}'}),
            _Response({"output_text": '{"title":"Gerettetes Rezept"}'}),
        ]
    )
    _install_client(monkeypatch, client)

    def real_extract(
        *,
        images: list[bytes],
        existing_category_paths: list[str],
        settings: Settings,
        **_kwargs: object,
    ) -> ExtractedRecipe:
        return extraction_client.extract_recipe(
            images=images,
            existing_category_paths=existing_category_paths,
            settings=settings,
        )

    monkeypatch.setattr(pipeline, "extract_recipe", real_extract)

    pipeline._process_import_job(job.id)

    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert len(client.requests) == 2
    assert job.status == "completed"
    assert candidate.status == "imported"
    assert candidate.title == "Gerettetes Rezept"


def test_multi_import_fails_only_exhausted_candidate_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    detected = _detected_document(
        DetectedRecipe(title_hint="Kaputt", source_regions=[RecipeSourceRegion(page=1)]),
        DetectedRecipe(title_hint="Gut", source_regions=[RecipeSourceRegion(page=1)]),
    )
    settings = _settings(ai_image_generation_enabled=False)
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        detected=detected,
        settings=settings,
    )
    client = _Client(
        [
            _Response({"output_text": '{"title":""}'}),
            _Response({"output_text": '{"title":""}'}),
            _Response({"output_text": '{"title":"Verwendbares Rezept"}'}),
        ]
    )
    _install_client(monkeypatch, client)

    def real_extract(
        *,
        images: list[bytes],
        existing_category_paths: list[str],
        settings: Settings,
        **_kwargs: object,
    ) -> ExtractedRecipe:
        return extraction_client.extract_recipe(
            images=images,
            existing_category_paths=existing_category_paths,
            settings=settings,
        )

    monkeypatch.setattr(pipeline, "extract_recipe", real_extract)

    pipeline._process_import_job(job.id)

    candidates = sorted(
        (item for item in db.added if isinstance(item, ImportCandidate)),
        key=lambda item: item.position,
    )
    assert len(client.requests) == 3
    assert job.status == "review"
    assert [candidate.status for candidate in candidates] == ["failed", "ready"]
    assert candidates[0].error_message == (
        "Die Rezeptdaten konnten nicht zuverlässig extrahiert werden: title ist leer oder zu kurz."
    )
    assert candidates[1].title == "Verwendbares Rezept"


def test_single_failed_candidate_exposes_its_safe_validation_detail_on_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
    )

    def fail_extraction(**_kwargs: object) -> ExtractedRecipe:
        raise AIExtractionError(
            "Die Rezeptdaten konnten nicht zuverlässig extrahiert werden: title ist leer."
        )

    monkeypatch.setattr(pipeline, "extract_recipe", fail_extraction)

    pipeline._process_import_job(job.id)

    candidate = next(item for item in db.added if isinstance(item, ImportCandidate))
    assert job.status == "failed"
    assert job.error_code == "candidate_extraction_failed"
    assert job.error_message == candidate.error_message
    assert job.error_message.endswith("title ist leer.")


@pytest.mark.parametrize("error", [AIUnavailable("offline"), AIExtractionError("kaputt")])
def test_ai_failure_marks_job_failed_with_actionable_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch, tmp_path
    )

    def fail(**kwargs: object) -> None:
        del kwargs
        raise error

    monkeypatch.setattr(pipeline, "detect_recipes", fail)

    pipeline._process_import_job(job.id)

    assert job.status == "failed"
    assert job.error_code == "ai_unavailable"
    assert job.error_message == str(error)
    assert job.lease_token is None
    assert db.rollbacks == 1
    assert db.commits == 2


def test_generic_pipeline_failure_rolls_back_and_removes_uncommitted_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_candidate = RecipeImageCandidate(
        page=1,
        bounding_box=NormalizedBoundingBox(left=0, top=0, right=500, bottom=500),
        description="Gericht",
        confidence=0.9,
    )
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        extracted=_extracted(),
        detected=_detected_document(
            DetectedRecipe(
                title_hint="Kartoffelsuppe",
                source_regions=[RecipeSourceRegion(page=1)],
                recipe_image_candidates=[image_candidate],
            )
        ),
    )
    generated_path = tmp_path / "not-committed.png"
    generated_path.write_bytes(b"generated")
    monkeypatch.setattr(
        pipeline,
        "verify_recipe_image",
        lambda **_kwargs: RecipeImageMatch(
            matches_recipe=True,
            confidence=0.9,
            reason="passt",
        ),
    )
    monkeypatch.setattr(
        pipeline, "store_bytes", lambda *_args, **_kwargs: SimpleNamespace(storage_key="generated")
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_storage_key",
        lambda key: generated_path if key == "generated" else tmp_path / "source.bin",
    )

    def fail_create(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("Datenbankverbindung verloren")

    monkeypatch.setattr(pipeline, "create_asset", fail_create)

    pipeline._process_import_job(job.id)

    assert job.status == "failed"
    assert job.error_code == "pipeline_error"
    assert "Original bleibt erhalten" in job.error_message
    assert not generated_path.exists()
    assert (tmp_path / "source.bin").exists()
    assert db.rollbacks >= 1


def test_maintenance_interruption_requeues_and_propagates_for_worker_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch, tmp_path
    )

    def maintenance_stage(*args: object) -> None:
        del args
        raise pipeline.ImportMaintenance("restore")

    monkeypatch.setattr(pipeline, "_stage", maintenance_stage)

    with pytest.raises(pipeline.ImportMaintenance, match="restore"):
        pipeline._process_import_job(job.id)

    assert db.rollbacks == 1
    assert db.commits == 1
    assert len(db.execute_results) == 0


def test_lost_lease_aborts_without_overwriting_new_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        pipeline,
        "_stage",
        lambda *_args: (_ for _ in ()).throw(pipeline.ImportLeaseLost("übernommen")),
    )

    pipeline._process_import_job(job.id)

    assert job.status == "queued"
    assert db.rollbacks == 1
    assert db.commits == 0


def test_claim_none_is_idempotent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Session()
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: db)
    monkeypatch.setattr(pipeline, "_maintenance_check", lambda: None)
    monkeypatch.setattr(pipeline, "_claim_job", lambda *_args: None)

    pipeline._process_import_job(uuid.uuid4())

    assert db.commits == 0
    assert db.rollbacks == 0


def test_lease_duration_uses_safe_floor_and_scales_for_long_ai_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(ai_timeout_seconds=10, ai_max_retries=0),
    )
    assert pipeline._lease_duration().total_seconds() == 30 * 60

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(ai_timeout_seconds=900, ai_max_retries=10),
    )
    assert pipeline._lease_duration().total_seconds() == 900 * 11 + 300


def test_maintenance_check_reads_redis_and_blocks_during_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MaintenanceRedis:
        def __init__(self, value: str | None) -> None:
            self.value = value
            self.get_calls: list[str] = []

        def __enter__(self) -> MaintenanceRedis:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def get(self, key: str) -> str | None:
            self.get_calls.append(key)
            return self.value

    redis = MaintenanceRedis(None)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://example/0"),
    )
    monkeypatch.setattr(
        pipeline,
        "Redis",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: redis),
    )
    pipeline._maintenance_check()
    assert redis.get_calls == ["maintenance:restore", "maintenance:backup"]

    redis.value = "restore-owner"
    with pytest.raises(pipeline.ImportMaintenance, match="Wartungsauftrag"):
        pipeline._maintenance_check()


def test_claim_job_is_atomic_and_duplicate_claim_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = uuid.uuid4()
    job = SimpleNamespace(id=identifier)
    db = _Session()
    db.scalar_results = [identifier, None]
    db.get_results[ImportJob] = job
    monkeypatch.setattr(pipeline, "_lease_duration", lambda: timedelta(hours=1))

    claimed: Any = pipeline._claim_job(cast(Any, db), identifier, "lease")
    assert claimed is job
    assert pipeline._claim_job(cast(Any, db), identifier, "other") is None
    assert db.commits == 2


def test_stage_commits_only_for_current_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    job = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(pipeline, "CursorResult", FakeCursor)
    monkeypatch.setattr(pipeline, "_maintenance_check", lambda: None)
    monkeypatch.setattr(pipeline, "_lease_duration", lambda: timedelta(hours=1))
    db = _Session()
    db.execute_results = [FakeCursor(1), FakeCursor(0), SimpleNamespace(rowcount=1)]

    pipeline._stage(cast(Any, db), cast(Any, job), "extracting", "owner")
    assert db.commits == 1

    with pytest.raises(pipeline.ImportLeaseLost, match="anderen Worker"):
        pipeline._stage(cast(Any, db), cast(Any, job), "validating", "stale")
    with pytest.raises(pipeline.ImportLeaseLost, match="anderen Worker"):
        pipeline._stage(cast(Any, db), cast(Any, job), "validating", "wrong-type")
    assert db.rollbacks == 2


def test_requeue_stale_imports_returns_exact_claimed_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers = [uuid.uuid4(), uuid.uuid4()]
    db = _Session()
    db.scalars_results = [identifiers]
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: db)
    monkeypatch.setattr(pipeline, "database_maintenance_guard", nullcontext)

    assert pipeline.requeue_stale_imports() == identifiers
    assert db.commits == 1


def test_category_paths_uses_full_hierarchy() -> None:
    db = _Session()
    db.scalars_results = [
        [SimpleNamespace(path="Küche › Deutsch"), SimpleNamespace(path="Saison › Winter")]
    ]

    assert pipeline._category_paths(cast(Any, db)) == ["Küche › Deutsch", "Saison › Winter"]


@pytest.mark.parametrize(
    ("input_type", "source_url", "remove_asset", "remove_user"),
    [
        ("url", None, False, False),
        ("image", None, True, False),
        ("image", None, False, True),
    ],
)
def test_missing_import_prerequisite_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    input_type: str,
    source_url: str | None,
    remove_asset: bool,
    remove_user: bool,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch, tmp_path, input_type=input_type, source_url=source_url
    )
    if remove_asset:
        job.source_asset = None
    if remove_user:
        db.get_results[User] = None

    pipeline._process_import_job(job.id)

    assert job.status == "failed"
    assert job.error_code == "pipeline_error"
    assert db.rollbacks == 1


def test_url_import_without_requesting_user_removes_uncommitted_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, _user, _asset, job, _stages, _recipe = _install_pipeline_success_fakes(
        monkeypatch,
        tmp_path,
        input_type="url",
        source_url="https://example.test/rezept",
    )
    snapshot = tmp_path / "unowned-snapshot.pdf"
    snapshot.write_bytes(b"pdf")
    db.get_results[User] = None
    monkeypatch.setattr(pipeline, "_render_url", lambda _url: b"pdf")
    monkeypatch.setattr(
        pipeline, "store_bytes", lambda *_args, **_kwargs: SimpleNamespace(storage_key="snapshot")
    )
    monkeypatch.setattr(pipeline, "resolve_storage_key", lambda _key: snapshot)

    pipeline._process_import_job(job.id)

    assert job.status == "failed"
    assert not snapshot.exists()


def test_recompute_batch_handles_partial_success_failure_and_empty_batch() -> None:
    db = _Session()
    batch = SimpleNamespace(completed_jobs=0, failed_jobs=0, status="queued")
    db.get_results[ImportBatch] = batch
    db.scalars_results = [
        [SimpleNamespace(status="completed"), SimpleNamespace(status="failed")],
        [SimpleNamespace(status="completed"), SimpleNamespace(status="extracting")],
        [],
    ]

    pipeline.recompute_batch(cast(Any, db), uuid.uuid4())
    assert (batch.completed_jobs, batch.failed_jobs, batch.status) == (
        1,
        1,
        "completed_with_errors",
    )
    pipeline.recompute_batch(cast(Any, db), uuid.uuid4())
    assert batch.status == "processing"
    pipeline.recompute_batch(cast(Any, db), uuid.uuid4())
    assert batch.status == "completed"

    db.get_results[ImportBatch] = None
    pipeline.recompute_batch(cast(Any, db), uuid.uuid4())


def test_render_url_authenticates_and_returns_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _Response(content=b"%PDF-1.7\nrecipe")
    post = MagicMock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(renderer_url="http://renderer/", renderer_token="secret"),
    )

    assert pipeline._render_url("https://example.test/rezept") == b"%PDF-1.7\nrecipe"
    post.assert_called_once_with(
        "http://renderer/render/pdf",
        headers={"Authorization": "Bearer secret"},
        json={"url": "https://example.test/rezept"},
        timeout=120,
    )


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (413, {}, "Webseite ist zu groß"),
        (422, {"detail": "Interne Weiterleitung blockiert."}, "Interne Weiterleitung"),
        (504, {}, "nicht rechtzeitig geladen"),
        (500, {}, "konnte die Webseite nicht verarbeiten"),
    ],
)
def test_render_url_translates_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    payload: dict[str, Any],
    expected: str,
) -> None:
    request = httpx.Request("POST", "http://renderer/render/pdf")
    response = httpx.Response(status_code, request=request, json=payload)
    monkeypatch.setattr(httpx, "post", MagicMock(return_value=response))
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(renderer_url="http://renderer", renderer_token="secret"),
    )

    with pytest.raises(pipeline.URLRenderError, match=expected):
        pipeline._render_url("https://example.test/rezept")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            httpx.ReadTimeout(
                "timeout", request=httpx.Request("POST", "http://renderer/render/pdf")
            ),
            "nicht rechtzeitig geladen",
        ),
        (
            httpx.ConnectError(
                "offline", request=httpx.Request("POST", "http://renderer/render/pdf")
            ),
            "momentan nicht erreichbar",
        ),
    ],
)
def test_render_url_translates_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: httpx.RequestError,
    expected: str,
) -> None:
    monkeypatch.setattr(httpx, "post", MagicMock(side_effect=error))
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(renderer_url="http://renderer", renderer_token="secret"),
    )

    with pytest.raises(pipeline.URLRenderError, match=expected):
        pipeline._render_url("https://example.test/rezept")


def test_render_url_rejects_non_pdf_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", MagicMock(return_value=_Response(content=b"<html>oops")))
    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(renderer_url="http://renderer", renderer_token="secret"),
    )

    with pytest.raises(pipeline.URLRenderError, match="keine gültige PDF-Datei"):
        pipeline._render_url("https://example.test/rezept")


def test_process_import_job_uses_database_maintenance_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Guard:
        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, *args: object) -> None:
            del args
            events.append("exit")

    monkeypatch.setattr(pipeline, "database_maintenance_guard", Guard)
    monkeypatch.setattr(pipeline, "_process_import_job", lambda _job_id: events.append("process"))

    pipeline.process_import_job(uuid.uuid4())

    assert events == ["enter", "process", "exit"]


def test_process_import_batch_processes_only_selected_jobs_and_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id = uuid.uuid4()
    job_ids = [uuid.uuid4(), uuid.uuid4()]
    batch = SimpleNamespace(status="queued")
    first = _Session()
    first.get_results[ImportBatch] = batch
    first.scalars_results = [job_ids]
    second = _Session()
    second.get_results[ImportBatch] = batch
    sessions = [first, second]
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: sessions.pop(0))
    processed: list[uuid.UUID] = []
    monkeypatch.setattr(pipeline, "process_import_job", processed.append)
    recomputed: list[uuid.UUID] = []
    monkeypatch.setattr(
        pipeline, "recompute_batch", lambda _db, identifier: recomputed.append(identifier)
    )
    monkeypatch.setattr(pipeline, "database_maintenance_guard", nullcontext)

    pipeline.process_import_batch(batch_id)

    assert batch.status == "processing"
    assert processed == job_ids
    assert recomputed == [batch_id]
    assert first.commits == 1
    assert second.commits == 1


def test_process_import_batch_missing_batch_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Session()
    db.get_results[ImportBatch] = None
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: db)
    processed = MagicMock()
    monkeypatch.setattr(pipeline, "process_import_job", processed)
    monkeypatch.setattr(pipeline, "database_maintenance_guard", nullcontext)

    pipeline.process_import_batch(uuid.uuid4())

    processed.assert_not_called()
    assert db.commits == 0


class _Redis:
    def __init__(self, set_results: list[bool] | None = None) -> None:
        self.set_results = set_results or []
        self.set_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.eval_calls: list[tuple[Any, ...]] = []
        self.fail_eval = False

    def set(self, *args: Any, **kwargs: Any) -> bool:
        self.set_calls.append((args, kwargs))
        return self.set_results.pop(0)

    def eval(self, *args: Any) -> int:
        self.eval_calls.append(args)
        if self.fail_eval:
            raise RuntimeError("redis offline")
        return 1


def _install_worker_session(monkeypatch: pytest.MonkeyPatch, db: _Session, redis: _Redis) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        tasks,
        "Redis",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: redis),
    )
    monkeypatch.setattr(tasks, "database_maintenance_guard", nullcontext)
    monkeypatch.setattr(tasks, "database_maintenance_shared_guard", nullcontext)
    monkeypatch.setattr(tasks, "_maintenance_heartbeat", lambda *_args, **_kwargs: nullcontext())

    def claim(identifier: uuid.UUID, token: str, stage: str) -> bool:
        del identifier, stage
        claimed = db.scalar_results.pop(0)
        if claimed is None:
            return False
        job = db.get_results.get(BackupRestoreJob)
        if job is not None:
            job.status = "running"
            job.lease_token = token
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        return True

    monkeypatch.setattr(tasks, "_claim_maintenance_job", claim)


def test_import_actors_parse_uuid_and_retry_during_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = uuid.uuid4()
    calls: list[tuple[str, uuid.UUID]] = []
    monkeypatch.setattr(tasks, "process_import_batch", lambda value: calls.append(("batch", value)))
    monkeypatch.setattr(tasks, "process_import_job", lambda value: calls.append(("job", value)))

    cast(Any, tasks.import_batch_task).fn(str(identifier))
    cast(Any, tasks.import_job_task).fn(str(identifier))
    assert calls == [("batch", identifier), ("job", identifier)]

    def maintenance(_value: uuid.UUID) -> None:
        raise pipeline.ImportMaintenance("Restore läuft")

    monkeypatch.setattr(tasks, "process_import_job", maintenance)
    with pytest.raises(dramatiq.Retry, match="Restore läuft") as exc_info:
        cast(Any, tasks.import_job_task).fn(str(identifier))
    assert exc_info.value.delay == 60_000

    monkeypatch.setattr(tasks, "process_import_batch", maintenance)
    with pytest.raises(dramatiq.Retry, match="Restore läuft") as batch_exc:
        cast(Any, tasks.import_batch_task).fn(str(identifier))
    assert batch_exc.value.delay == 60_000


def test_release_lock_is_owner_safe_and_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    tasks._release_lock(cast(Any, redis), "lock", "owner")
    assert redis.eval_calls == [(tasks.RELEASE_LOCK_SCRIPT, 1, "lock", "owner")]

    redis.fail_eval = True
    logged = MagicMock()
    monkeypatch.setattr(tasks.logger, "exception", logged)
    tasks._release_lock(cast(Any, redis), "lock", "owner")
    logged.assert_called_once()


def _maintenance_job(identifier: uuid.UUID) -> Any:
    return SimpleNamespace(
        id=identifier,
        requested_by_user_id=uuid.uuid4(),
        status="queued",
        progress=0,
        current_stage="Wartet",
        started_at=None,
        finished_at=None,
        archive_filename=None,
        archive_sha256=None,
        summary_json=None,
        error_message=None,
        lease_token=None,
        lease_expires_at=None,
    )


def test_backup_task_claims_exports_audits_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identifier = uuid.uuid4()
    job = _maintenance_job(identifier)
    db = _Session()
    db.scalar_results = [identifier, None]
    db.get_results[BackupRestoreJob] = job
    redis = _Redis([True, True])
    _install_worker_session(monkeypatch, db, redis)
    archive = tmp_path / "backup.tar.zst"
    archive.write_bytes(b"backup")
    manifest = SimpleNamespace(model_dump=lambda **_kwargs: {"schema_version": "1"})

    def export_without_started_transaction(_db: object) -> tuple[Path, Any, str]:
        assert db.get_calls == []
        assert db.rollbacks == 1
        return archive, manifest, "abc123"

    monkeypatch.setattr(tasks, "export_backup", export_without_started_transaction)
    releases: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks, "_release_lock", lambda _redis, key, token: releases.append((key, token))
    )

    cast(Any, tasks.backup_task).fn(str(identifier))

    assert job.status == "completed"
    assert job.progress == 100
    assert job.archive_filename == archive.name
    assert job.archive_sha256 == "abc123"
    assert job.summary_json == {"schema_version": "1"}
    assert any(getattr(item, "action", None) == "backup.completed" for item in db.added)
    assert redis.set_calls[0][0][0] == tasks.MAINTENANCE_LOCK_KEY
    assert [key for key, _token in releases] == [
        "maintenance:backup",
        tasks.MAINTENANCE_LOCK_KEY,
    ]
    assert db.rollbacks == 2


def test_backup_task_handles_duplicate_delivery_and_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = uuid.uuid4()
    db = _Session()
    db.scalar_results = [None]
    redis = _Redis()
    _install_worker_session(monkeypatch, db, redis)
    cast(Any, tasks.backup_task).fn(str(identifier))
    assert redis.set_calls == []

    job = _maintenance_job(identifier)
    db = _Session()
    db.scalar_results = [identifier]
    db.get_results[BackupRestoreJob] = job
    redis = _Redis([False])
    _install_worker_session(monkeypatch, db, redis)
    cast(Any, tasks.backup_task).fn(str(identifier))
    assert job.status == "failed"
    assert "nicht verändert" in job.error_message
    assert db.rollbacks == 0


def test_restore_task_restores_admin_audits_cleans_upload_and_unlocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identifier = uuid.uuid4()
    upload = tmp_path / "restore-upload.tar.zst"
    upload.write_bytes(b"archive")
    job = _maintenance_job(identifier)
    db = _Session()
    db.scalar_results = [identifier, None]
    db.get_results[BackupRestoreJob] = job
    redis = _Redis([True, True])
    _install_worker_session(monkeypatch, db, redis)
    monkeypatch.setattr(tasks, "database_maintenance_guard", nullcontext)

    def restore_without_started_transaction(
        _db: object, path: Path, **kwargs: object
    ) -> dict[str, object]:
        assert db.get_calls == []
        assert db.rollbacks == 1
        job.status = "completed"
        job.lease_token = None
        job.lease_expires_at = None
        return {
            "summary": {"recipes": 3},
            "safety_backup": "safety.tar.zst",
            "path": str(path),
            "restore_id": kwargs["restore_id"],
        }

    monkeypatch.setattr(tasks, "restore_backup", restore_without_started_transaction)
    releases: list[str] = []
    monkeypatch.setattr(tasks, "_release_lock", lambda _redis, key, _token: releases.append(key))

    cast(Any, tasks.restore_task).fn(str(identifier), str(upload))

    assert job.status == "completed"
    assert [call[0][0] for call in redis.set_calls] == [
        tasks.MAINTENANCE_LOCK_KEY,
        "maintenance:restore",
    ]
    assert releases == ["maintenance:restore", tasks.MAINTENANCE_LOCK_KEY]
    assert not upload.exists()


def test_restore_task_marks_failure_and_always_deletes_sensitive_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identifier = uuid.uuid4()
    upload = tmp_path / "bad-restore.tar.zst"
    upload.write_bytes(b"archive")
    job = _maintenance_job(identifier)
    db = _Session()
    db.scalar_results = [identifier, None]
    db.get_results[BackupRestoreJob] = job
    redis = _Redis([True, True])
    _install_worker_session(monkeypatch, db, redis)
    monkeypatch.setattr(tasks, "database_maintenance_guard", nullcontext)

    def fail_restore(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ValueError("Prüfsumme falsch")

    monkeypatch.setattr(tasks, "restore_backup", fail_restore)

    cast(Any, tasks.restore_task).fn(str(identifier), str(upload))

    assert job.status == "failed"
    assert job.current_stage == "Wiederherstellung fehlgeschlagen"
    assert "Recovery" in job.error_message
    assert not upload.exists()
    assert db.rollbacks == 1


def test_restore_task_duplicate_delivery_does_not_touch_inflight_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identifier = uuid.uuid4()
    upload = tmp_path / "inflight-restore.tar.zst"
    upload.write_bytes(b"archive still used by owner")
    db = _Session()
    db.scalar_results = [None]
    redis = _Redis()
    _install_worker_session(monkeypatch, db, redis)

    cast(Any, tasks.restore_task).fn(str(identifier), str(upload))

    assert upload.exists()
    assert redis.set_calls == []


@pytest.mark.parametrize(
    ("set_results", "restore_result", "expected_releases"),
    [
        ([False], {"summary": {}, "safety_backup": "unused"}, []),
        ([True, False], {"summary": {}, "safety_backup": "unused"}, [tasks.MAINTENANCE_LOCK_KEY]),
    ],
)
def test_restore_task_rejects_lock_flag_or_missing_restored_admin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    set_results: list[bool],
    restore_result: dict[str, Any],
    expected_releases: list[str],
) -> None:
    identifier = uuid.uuid4()
    upload = tmp_path / f"restore-{len(set_results)}.tar.zst"
    upload.write_bytes(b"archive")
    job = _maintenance_job(identifier)
    db = _Session()
    db.scalar_results = [identifier]
    db.get_results[BackupRestoreJob] = job
    redis = _Redis(list(set_results))
    _install_worker_session(monkeypatch, db, redis)
    monkeypatch.setattr(tasks, "database_maintenance_guard", nullcontext)
    monkeypatch.setattr(tasks, "restore_backup", lambda *_args, **_kwargs: restore_result)
    releases: list[str] = []
    monkeypatch.setattr(tasks, "_release_lock", lambda _redis, key, _token: releases.append(key))

    cast(Any, tasks.restore_task).fn(str(identifier), str(upload))

    assert job.status == "failed"
    assert job.current_stage == "Wiederherstellung fehlgeschlagen"
    assert releases == expected_releases
    assert not upload.exists()
