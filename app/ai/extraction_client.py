from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any, cast

import httpx
from pydantic import BaseModel, ValidationError

from app.ai.extraction_repair import (
    finalize_extracted_recipe,
    repair_categories_payload,
    repair_extracted_recipe_payload,
)
from app.ai.prompts import (
    detection_prompt,
    detection_system_prompt,
    extraction_prompt,
    extraction_system_prompt,
    image_match_prompt,
    image_match_system_prompt,
)
from app.ai.recipe_localization import normalize_recipe_units
from app.config import Settings, get_settings
from app.i18n import DEFAULT_LOCALE, Locale
from app.schemas.ai import (
    DetectedRecipeDocument,
    ExtractedRecipe,
    ExtractedRecipeDraft,
    RecipeImageMatch,
)

logger = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    pass


class AIExtractionError(RuntimeError):
    pass


class _RetryableExtractionError(AIExtractionError):
    pass


MAX_STRUCTURED_EXTRACTION_ATTEMPTS = 2


def strict_response_schema(model: type[BaseModel] = ExtractedRecipeDraft) -> dict[str, Any]:
    """Return the OpenAI Structured Outputs subset for the extraction model.

    Pydantic deliberately omits fields with defaults from ``required`` and
    emits ``default`` keywords. Strict Structured Outputs instead requires
    every declared property at every object level, disallows additional
    properties, and represents optional values as nullable required fields.
    Pydantic already emits those nullable unions, so the remaining conversion
    can be performed without maintaining a second schema by hand.
    """

    schema = model.model_json_schema()

    def normalize(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                normalize(item)
            return
        if not isinstance(value, dict):
            return

        # Defaults are application-side fallbacks and are not part of the
        # strict schema subset accepted by the Responses API. ``format`` is
        # descriptive validation metadata emitted by Pydantic (for example
        # ``format: uri`` for HttpUrl), but it is not accepted consistently by
        # Responses-compatible providers. Decimal fields also emit regex
        # ``pattern`` values containing lookarounds, which are outside the
        # regex dialect supported by some providers. Pydantic still validates
        # the parsed model after extraction, so removing these annotations here
        # does not weaken the application's URL or numeric validation.
        value.pop("default", None)
        value.pop("format", None)
        value.pop("pattern", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for child in value.values():
            normalize(child)

    normalize(schema)
    return schema


def _provider_error_detail(response: httpx.Response) -> str | None:
    """Extract a short, safe provider error without exposing request data."""

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    detail = " ".join(message.split())
    return detail[:500] or None


def _extract_output_text(payload: dict[str, Any]) -> str:
    if payload.get("status") == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason", "unbekannt") if isinstance(details, dict) else "unbekannt"
        raise _RetryableExtractionError(f"Die KI-Antwort war unvollständig ({reason})")
    if isinstance(payload.get("output_text"), str):
        return cast(str, payload["output_text"])
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "refusal":
                raise AIExtractionError("Der KI-Dienst hat die Extraktion abgelehnt")
            if content.get("type") in {"output_text", "text"} and isinstance(
                content.get("text"), str
            ):
                return cast(str, content["text"])
    raise _RetryableExtractionError("Der KI-Dienst hat keine auswertbare Antwort geliefert")


def _repair_categories(data: dict[str, Any]) -> dict[str, Any]:
    return repair_categories_payload(data)


def _validation_error_message(exc: ValidationError, failure_message: str) -> str:
    descriptions = {
        "missing": "fehlt",
        "string_too_short": "ist leer oder zu kurz",
        "string_too_long": "ist zu lang",
        "greater_than": "muss größer sein",
        "greater_than_equal": "ist zu klein",
        "less_than_equal": "ist zu groß",
        "literal_error": "hat einen unbekannten Wert",
        "url_parsing": "ist keine gültige URL",
    }
    details: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "Antwort"
        kind = str(error.get("type", "ungültig"))
        description = descriptions.get(kind, "ist ungültig")
        detail = f"{location} {description}"
        if detail not in details:
            details.append(detail)
        if len(details) == 5:
            break
    joined = "; ".join(details) or "ein Pflichtfeld ist ungültig"
    logger.warning("KI-Ausgabe verletzt das Extraktionsschema: %s", joined)
    return f"{failure_message}: {joined}."


def _data_url(content: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


def _image_input(content: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    return {
        "type": "input_image",
        "image_url": _data_url(content, mime_type),
        "detail": "high",
    }


def _request_structured_attempt[SchemaModel: BaseModel](
    *,
    model_type: type[SchemaModel],
    request_payload: dict[str, Any],
    settings: Settings,
    failure_message: str,
    repair: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> SchemaModel:
    last_error: Exception | None = None
    with httpx.Client(timeout=settings.ai_timeout_seconds) as client:
        for transport_attempt in range(settings.ai_max_retries + 1):
            try:
                response = client.post(
                    f"{settings.ai_base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {settings.ai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {408, 409, 429} and not (
                    500 <= exc.response.status_code < 600
                ):
                    detail = _provider_error_detail(exc.response)
                    logger.warning(
                        "KI-Anfrage wurde mit HTTP %s abgelehnt: %s",
                        exc.response.status_code,
                        detail or "keine Fehlerdetails",
                    )
                    message = "Der KI-Dienst hat die Extraktionsanfrage abgelehnt"
                    if detail:
                        message = f"{message}: {detail}"
                    raise AIExtractionError(message) from exc
                last_error = exc
                if transport_attempt < settings.ai_max_retries:
                    time.sleep(min(2**transport_attempt, 8))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                if transport_attempt < settings.ai_max_retries:
                    time.sleep(min(2**transport_attempt, 8))
                continue

            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Die KI-Antwort ist kein Objekt")
                raw = json.loads(_extract_output_text(payload))
                if not isinstance(raw, dict):
                    raise ValueError("Die strukturierte Antwort ist kein Objekt")
                return model_type.model_validate(repair(raw) if repair else raw)
            except _RetryableExtractionError:
                raise
            except ValidationError as exc:
                raise _RetryableExtractionError(
                    _validation_error_message(exc, failure_message)
                ) from exc
            except json.JSONDecodeError as exc:
                raise _RetryableExtractionError(
                    f"{failure_message}: Die KI-Antwort war kein gültiges JSON."
                ) from exc
            except (TypeError, ValueError) as exc:
                raise _RetryableExtractionError(
                    f"{failure_message}: Die KI-Antwort hatte kein gültiges Objektformat."
                ) from exc
    raise _RetryableExtractionError(failure_message) from last_error


def _request_structured[SchemaModel: BaseModel](
    *,
    model_type: type[SchemaModel],
    schema_name: str,
    instructions: str,
    input_content: list[dict[str, Any]],
    settings: Settings,
    extraction_attempts: int = 1,
    failure_message: str = "Die Rezeptdaten konnten nicht zuverlässig verarbeitet werden",
    repair: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> SchemaModel:
    request_payload = {
        "model": settings.ai_extraction_model,
        "store": False,
        "reasoning": {"effort": settings.ai_extraction_reasoning_effort},
        "instructions": instructions,
        "input": [{"role": "user", "content": input_content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": strict_response_schema(model_type),
            }
        },
    }
    last_error: _RetryableExtractionError | None = None
    for extraction_attempt in range(extraction_attempts):
        try:
            return _request_structured_attempt(
                model_type=model_type,
                request_payload=request_payload,
                settings=settings,
                failure_message=failure_message,
                repair=repair,
            )
        except _RetryableExtractionError as exc:
            last_error = exc
            if extraction_attempt + 1 < extraction_attempts:
                logger.warning(
                    "Strukturierte KI-Ausgabe unzuverlässig; Extraktion wird einmal wiederholt: %s",
                    exc,
                )
    raise AIExtractionError(str(last_error) if last_error else failure_message) from last_error


def _configured_settings(settings: Settings | None) -> Settings:
    configured = settings or get_settings()
    if not configured.ai_api_key:
        raise AIUnavailable(
            "Die KI-Extraktion ist noch nicht konfiguriert. Hinterlege AI_API_KEY und ein kompatibles Modell."
        )
    return configured


def detect_recipes(
    *,
    content: bytes,
    mime_type: str,
    target_language: Locale = DEFAULT_LOCALE,
    settings: Settings | None = None,
) -> DetectedRecipeDocument:
    settings = _configured_settings(settings)
    input_content: list[dict[str, Any]] = [{"type": "input_text", "text": detection_prompt()}]
    if mime_type == "application/pdf":
        input_content.append(
            {
                "type": "input_file",
                "filename": "rezeptquelle.pdf",
                "file_data": _data_url(content, mime_type),
            }
        )
    else:
        input_content.append(_image_input(content, mime_type))
    return _request_structured(
        model_type=DetectedRecipeDocument,
        schema_name="detected_recipe_document",
        instructions=detection_system_prompt(target_language),
        input_content=input_content,
        settings=settings,
        extraction_attempts=MAX_STRUCTURED_EXTRACTION_ATTEMPTS,
        failure_message="Die Rezepte konnten nicht zuverlässig erkannt werden",
    )


def extract_recipe(
    *,
    content: bytes | None = None,
    mime_type: str | None = None,
    images: list[bytes] | None = None,
    existing_category_paths: list[str],
    title_hint: str | None = None,
    target_language: Locale = DEFAULT_LOCALE,
    settings: Settings | None = None,
) -> ExtractedRecipe:
    settings = _configured_settings(settings)
    input_content: list[dict[str, Any]] = [
        {"type": "input_text", "text": extraction_prompt(existing_category_paths)}
    ]
    if images:
        input_content.extend(_image_input(image) for image in images)
    elif content is not None and mime_type == "application/pdf":
        input_content.append(
            {
                "type": "input_file",
                "filename": "rezept.pdf",
                "file_data": _data_url(content, mime_type),
            }
        )
    elif content is not None and mime_type is not None:
        input_content.append(_image_input(content, mime_type))
    else:
        raise ValueError("Für die Extraktion fehlt das Quellmaterial")
    draft = _request_structured(
        model_type=ExtractedRecipeDraft,
        schema_name="extracted_recipe",
        instructions=extraction_system_prompt(target_language),
        input_content=input_content,
        settings=settings,
        extraction_attempts=MAX_STRUCTURED_EXTRACTION_ATTEMPTS,
        failure_message="Die Rezeptdaten konnten nicht zuverlässig extrahiert werden",
        repair=lambda raw: repair_extracted_recipe_payload(
            raw,
            title_hint=title_hint,
            target_language=target_language,
        ),
    )
    extracted = finalize_extracted_recipe(draft, target_language=target_language)
    return normalize_recipe_units(extracted, target_language=target_language)


def verify_recipe_image(
    *,
    extracted: ExtractedRecipe,
    image: bytes,
    target_language: Locale = DEFAULT_LOCALE,
    settings: Settings | None = None,
) -> RecipeImageMatch:
    settings = _configured_settings(settings)
    ingredient_names = [
        ingredient.name for group in extracted.ingredient_groups for ingredient in group.ingredients
    ]
    return _request_structured(
        model_type=RecipeImageMatch,
        schema_name="recipe_image_match",
        instructions=image_match_system_prompt(target_language),
        input_content=[
            {
                "type": "input_text",
                "text": image_match_prompt(
                    title=extracted.title,
                    description=extracted.description,
                    ingredients=ingredient_names,
                ),
            },
            _image_input(image),
        ],
        settings=settings,
        failure_message="Das Rezeptbild konnte nicht zuverlässig geprüft werden",
    )
