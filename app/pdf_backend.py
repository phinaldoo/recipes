from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path
from typing import Never, cast

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image

MAX_RENDER_PIXELS = 16_000_000
MAX_RENDER_DIMENSION = 8192


class PDFError(ValueError):
    pass


class PasswordProtectedPDF(PDFError):
    pass


class PDFPageOutOfRange(PDFError):
    pass


@dataclass(frozen=True)
class PDFInfo:
    page_count: int


def _raise_pdf_error(exc: Exception) -> Never:
    if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
        raise PasswordProtectedPDF("The PDF is password protected") from exc
    raise PDFError("The PDF could not be read") from exc


def inspect_pdf(source: bytes | Path) -> PDFInfo:
    """Validate a PDF and return the metadata needed by the application."""

    pdf_source = str(source) if isinstance(source, Path) else source
    try:
        with pdfium.PdfDocument(pdf_source) as document:
            return PDFInfo(page_count=int(len(document)))
    except pdfium.PdfiumError as exc:
        _raise_pdf_error(exc)


def render_pdf_page(content: bytes, page_number: int, *, scale: float = 2.5) -> Image.Image:
    """Render a one-based PDF page to an independent RGB Pillow image."""

    if page_number < 1:
        raise PDFPageOutOfRange("PDF page numbers start at one")
    if not isfinite(scale) or scale <= 0:
        raise ValueError("scale must be finite and positive")

    try:
        with pdfium.PdfDocument(content) as document:
            if page_number > int(len(document)):
                raise PDFPageOutOfRange("The PDF page is out of range")

            page = document[page_number - 1]
            try:
                dimensions = [dimension * scale for dimension in page.get_size()]
                if any(
                    not isfinite(dimension) or dimension <= 0 or dimension > MAX_RENDER_DIMENSION
                    for dimension in dimensions
                ):
                    raise PDFError("The PDF page exceeds the rendering dimensions")
                width, height = (ceil(dimension) for dimension in dimensions)
                if width * height > MAX_RENDER_PIXELS:
                    raise PDFError("The PDF page exceeds the rendering pixel budget")
                bitmap = page.render(scale=scale)
                try:
                    image = cast(Image.Image, bitmap.to_pil())
                    return image.convert("RGB").copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
    except PDFPageOutOfRange:
        raise
    except pdfium.PdfiumError as exc:
        _raise_pdf_error(exc)
