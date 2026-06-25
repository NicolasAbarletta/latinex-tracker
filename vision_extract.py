# -*- coding: utf-8 -*-
"""
vision_extract.py -- read financial statements from scanned pages with Claude vision.

Tesseract OCR finds the right pages but mangles the numbers in scanned tables.
For issuers whose statements are images (e.g. Grupo Melo), this renders the
statement pages and asks Claude (vision) to transcribe the income statement and
balance sheet as structured line items, returning a dict shaped like
financials.get_financials() so the rest of the pipeline (ratios, DuPont, deep
dive) works unchanged.
"""

import base64
import json
import logging
import os

import pandas as pd

log = logging.getLogger("vision_extract")

MODEL = os.getenv("LATINEX_VISION_MODEL", os.getenv("LATINEX_MODEL", "claude-opus-4-6"))


def _render_png(pdf_bytes, page_index, dpi=170):
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page_index >= doc.page_count:
            return None
        zoom = dpi / 72.0
        pix = doc.load_page(page_index).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


PROMPT = """These are page images from a Panamanian company's consolidated financial \
report (in Spanish). Transcribe TWO statements:
- Estado de Resultados (income statement)
- Estado de Situacion / Posicion Financiera (balance sheet)

Return ONLY a JSON object (no prose, no code fences) shaped exactly:
{
  "scale": "the scale as printed, e.g. 'en balboas', 'en miles de US$', 'en dolares'",
  "period": "the current-period label, e.g. '31 de marzo de 2026'",
  "income": [{"label": "<exact Spanish label as printed>", "value": <number>}, ...],
  "balance": [{"label": "<exact Spanish label as printed>", "value": <number>}, ...]
}

Rules:
- Use the CURRENT period column only (ignore prior-year comparatives and any 'Nota' column).
- Values are plain numbers (no commas/currency); negatives as negative numbers.
- Do NOT apply the scale yourself; report raw printed numbers plus the scale string.
- Include all real line items and especially totals: total de ingresos, costo de ventas, \
utilidad bruta, gastos generales, utilidad neta, utilidad neta atribuible a la controladora; \
total de activos, total de pasivos, total de patrimonio (y patrimonio de la controladora).
- If a statement is not visible in these images, return it as an empty list."""

_SCALE_FACTOR = {"miles": 1000, "millones": 1_000_000}


def _scale_factor(scale_label):
    s = (scale_label or "").lower()
    for k, f in _SCALE_FACTOR.items():
        if k in s:
            return f
    return 1


def extract_statements(pdf_bytes, pages, report_name="", pdf_url="",
                       is_quarterly=True, period_hint="", dpi=170, api_key=None):
    """Render `pages` and have Claude vision transcribe the statements.

    Returns a dict shaped like financials.get_financials() (income/balance
    DataFrames, scale, periods, error, vision_used=True)."""
    import anthropic

    result = {"income": pd.DataFrame(), "balance": pd.DataFrame(), "periods": [],
              "scale_label": "", "scale_factor": None, "is_quarterly": is_quarterly,
              "ocr_used": False, "vision_used": True, "pdf_url": pdf_url,
              "report_name": report_name, "report_date": "", "error": None}

    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        result["error"] = "ANTHROPIC_API_KEY not set for vision extraction"
        return result

    content = [{"type": "text", "text": PROMPT}]
    rendered = 0
    for pg in pages[:8]:
        png = _render_png(pdf_bytes, pg, dpi=dpi)
        if not png:
            continue
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode()}})
        rendered += 1
    if not rendered:
        result["error"] = "no statement pages to render for vision"
        return result

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": content}])
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
    except Exception as e:  # noqa: BLE001
        result["error"] = f"vision extraction failed: {e}"
        return result

    scale_label = data.get("scale", "") or ""
    period = data.get("period") or period_hint or "current"
    result["scale_label"] = f"{scale_label} (vision)" if scale_label else "vision-extracted"
    result["scale_factor"] = _scale_factor(scale_label)
    result["periods"] = [period]

    def _df(rows):
        recs = []
        for r in rows or []:
            label = str(r.get("label", "")).strip()
            val = r.get("value")
            if label and isinstance(val, (int, float)):
                recs.append({"Line Item": label, period: float(val)})
        return pd.DataFrame(recs, columns=["Line Item", period]) if recs else pd.DataFrame()

    result["income"] = _df(data.get("income"))
    result["balance"] = _df(data.get("balance"))
    if result["income"].empty and result["balance"].empty:
        result["error"] = "vision returned no usable line items"
    return result
