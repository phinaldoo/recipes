from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, pass_context, select_autoescape
from playwright.async_api import async_playwright

from app.config import Settings, get_settings
from app.i18n import DEFAULT_LOCALE, Locale, locale_context, normalize_locale
from app.models import Recipe
from app.services.scaling import format_amount, format_decimal, format_duration, scale_amount
from app.services.storage import resolve_storage_key

templates = Environment(
    loader=FileSystemLoader(Path(__file__).parents[1] / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def _context_locale(context: object) -> Locale:
    return normalize_locale(cast(Any, context).get("locale")) or DEFAULT_LOCALE


@pass_context
def _format_amount(context: object, value: object) -> str:
    return format_amount(cast(Any, value), _context_locale(context))


@pass_context
def _format_decimal(context: object, value: object) -> str:
    return format_decimal(cast(Any, value), _context_locale(context))


@pass_context
def _format_duration(context: object, value: object) -> str:
    return format_duration(cast(Any, value), _context_locale(context))


templates.filters["format_amount"] = _format_amount
templates.filters["format_decimal"] = _format_decimal
templates.filters["duration"] = _format_duration
templates.globals.update(locale_context(DEFAULT_LOCALE))
pdf_render_slots = asyncio.Semaphore(2)
json_export_slots = threading.BoundedSemaphore(get_settings().recipe_json_export_concurrency)


class RecipeExportTooLarge(ValueError):
    pass


class RecipeExportBusy(RuntimeError):
    pass


@contextmanager
def json_export_slot() -> Iterator[None]:
    if not json_export_slots.acquire(blocking=False):
        raise RecipeExportBusy("Es laufen bereits zu viele Rezept-Exporte.")
    try:
        yield
    finally:
        json_export_slots.release()


def _text_size(recipe: Recipe) -> int:
    values: list[str | None] = [
        recipe.title,
        recipe.description,
        recipe.serving_label,
        recipe.notes,
        recipe.created_by_name_snapshot,
        recipe.updated_by_name_snapshot,
        recipe.created_by.visible_name if recipe.created_by else None,
        recipe.updated_by.visible_name if recipe.updated_by else None,
    ]
    if recipe.source:
        values.extend([recipe.source.title, recipe.source.url])
    values.extend(value.note for value in getattr(recipe, "nutrition", []))
    for group in recipe.ingredient_groups:
        values.append(group.title)
        for item in group.ingredients:
            values.extend([item.unit, item.name, item.note])
    values.extend(step.text for step in recipe.instruction_steps)
    values.extend(category.path for category in recipe.categories)
    values.extend(tag.name for tag in recipe.tags)
    for comment in recipe.comments:
        if comment.deleted_at is None:
            values.extend([comment.author_name_snapshot, comment.text])
    return sum(len(value.encode("utf-8")) for value in values if value)


def _asset_metadata_size(links: list[Any]) -> int:
    size = 0
    for link in links:
        asset = link.asset
        values = [
            asset.original_filename,
            asset.mime_type,
            asset.sha256,
            asset.kind,
            getattr(link, "caption", None),
            getattr(link, "alt_text", None),
        ]
        size += sum(len(value.encode("utf-8")) for value in values if value)
        metadata = getattr(link, "generation_metadata", None)
        if metadata is not None:
            size += len(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
    return size


def _preflight_export_assets(
    recipe: Recipe,
    *,
    include_originals: bool,
    settings: Settings,
) -> dict[str, Path]:
    links: list[Any] = list(recipe.images)
    if include_originals:
        links.extend(recipe.original_assets)
    if len(links) > settings.recipe_json_export_max_assets:
        raise RecipeExportTooLarge(
            f"Das Rezept enthält mehr als {settings.recipe_json_export_max_assets} exportierbare Dateien."
        )
    paths: dict[str, Path] = {}
    encoded_bytes = 0
    for link in links:
        asset = link.asset
        path = resolve_storage_key(asset.storage_key)
        actual_size = path.stat().st_size
        size = max(int(asset.byte_size), actual_size)
        encoded_bytes += 4 * ((size + 2) // 3)
        paths[asset.storage_key] = path
    # JSON escaping and Python's transient Unicode representation add overhead.
    # Keep a conservative floor plus twice the measured text and per-asset metadata.
    unescaped_metadata_bytes = _text_size(recipe) + _asset_metadata_size(links)
    structural_bytes = max(
        256 * 1024,
        unescaped_metadata_bytes * 6 + len(links) * 4096,
    )
    if encoded_bytes + structural_bytes > settings.recipe_json_export_max_bytes:
        raise RecipeExportTooLarge(
            f"Das Rezept überschreitet das Exportlimit von {settings.recipe_json_export_max_mb} MB."
        )
    return paths


def recipe_package_dict(
    recipe: Recipe,
    *,
    include_originals: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    asset_paths = _preflight_export_assets(
        recipe,
        include_originals=include_originals,
        settings=settings,
    )

    def encoded_asset(link: Any, *, image: bool) -> dict[str, Any]:
        asset = link.asset
        path = asset_paths[asset.storage_key]
        data = path.read_bytes()
        return {
            "filename": asset.original_filename,
            "mime_type": asset.mime_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(data).decode("ascii"),
            "kind": asset.kind,
            "caption": link.caption if image else None,
            "alt_text": link.alt_text if image else None,
            "is_cover": link.is_cover if image else False,
            "generation_metadata": link.generation_metadata if image else None,
        }

    result: dict[str, Any] = {
        "schema_version": "1.3",
        "recipe": {
            "title": recipe.title,
            "description": recipe.description,
            "recipe_kind": getattr(recipe, "recipe_kind", "cooking"),
            "base_servings": str(recipe.base_servings),
            "serving_label": recipe.serving_label,
            "prep_time_minutes": recipe.prep_time_minutes,
            "cook_time_minutes": recipe.cook_time_minutes,
            "rest_time_minutes": recipe.rest_time_minutes,
            "total_time_minutes": recipe.total_time_minutes,
            "total_time_is_manual": recipe.total_time_is_manual,
            "nutrition": [
                {
                    "basis": value.basis,
                    "energy_kj": str(value.energy_kj) if value.energy_kj is not None else None,
                    "energy_kcal": str(value.energy_kcal)
                    if value.energy_kcal is not None
                    else None,
                    "fat_g": str(value.fat_g) if value.fat_g is not None else None,
                    "saturated_fat_g": str(value.saturated_fat_g)
                    if value.saturated_fat_g is not None
                    else None,
                    "carbohydrates_g": str(value.carbohydrates_g)
                    if value.carbohydrates_g is not None
                    else None,
                    "sugars_g": str(value.sugars_g) if value.sugars_g is not None else None,
                    "fiber_g": str(value.fiber_g) if value.fiber_g is not None else None,
                    "protein_g": str(value.protein_g) if value.protein_g is not None else None,
                    "salt_g": str(value.salt_g) if value.salt_g is not None else None,
                    "note": value.note,
                }
                for value in getattr(recipe, "nutrition", [])
            ],
            "notes": recipe.notes,
            "status": recipe.status,
            "ingredient_groups": [
                {
                    "title": group.title,
                    "ingredients": [
                        {
                            "amount_min": str(item.amount_min)
                            if item.amount_min is not None
                            else None,
                            "amount_max": str(item.amount_max)
                            if item.amount_max is not None
                            else None,
                            "unit": item.unit,
                            "name": item.name,
                            "note": item.note,
                            "is_scalable": item.is_scalable,
                        }
                        for item in group.ingredients
                    ],
                }
                for group in recipe.ingredient_groups
            ],
            "instruction_steps": [{"text": step.text} for step in recipe.instruction_steps],
            "categories": [
                {"id": None, "path": category.path.split(" › "), "origin": category.origin}
                for category in recipe.categories
            ],
            "tags": [tag.name for tag in recipe.tags],
            "source": (
                {"title": recipe.source.title, "url": recipe.source.url} if recipe.source else None
            ),
            "comments": [
                {
                    "author_name": comment.author_name_snapshot,
                    "author_email": None,
                    "text": comment.text,
                    "created_at": comment.created_at.isoformat(),
                    "updated_at": comment.updated_at.isoformat() if comment.updated_at else None,
                }
                for comment in recipe.comments
                if comment.deleted_at is None
            ],
            "images": [encoded_asset(image, image=True) for image in recipe.images],
            "original_assets": (
                [encoded_asset(asset, image=False) for asset in recipe.original_assets]
                if include_originals
                else []
            ),
            "created_at": recipe.created_at.isoformat(),
            "updated_at": recipe.updated_at.isoformat(),
            "created_by_name": recipe.created_by_name_snapshot
            or (recipe.created_by.visible_name if recipe.created_by else None),
            "updated_by_name": recipe.updated_by_name_snapshot
            or (recipe.updated_by.visible_name if recipe.updated_by else None),
        },
    }
    return result


def scaled_recipe_view(
    recipe: Recipe,
    desired_servings: float,
    locale: Locale = DEFAULT_LOCALE,
) -> dict[str, Any]:
    from decimal import Decimal

    desired = Decimal(str(desired_servings))
    groups = []
    for group in recipe.ingredient_groups:
        items = []
        for ingredient in group.ingredients:
            items.append(
                {
                    "amount_min": format_amount(
                        scale_amount(
                            ingredient.amount_min,
                            base_servings=recipe.base_servings,
                            desired_servings=desired,
                            scalable=ingredient.is_scalable,
                        ),
                        locale,
                    ),
                    "amount_max": format_amount(
                        scale_amount(
                            ingredient.amount_max,
                            base_servings=recipe.base_servings,
                            desired_servings=desired,
                            scalable=ingredient.is_scalable,
                        ),
                        locale,
                    ),
                    "unit": ingredient.unit,
                    "name": ingredient.name,
                    "note": ingredient.note,
                }
            )
        groups.append({"title": group.title, "ingredients": items})
    return {
        "recipe": recipe,
        "desired_servings": desired,
        "groups": groups,
        "print_images": printable_recipe_images(recipe),
    }


def printable_recipe_images(
    recipe: Recipe,
    *,
    embed: bool = False,
) -> list[dict[str, Any]]:
    """Build cover-first image data for browser printing or self-contained PDFs."""

    cover = recipe.cover_image
    ordered_images = list(recipe.images)
    if cover is not None:
        ordered_images = [cover, *[image for image in ordered_images if image.id != cover.id]]

    result: list[dict[str, Any]] = []
    for image in ordered_images:
        # The normalized JPEG thumbnail is compact and remains printable even
        # when the uploaded original uses a browser-specific format such as HEIC.
        asset = image.thumbnail_asset or image.asset
        if embed:
            encoded = base64.b64encode(resolve_storage_key(asset.storage_key).read_bytes()).decode(
                "ascii"
            )
            source = f"data:{asset.mime_type};base64,{encoded}"
        else:
            source = f"/api/v1/assets/{asset.id}/view"
        result.append(
            {
                "src": source,
                "alt_text": image.alt_text or recipe.title,
                "caption": image.caption,
                "is_cover": cover is not None and image.id == cover.id,
            }
        )
    return result


async def render_recipe_pdf(
    recipe: Recipe,
    *,
    desired_servings: float,
    include_comments: bool,
    locale: Locale = DEFAULT_LOCALE,
) -> bytes:
    view = scaled_recipe_view(recipe, desired_servings, locale)
    view["print_images"] = printable_recipe_images(recipe, embed=True)
    html = templates.get_template("recipes/print.html").render(
        **view,
        **locale_context(locale),
        include_comments=include_comments,
        pdf_mode=True,
        print_css=(Path(__file__).parents[1] / "static" / "css" / "print.css").read_text(
            encoding="utf-8"
        ),
    )
    async with pdf_render_slots, async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            return await page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "14mm",
                    "right": "12mm",
                    "bottom": "16mm",
                    "left": "12mm",
                },
            )
        finally:
            await browser.close()
