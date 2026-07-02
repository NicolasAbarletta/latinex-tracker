# -*- coding: utf-8 -*-
"""
report_engine.py -- Morningstar-style company report generator.

Per company, offline (results are preloaded in the snapshot):
1. Locate the business-description note ("Organizacion y operaciones") and the
   segment-information note in the latest annual filing. Text-layer pages are
   passed as text; scanned filings pass the page IMAGES to Claude vision.
2. Compute valuation anchors in code (EPS/BVPS, own-history median multiples,
   peer medians, ROE, dividends) so fair value is derived from stated methods,
   never invented.
3. One Claude call -> structured report JSON: business description, segments
   with revenue split, competitors (labeled analyst knowledge), moat,
   uncertainty, capital allocation, fair-value range, bulls/bears, thesis.
4. Star rating computed in code from price vs. fair value, widened by the
   uncertainty rating (Morningstar-style).

Usage:
    python report_engine.py MELO            # one company -> part file
    python report_engine.py --all           # every verified company
Then: python merge_parts.py
"""

import argparse
import base64
import io
import json
import os
import sys

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")

import pdfplumber

import latinex_api as api
import financials as fm
import analytics
import analyst
import ocr
import snapshot as snap
from worker_task import save_part

BUSINESS_KEYS = ["organizacion y operaciones", "informacion general",
                 "constitucion y operaciones", "operaciones y actividades"]
SEGMENT_KEYS = ["informacion por segmentos", "informacion de segmentos",
                "segmentos de operacion", "informacion financiera por segmentos"]

MODEL = analyst.MODEL


def _log(m):
    print(m, flush=True)


# ---------------------------------------------------------------------------
# Evidence gathering from the annual filing
# ---------------------------------------------------------------------------

def _annual_doc(e):
    docs = e.get("documents")
    if docs is None or docs.empty:
        docs = api.get_documents((e.get("quote") or {}).get("issuer_code"))
    quarterly = docs[docs["type"] == "Informe Trimestral"]
    q4 = quarterly[quarterly["name"].str.contains("Q4", case=False, na=False)]
    return (q4 if not q4.empty else quarterly).iloc[0]


def gather_evidence(e, max_pages_scan=60, max_evidence_pages=6):
    """Returns (text_evidence, image_pages, pdf_bytes, report_name).
    text_evidence: str of relevant note pages (text-layer filings);
    image_pages: page indices to attach as images (scanned filings)."""
    doc = _annual_doc(e)
    pdf = fm._get_pdf_cached(doc["name"], doc["pdf_url"])
    texts = {}
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for i in range(min(max_pages_scan, len(p.pages))):
            texts[i] = p.pages[i].extract_text() or ""
    scanned = sum(len(t) for t in texts.values()) < 500
    if scanned:
        texts = ocr.ocr_pdf_pages(pdf, max_pages=max_pages_scan)

    hits = []
    for i in sorted(texts):
        n = fm._norm(texts[i])
        if any(k in n for k in BUSINESS_KEYS + SEGMENT_KEYS):
            hits.extend([i, i + 1])          # notes often run onto the next page
    pages = sorted(set(h for h in hits if h in texts))[:max_evidence_pages]

    if not pages:
        return "", [], pdf, doc["name"]
    if scanned:
        return "", pages, pdf, doc["name"]   # attach images; OCR prose too lossy for tables
    text = "\n\n".join(f"--- filing page {i + 1} ---\n{texts[i]}" for i in pages)
    return text[:28000], [], pdf, doc["name"]


