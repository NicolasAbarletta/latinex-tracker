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


_PROMPT_HEAD = """These are page images from a Panamanian company's consolidated financial \
report (in Spanish). Transcribe THREE statements (any that are visible):
- Estado de Resultados (income statement)
- Estado de Situacion / Posicion Financiera (balance sheet)
- Estado de Flujos de Efectivo (cash-flow statement)

{period_instr}

Return ONLY a JSON object (no prose, no code fences) shaped exactly:
{{
  "income": [{{"label": "<exact Spanish label as printed>", "value": <number>}}, ...],
  "balance": [{{"label": "<exact Spanish label as printed>", "value": <number>}}, ...],
  "cashflow": [{{"label": "<exact Spanish label as printed>", "value": <number>}}, ...]
}}

For the cash-flow statement, make sure to include the section NET totals (efectivo neto de/\
usado en actividades de operacion / inversion / financiamiento), plus capital-expenditure \
lines (adquisicion de inmuebles, mobiliario y equipo / propiedades) and "dividendos pagados" \
if shown.

CRITICAL rules:
- Return EVERY value in FULL CURRENCY UNITS. If a statement is presented "en miles" / \
"in thousands", multiply each of its numbers by 1,000; if "en millones", by 1,000,000. \
The income statement and balance sheet MAY USE DIFFERENT SCALES in the same report \
(e.g. income "en miles", balance in full balboas) -- apply each statement's own scale so \
both end up in full units.
- Ignore any "Nota" reference column and all prior-year comparative columns.
- Plain numbers only (no commas/currency symbols); negatives as negative numbers.
- Include all line items, especially the totals: total de ingresos / total de ingresos por \
intereses, utilidad neta, utilidad neta atribuible a la controladora (o a los propietarios); \
total de activos, total de pasivos, total de patrimonio.
- If a statement is not visible in these images, return it as an empty list."""

_PERIOD_QUARTERLY = ("This is an interim report. Use the CURRENT 3-month period column "
                     "(the most recent quarter shown).")
_PERIOD_ANNUAL = (
    "This is a YEAR-END (fourth-quarter) report; you must extract FULL-YEAR (12-month) "
    "figures for the most recent year.\n"
    "IMPORTANT: the income statement frequently shows, for the SAME year, TWO columns: a "
    "3-month quarter column (headed 'IV Trimestre' / 'Por los tres meses terminados el 31 "
    "de diciembre') AND a 12-month full-year column (headed 'Acumulado' / 'Por el ano "
    "terminado el 31 de diciembre'). Column order is often: Nota, 2025-quarter, 2024-quarter, "
    "2025-accumulated, 2024-accumulated. You MUST use the 2025 FULL-YEAR ACCUMULATED column "
    "(the 12-month one) -- its 'utilidad neta' is roughly 4x a single quarter (for a large "
    "bank, hundreds of millions, not tens of millions). NEVER use the 3-month quarter column. "
    "The balance sheet has a single date column -- use the most recent date.")


def _build_prompt(is_quarterly):
    return _PROMPT_HEAD.format(
        period_instr=_PERIOD_QUARTERLY if is_quarterly else _PERIOD_ANNUAL)


def extract_statements(pdf_bytes, pages, report_name="", pdf_url="",
                       is_quarterly=True, period_hint="", dpi=170, api_key=None):
    """Render `pages` and have Claude vision transcribe the statements.

    Returns a dict shaped like financials.get_financials() (income/balance
    DataFrames, scale, periods, error, vision_used=True)."""
    import anthropic

    result = {"income": pd.DataFrame(), "balance": pd.DataFrame(),
              "cashflow": pd.DataFrame(), "periods": [],
              "scale_label": "", "scale_factor": None, "is_quarterly": is_quarterly,
              "ocr_used": False, "vision_used": True, "pdf_url": pdf_url,
              "report_name": report_name, "report_date": "", "error": None}

    api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        result["error"] = "ANTHROPIC_API_KEY not set for vision extraction"
        return result

    content = [{"type": "text", "text": _build_prompt(is_quarterly)}]
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

    # Vision already returns full currency units (per-statement scale applied),
    # so the downstream factor is 1.
    period = period_hint or "current"
    result["scale_label"] = "full units (vision)"
    result["scale_factor"] = 1
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
    result["cashflow"] = _df(data.get("cashflow"))
    if result["income"].empty and result["balance"].empty:
        result["error"] = "vision returned no usable line items"
    return result
