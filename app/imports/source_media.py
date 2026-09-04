from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.pdf_backend import PDFError, PDFPageOutOfRange, inspect_pdf, render_pdf_page
from app.schemas.ai import NormalizedBoundingBox, RecipeSourceRegion


class SourceRegionError(ValueError):
    pass


def normalize_image_source(content: bytes) -> bytes:
    """Return the exact upright PNG coordinate space sent to the vision model."""

    try:
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise SourceRegionError("Das Quellbild konnte nicht normalisiert werden") from exc


def source_page_count(content: bytes, mime_type: str) -> int:
    if mime_type != "application/pdf":
        return 1
    try:
        return inspect_pdf(content).page_count
    except PDFError as exc:
        raise SourceRegionError("Die PDF-Quelle konnte nicht gelesen werden") from exc


def _render_page(content: bytes, mime_type: str, page: int) -> Image.Image:
    if page < 1:
        raise SourceRegionError("Eine Quellseite liegt außerhalb des Dokuments")
    if mime_type != "application/pdf":
        if page != 1:
            raise SourceRegionError("Ein Bild besitzt nur eine Seite")
        try:
            with Image.open(BytesIO(content)) as source:
                source.seek(0)
                return ImageOps.exif_transpose(source).convert("RGB")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise SourceRegionError("Das Quellbild konnte nicht gelesen werden") from exc
    try:
        return render_pdf_page(content, page)
    except PDFPageOutOfRange as exc:
        raise SourceRegionError("Eine erkannte Quellseite liegt außerhalb des PDFs") from exc
    except PDFError as exc:
        raise SourceRegionError("Die PDF-Quelle konnte nicht gerendert werden") from exc


def _pixel_box(box: NormalizedBoundingBox, width: int, height: int) -> tuple[int, int, int, int]:
    left = max(0, min(width - 1, math.floor(width * box.left / 1000)))
    top = max(0, min(height - 1, math.floor(height * box.top / 1000)))
    right = max(left + 1, min(width, math.ceil(width * box.right / 1000)))
    bottom = max(top + 1, min(height, math.ceil(height * box.bottom / 1000)))
    return left, top, right, bottom


def crop_source_region(
    content: bytes,
    mime_type: str,
    region: RecipeSourceRegion,
    *,
    max_dimension: int = 3500,
) -> bytes:
    page = _render_page(content, mime_type, region.page)
    try:
        cropped = page.crop(_pixel_box(region.bounding_box, *page.size))
        if cropped.width < 8 or cropped.height < 8:
            raise SourceRegionError("Ein erkannter Quellausschnitt ist zu klein")
        if max(cropped.size) > max_dimension:
            cropped.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = BytesIO()
        cropped.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        page.close()


def bounding_box_iou(first: NormalizedBoundingBox, second: NormalizedBoundingBox) -> float:
    intersection_width = max(0, min(first.right, second.right) - max(first.left, second.left))
    intersection_height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    intersection = intersection_width * intersection_height
    if intersection == 0:
        return 0.0
    first_area = (first.right - first.left) * (first.bottom - first.top)
    second_area = (second.right - second.left) * (second.bottom - second.top)
    return intersection / (first_area + second_area - intersection)


def bounding_box_overlap_ratio(
    first: NormalizedBoundingBox, second: NormalizedBoundingBox
) -> float:
    """Return how much of the smaller box is covered by the intersection."""

    intersection_width = max(0, min(first.right, second.right) - max(first.left, second.left))
    intersection_height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    intersection = intersection_width * intersection_height
    if intersection == 0:
        return 0.0
    first_area = (first.right - first.left) * (first.bottom - first.top)
    second_area = (second.right - second.left) * (second.bottom - second.top)
    return intersection / min(first_area, second_area)
