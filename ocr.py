# -*- coding: utf-8 -*-
"""
ocr.py -- OCR fallback for scanned (image-only) Latinex filings.

Some issuers (TRENCO, Grupo Melo/MHCH, CMBG, CMRealty, ...) publish their
quarterly reports as scanned images with no extractable text layer, so the
pdfplumber text parser in financials.py finds nothing. This module renders
each PDF page to a high-resolution image with PyMuPDF and runs Tesseract OCR
(via pytesseract) to recover a text layer that the existing parser can consume.

The heavy/optional dependencies (pymupdf, pytesseract, pillow) are imported
lazily so the rest of the app keeps running if they are not installed. On
Windows the Tesseract binary must be installed separately; set TESSERACT_CMD
to its full path if it is not on PATH. Install the Spanish language data
('spa') for best results on these statements.
"""

import io
import logging
import os

log = logging.getLogger("ocr")

OCR_DPI = int(os.getenv("LATINEX_OCR_DPI", "300"))
OCR_LANG = os.getenv("LATINEX_OCR_LANG", "spa+eng")

# Standard install locations checked when TESSERACT_CMD is not set, so OCR
# works out of the box on a typical Windows machine.
_DEFAULT_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
]


def _resolve_tesseract_cmd():
    cmd = os.getenv("TESSERACT_CMD", "")
    if cmd and os.path.exists(cmd):
        return cmd
    for path in _DEFAULT_TESSERACT_PATHS:
        if os.path.exists(path):
            return path
    return ""  # fall back to PATH lookup by pytesseract


_checked = False


class OCRUnavailable(Exception):
    """Raised when the OCR dependencies or the Tesseract binary are missing."""


def _ensure():
    """Import deps and confirm the Tesseract binary works. Raises OCRUnavailable."""
    global _checked
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError as e:
        raise OCRUnavailable(
            "OCR needs pymupdf, pytesseract and pillow "
            "(pip install pymupdf pytesseract pillow) plus the Tesseract binary."
        ) from e
    cmd = _resolve_tesseract_cmd()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    if not _checked:
        try:
            pytesseract.get_tesseract_version()
        except Exception as e:  # binary not found / not on PATH
            raise OCRUnavailable(
                "Tesseract binary not found. Install it and/or set the env var "
                "TESSERACT_CMD to its full path, e.g. "
                "C:/Program Files/Tesseract-OCR/tesseract.exe"
            ) from e
        _checked = True


def is_available():
    """True if scanned PDFs can be OCR'd in this environment."""
    try:
        _ensure()
        return True
    except OCRUnavailable:
        return False


def _pick_lang(pytesseract, lang):
    """Drop language codes whose traineddata is missing; fall back to eng."""
    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception:
        return lang
    parts = [p for p in lang.split("+") if p in available]
    return "+".join(parts) or "eng"


def ocr_pdf_pages(pdf_bytes, max_pages=45, dpi=OCR_DPI, lang=OCR_LANG):
    """Render each page to an image and OCR it.

    Returns {page_index: text}, preserving line breaks so the financials
    parser (which works line-by-line) can consume the output directly.
    Raises OCRUnavailable if dependencies or the binary are missing.
    """
    _ensure()
    import fitz
    import pytesseract
    from PIL import Image

    lang = _pick_lang(pytesseract, lang)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    texts = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        limit = min(max_pages, doc.page_count)
        for i in range(limit):
            try:
                pix = doc.load_page(i).get_pixmap(matrix=matrix, alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                texts[i] = pytesseract.image_to_string(img, lang=lang) or ""
            except Exception as e:
                log.warning("OCR failed on page %d: %s", i, e)
                texts[i] = ""
    return texts


if __name__ == "__main__":
    import sys
    print("OCR available:", is_available())
    if len(sys.argv) > 1 and is_available():
        with open(sys.argv[1], "rb") as f:
            pages = ocr_pdf_pages(f.read())
        total = sum(len(t) for t in pages.values())
        print(f"OCR'd {len(pages)} pages, {total} chars")
        for i in sorted(pages)[:3]:
            print(f"\n--- page {i} (first 400 chars) ---\n{pages[i][:400]}")
