from __future__ import annotations

from io import BytesIO

import pypdfium2 as pdfium
import pytest

from app import pdf_backend
from app.pdf_backend import PasswordProtectedPDF, PDFError, PDFPageOutOfRange


def blank_pdf(*page_sizes: tuple[int, int]) -> bytes:
    output = BytesIO()
    with pdfium.PdfDocument.new() as document:
        for width, height in page_sizes:
            page = document.new_page(width=width, height=height)
            page.close()
        document.save(output)
    return output.getvalue()


def test_inspect_and_render_pdf() -> None:
    content = blank_pdf((40, 40), (100, 50))

    assert pdf_backend.inspect_pdf(content).page_count == 2
    image = pdf_backend.render_pdf_page(content, 2)
    try:
        assert image.mode == "RGB"
        assert image.size == (250, 125)
    finally:
        image.close()


def test_pdf_backend_rejects_invalid_content_and_page_numbers() -> None:
    with pytest.raises(PDFError):
        pdf_backend.inspect_pdf(b"not a PDF")

    content = blank_pdf((40, 40))
    with pytest.raises(PDFPageOutOfRange):
        pdf_backend.render_pdf_page(content, 0)
    with pytest.raises(PDFPageOutOfRange):
        pdf_backend.render_pdf_page(content, 2)


def test_inspect_pdf_distinguishes_password_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_locked_pdf(_source: bytes) -> None:
        raise pdfium.PdfiumError("locked", pdfium.raw.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(pdf_backend.pdfium, "PdfDocument", reject_locked_pdf)

    with pytest.raises(PasswordProtectedPDF):
        pdf_backend.inspect_pdf(b"%PDF-locked")


@pytest.mark.parametrize("size", [(4000, 4000), (10000, 1), (2000, 2000)])
def test_oversized_pdf_is_rejected_before_native_render(
    monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    content = blank_pdf(size)

    def forbidden_render(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An oversized bitmap must never be allocated")

    monkeypatch.setattr(pdfium.PdfPage, "render", forbidden_render)
    with pytest.raises(PDFError, match="rendering"):
        pdf_backend.render_pdf_page(content, 1)


@pytest.mark.parametrize("scale", [float("nan"), float("inf"), 0, -1])
def test_invalid_render_scale_is_rejected(scale: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        pdf_backend.render_pdf_page(blank_pdf((40, 40)), 1, scale=scale)


@pytest.mark.parametrize("width", [float("nan"), float("inf"), 0, -1])
def test_invalid_pdf_geometry_is_rejected(monkeypatch: pytest.MonkeyPatch, width: float) -> None:
    content = blank_pdf((40, 40))
    monkeypatch.setattr(pdfium.PdfPage, "get_size", lambda _page: (width, 40))
    with pytest.raises(PDFError, match="rendering dimensions"):
        pdf_backend.render_pdf_page(content, 1)
