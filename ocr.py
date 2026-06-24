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
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout

log = logging.getLogger("ocr")

# 200 DPI + fast LSTM models keep accuracy high enough for these statements
# while running several times faster than 300 DPI (OCR cost ~ DPI^2).
OCR_DPI = int(os.getenv("LATINEX_OCR_DPI", "200"))
OCR_LANG = os.getenv("LATINEX_OCR_LANG", "spa+eng")
OCR_MAX_PAGES = int(os.getenv("LATINEX_OCR_MAX_PAGES", "32"))
OCR_CONFIG = os.getenv("LATINEX_OCR_CONFIG", "--oem 1")  # LSTM engine only (faster)
OCR_TIMEOUT = int(os.getenv("LATINEX_OCR_TIMEOUT", "600"))  # seconds per PDF

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


def _ocr_render_worker(pdf_bytes, max_pages, dpi, lang, tess_cmd):
    """Runs in a CHILD process: render pages and OCR them. Isolated so a fatal
    MuPDF error / OOM / segfault on a malformed PDF kills only this child, not
    the build. Returns {page_index: text}."""
    import io as _io
    import fitz
    import pytesseract
    from PIL import Image
    if tess_cmd:
        pytesseract.pytesseract.tesseract_cmd = tess_cmd
    lang = _pick_lang(pytesseract, lang)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    texts = {}
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        limit = min(max_pages, doc.page_count, OCR_MAX_PAGES)
        for i in range(limit):
            try:
                pix = doc.load_page(i).get_pixmap(matrix=matrix, alpha=False)
                img = Image.open(_io.BytesIO(pix.tobytes("png")))
                texts[i] = pytesseract.image_to_string(img, lang=lang, config=OCR_CONFIG) or ""
            except Exception:
                texts[i] = ""
    finally:
        doc.close()
    return texts


def ocr_pdf_pages(pdf_bytes, max_pages=45, dpi=OCR_DPI, lang=OCR_LANG):
    """OCR a PDF to {page_index: text}.

    When LATINEX_OCR_SUBPROCESS=1 (set by the offline builder), rendering+OCR
    runs in an isolated child process with a timeout, so a fatal MuPDF error /
    OOM / hang on a malformed PDF kills only the child and yields {} instead of
    taking down the whole build. Otherwise it runs in-process (used by the app,
    which avoids spawning children that would re-import the Streamlit script).
    Raises OCRUnavailable only if deps/binary are missing.
    """
    _ensure()
    tess_cmd = _resolve_tesseract_cmd()
    if os.getenv("LATINEX_OCR_SUBPROCESS") == "1":
        try:
            with ProcessPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_ocr_render_worker, pdf_bytes, max_pages, dpi, lang, tess_cmd)
                return fut.result(timeout=OCR_TIMEOUT)
        except _FutureTimeout:
            log.warning("OCR timed out after %ss; skipping", OCR_TIMEOUT)
            return {}
        except Exception as e:  # BrokenProcessPool on a child crash, etc.
            log.warning("OCR child process failed (%s); skipping", e)
            return {}
    try:
        return _ocr_render_worker(pdf_bytes, max_pages, dpi, lang, tess_cmd)
    except Exception as e:
        log.warning("OCR failed (%s); skipping", e)
        return {}


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
