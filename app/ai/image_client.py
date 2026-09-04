from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.imports.source_media import SourceRegionError, crop_source_region, source_page_count
from app.schemas.ai import ExtractedRecipe, RecipeSourceRegion


class AIImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedRecipeImage:
    data: bytes
    revised_prompt: str | None = None


def _decode_image(payload: dict[str, Any]) -> GeneratedRecipeImage:
    item = payload["data"][0]
    if not isinstance(item, dict):
        raise ValueError("Ungültige Bildantwort")
    image = base64.b64decode(item["b64_json"], validate=True)
    if not image or len(image) > 50 * 1024 * 1024:
        raise ValueError("Unzulässige Bildgröße")
    revised_prompt = item.get("revised_prompt")
    if not isinstance(revised_prompt, str):
        revised_prompt = None
    elif len(revised_prompt) > 20_000:
        revised_prompt = revised_prompt[:20_000]
    return GeneratedRecipeImage(data=image, revised_prompt=revised_prompt)


def _request_image(
    endpoint: str,
    *,
    settings: Settings,
    request_kwargs: dict[str, Any],
) -> GeneratedRecipeImage:
    last_error: Exception | None = None
    for attempt in range(settings.ai_max_retries + 1):
        try:
            response = httpx.post(
                f"{settings.ai_base_url}/{endpoint}",
                headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                timeout=settings.ai_timeout_seconds,
                **request_kwargs,
            )
            response.raise_for_status()
            return _decode_image(response.json())
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {408, 409, 429} and not (
                500 <= exc.response.status_code < 600
            ):
                raise AIImageError("Der Bilddienst hat die Anfrage abgelehnt") from exc
            last_error = exc
        except (httpx.RequestError, IndexError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
        if attempt < settings.ai_max_retries:
            time.sleep(min(2**attempt, 8))
    raise AIImageError("Der Bilddienst hat keine verwendbare Bilddatei geliefert") from last_error


def _image_reference(
    extracted: ExtractedRecipe, reference: bytes, reference_mime: str
) -> tuple[bytes, str]:
    candidate = max(
        extracted.recipe_image_candidates,
        key=lambda item: item.confidence,
        default=None,
    )
    if candidate is None and reference_mime != "application/pdf":
        return reference, reference_mime
    try:
        page = candidate.page if candidate else 1
        if reference_mime == "application/pdf":
            pages = source_page_count(reference, reference_mime)
            if pages == 0:
                raise AIImageError("Das PDF enthält keine Seite für die Bildaufbereitung")
            page = min(page, pages)
        region = (
            RecipeSourceRegion(page=page, bounding_box=candidate.bounding_box)
            if candidate
            else RecipeSourceRegion(page=page)
        )
        return crop_source_region(reference, reference_mime, region), "image/png"
    except AIImageError:
        raise
    except (SourceRegionError, ValueError) as exc:
        raise AIImageError("Das zugeordnete Rezeptbild konnte nicht vorbereitet werden") from exc


def maybe_generate_recipe_image(
    extracted: ExtractedRecipe,
    reference: bytes,
    reference_mime: str,
    *,
    settings: Settings | None = None,
    reference_is_cropped: bool = False,
) -> bytes | None:
    settings = settings or get_settings()
    if not extracted.has_recipe_image:
        return None
    if not settings.ai_image_generation_enabled:
        return None
    if not settings.ai_api_key:
        raise AIImageError("Die Bildaufbereitung ist nicht konfiguriert")
    if not reference_is_cropped:
        reference, reference_mime = _image_reference(extracted, reference, reference_mime)
    prompt = (
        f"Erzeuge ein hochwertiges, natürliches Rezeptbild für ‚{extracted.title}‘ auf Basis "
        "des verifizierten Referenzausschnitts. "
        "Erhalte Gericht, Zutaten, Anrichteweise und Bildstimmung; keine Schrift, "
        "keine Logos, keine zusätzlichen Speisen."
    )
    return _request_image(
        "images/edits",
        settings=settings,
        request_kwargs={
            "data": {
                "model": settings.ai_image_model,
                "prompt": prompt,
                "quality": settings.ai_image_quality,
                "size": "1536x1024",
            },
            "files": {"image": ("reference", reference, reference_mime)},
        },
    ).data


def generate_recipe_image(
    prompt: str,
    *,
    settings: Settings | None = None,
) -> GeneratedRecipeImage:
    settings = settings or get_settings()
    if not settings.ai_image_generation_enabled or not settings.ai_api_key:
        raise AIImageError("Die Bildgenerierung ist nicht konfiguriert")
    prompt = prompt.strip()
    if not prompt or len(prompt) > 20_000:
        raise AIImageError("Die Rezeptbeschreibung für das Bild ist ungültig")
    return _request_image(
        "images/generations",
        settings=settings,
        request_kwargs={
            "json": {
                "model": settings.ai_image_model,
                "prompt": prompt,
                "quality": settings.ai_image_quality,
                "size": "1536x1024",
            }
        },
    )


def edit_recipe_image(
    prompt: str,
    reference: bytes,
    reference_mime: str,
    *,
    settings: Settings | None = None,
) -> GeneratedRecipeImage:
    settings = settings or get_settings()
    if not settings.ai_image_generation_enabled or not settings.ai_api_key:
        raise AIImageError("Die Bildgenerierung ist nicht konfiguriert")
    prompt = prompt.strip()
    if not prompt or len(prompt) > 20_000:
        raise AIImageError("Die Rezeptbeschreibung für das Bild ist ungültig")
    if (
        not reference
        or len(reference) > 50 * 1024 * 1024
        or not reference_mime.startswith("image/")
    ):
        raise AIImageError("Das aktuelle Rezeptbild ist keine gültige Bildreferenz")
    return _request_image(
        "images/edits",
        settings=settings,
        request_kwargs={
            "data": {
                "model": settings.ai_image_model,
                "prompt": prompt,
                "quality": settings.ai_image_quality,
                "size": "1536x1024",
            },
            "files": [
                (
                    "image[]",
                    ("current-recipe-image", reference, reference_mime),
                )
            ],
        },
    )