def _render_png(pdf_bytes, page_index, dpi=170):
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page_index >= doc.page_count:
            return None
        pix = doc.load_page(page_index).get_pixmap(
            matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        return pix.tobytes("png")


# ---------------------------------------------------------------------------
# Valuation anchors (computed, not model-invented)
# ---------------------------------------------------------------------------

def valuation_anchors(e):
    q, s = e.get("quote") or {}, e.get("summary") or {}
    fin = e.get("financials") or {}
    ht = (e.get("historical") or {}).get("table")
    shares = s.get("shares_outstanding")
    m = fm.extract_metrics(fin)
    r = fm.compute_ratios(fin, q.get("price"), shares)
    quarterly = fin.get("is_quarterly", True)
    vb = analytics.valuation_bands(e.get("history_all"), ht, shares, quarterly)
    dp = analytics.dividend_profile(e.get("dividends"), q.get("price"), ht, shares, quarterly)
    lq = analytics.liquidity_score(e.get("history_all"), q, e.get("order_book_depth"))
    eq = analytics.earnings_quality(fin)
    tr = analytics.total_return(e.get("history_all"), e.get("dividends"))

    ni25 = analytics._fy_value(ht, "Net income", "FY2025")
    eq25 = analytics._fy_value(ht, "Equity (controlling)", "FY2025")
    a = {
        "price": q.get("price"), "market_cap": q.get("market_cap"),
        "shares": shares,
        "eps_fy2025": round(ni25 / shares, 3) if (ni25 and shares) else None,
        "bvps_latest": r.get("bvps"),
        "pe_now": r.get("pe"), "pb_now": r.get("pb"), "roe_pct": r.get("roe_pct"),
        "pe_hist_median": (vb.get("pe") or {}).get("median"),
        "pe_hist_range": [(vb.get("pe") or {}).get("min"), (vb.get("pe") or {}).get("max")],
        "pb_hist_median": (vb.get("pb") or {}).get("median"),
        "ttm_dps": dp.get("ttm_dps"), "ttm_yield_pct": dp.get("ttm_yield_pct"),
        "payout_pct": dp.get("payout_pct"),
        "div_growth_streak_years": dp.get("growth_streak_years"),
        "liquidity_grade": lq.get("grade"), "days_to_exit_100k": lq.get("days_to_exit"),
        "cash_conversion_pct": eq.get("cash_conversion_pct"),
        "div_coverage_x": eq.get("div_coverage_x"),
        "total_return_1y_pct": tr.get("tr_1y_pct"),
        "ni_by_year": {},
    }
    if ht is not None:
        row = ht[ht["Metric"] == "Net income"]
        if not row.empty:
            for c in ht.columns:
                if str(c).startswith("FY"):
                    v = row.iloc[0][c]
                    if v == v:
                        a["ni_by_year"][str(c)] = float(v)
    return a


# ---------------------------------------------------------------------------
# The synthesis call
# ---------------------------------------------------------------------------

REPORT_PROMPT = """You are a senior equity analyst writing a Morningstar-style report on \
{name} ({nemo}), listed on Latinex (Panama). Two evidence blocks follow.

=== VERIFIED METRICS (computed from audited filings & market data -- your ONLY source of numbers) ===
{anchors}

=== FILING EXTRACTS (business-description / segment notes from the latest annual report, in Spanish) ===
{filing_note}

Write the report as ONLY a JSON object (no prose around it, no code fences):
{{
  "business_description": "3-5 sentences in English: what the company actually does, its main operations and where it sits in Panama's economy. Ground it in the filing extracts.",
  "segments": [{{"name": "segment/line of business", "detail": "one sentence", "revenue_share_pct": <number or null>}}],
  "competitors": [{{"area": "segment or market", "names": ["competitor", ...], "positioning": "one sentence"}}],
  "moat": {{"rating": "Wide | Narrow | None", "rationale": "2-3 sentences citing the metrics (ROE level/stability, franchise, market position)"}},
  "uncertainty": {{"rating": "Low | Medium | High | Very High", "rationale": "1-2 sentences (liquidity, earnings volatility, business risk)"}},
  "capital_allocation": {{"rating": "Exemplary | Standard | Poor", "rationale": "1-2 sentences (dividend record, payout discipline, balance sheet)"}},
  "fair_value": {{"low": <per-share>, "mid": <per-share>, "high": <per-share>,
                  "methods": "2-3 sentences: EXACTLY how you derived it from the anchors (e.g. historical-median P/E x FY2025 EPS, justified P/B from ROE, dividend yield reversion). Weight methods sensibly."}},
  "bulls_say": ["3-4 crisp bullets with numbers"],
  "bears_say": ["3-4 crisp bullets with numbers"],
  "thesis": "4-6 sentences: the analyst view synthesizing valuation, quality and risk"
}}

Hard rules:
- Numbers: use ONLY figures present in the anchors block; fair value must follow arithmetically from them (show the method). If segments lack revenue figures in the extracts, set revenue_share_pct to null.
- The competitors section reflects your market knowledge of Panama, NOT the filings -- keep it to well-known, real institutions and be conservative.
- If the filing extract is empty, derive the business description from the company's sector and your knowledge, and say so in one clause.
- English throughout."""


def _stars(price, fv_mid, uncertainty):
    """Morningstar-style: discount/premium thresholds widen with uncertainty."""
    if not price or not fv_mid:
        return None
    ratio = price / fv_mid
    widen = {"Low": 1.0, "Medium": 1.35, "High": 1.8, "Very High": 2.4}.get(uncertainty, 1.35)
    lo, hi = 0.15 * widen, 0.30 * widen
    if ratio <= 1 - hi:
        return 5
    if ratio <= 1 - lo:
        return 4
    if ratio < 1 + lo:
        return 3
    if ratio < 1 + hi:
        return 2
    return 1


def generate_report(nemo):
    import anthropic

    data = snap.load() or {}
    e = data.get("tickers", {}).get(nemo)
    if not e:
        raise SystemExit(f"{nemo} not in snapshot")
    q = e.get("quote") or {}

    _log(f"[{nemo}] gathering filing evidence...")
    text_ev, image_pages, pdf, report_name = gather_evidence(e)
    _log(f"[{nemo}] evidence: text={len(text_ev)} chars, image_pages={image_pages} ({report_name})")

    anchors = valuation_anchors(e)
    content = [{"type": "text", "text": REPORT_PROMPT.format(
        name=q.get("issuer_name") or nemo, nemo=nemo,
        anchors=json.dumps(anchors, indent=1),
        filing_note=text_ev or "(see attached filing page images)" if (text_ev or image_pages)
        else "(no business/segment note found in the filing)")}]
    for pg in image_pages:
        png = _render_png(pdf, pg)
        if png:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode()}})

    _log(f"[{nemo}] generating report with {MODEL}...")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(model=MODEL, max_tokens=4000,
                                 messages=[{"role": "user", "content": content}])
    text = next((b.text for b in msg.content if b.type == "text"), "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    report = json.loads(text[text.find("{"):text.rfind("}") + 1])

    unc = (report.get("uncertainty") or {}).get("rating", "Medium")
    fv_mid = (report.get("fair_value") or {}).get("mid")
    report["stars"] = _stars(anchors.get("price"), fv_mid, unc)
    report["price_at_report"] = anchors.get("price")
    report["source_report"] = report_name
    report["anchors"] = anchors

    save_part(f"report_{nemo.lower()}", [(nemo, "report", report)])
    fv = report.get("fair_value") or {}
    _log(f"[{nemo}] DONE: FV {fv.get('low')}-{fv.get('mid')}-{fv.get('high')} vs price "
         f"{anchors.get('price')} -> {report['stars']}* | moat {report.get('moat', {}).get('rating')} "
         f"| segments {len(report.get('segments', []))}")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="*")
    p.add_argument("--all", action="store_true")
    a = p.parse_args()
    tks = a.tickers
    if a.all or not tks:
        d = snap.load() or {}
        tks = [t for t, e in sorted(d.get("tickers", {}).items())
               if (e.get("financials") or {}).get("vision_used")
               and not (e.get("financials") or {}).get("error")]
    for tk in tks:
        try:
            generate_report(tk)
        except Exception as ex:  # noqa: BLE001
            _log(f"[{tk}] FAILED: {ex}")
