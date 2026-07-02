# -*- coding: utf-8 -*-
"""
ingest_private.py -- analyze PRIVATE (non-listed) companies with the same
pipeline as the Latinex names.

Usage:
  1. Create a folder per company under private/, and drop its financial
     statements (PDFs) inside:
         private/Acme Holdings/estados_2025.pdf
         private/Acme Holdings/estados_2024.pdf   (optional prior years)
     Name files so the fiscal year appears somewhere (e.g. "...2025...").
  2. Run:  python ingest_private.py "Acme Holdings"
     (or with no argument to ingest every folder under private/)

What it does per company: vision-reads the statements (income, balance, cash
flow) exactly like the listed names, computes ratios / DuPont / earnings
quality, and generates the Morningstar-style report (market-price sections --
valuation bands, liquidity, stars -- are omitted, since there is no quote).
Results are stored in the snapshot under a "PRIV:" ticker and appear in the
Deep Dive picker.
"""

import argparse
import json
import os
import re
import sys

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")

import financials as fm
import vision_extract as vx
import analytics
import analyst
import snapshot as snap
from rebuild_vision import _statement_pages

PRIVATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "private")


def _log(m):
    print(m, flush=True)


def _year_of(filename):
    m = re.findall(r"(20\d{2})", filename)
    return int(m[-1]) if m else None


def ingest(company_dir):
    name = os.path.basename(company_dir.rstrip("/\\"))
    ticker = f"PRIV:{name[:14].upper().replace(' ', '_')}"
    pdfs = sorted(
        (os.path.join(company_dir, f) for f in os.listdir(company_dir)
         if f.lower().endswith(".pdf")),
        key=lambda p: _year_of(os.path.basename(p)) or 0, reverse=True)
    if not pdfs:
        _log(f"{name}: no PDFs found in {company_dir}")
        return

    _log(f"=== {name} ({ticker}) — {len(pdfs)} PDF(s) ===")
    latest_fin, columns, sources = None, {}, []
    for path in pdfs[:4]:                     # newest + up to 3 prior years
        year = _year_of(os.path.basename(path))
        with open(path, "rb") as f:
            pdf = f.read()
        pages = _statement_pages(pdf, annual=True)
        if not pages:
            _log(f"  {os.path.basename(path)}: no statement pages found; skipping")
            continue
        fin = vx.extract_statements(pdf, pages, report_name=os.path.basename(path),
                                    is_quarterly=False, period_hint=str(year or ""))
        if fin.get("error"):
            _log(f"  {os.path.basename(path)}: {fin['error']}")
            continue
        m = fm.extract_metrics(fin)
        _log(f"  {os.path.basename(path)}: NI={m.get('net_income')} "
             f"assets={m.get('total_assets')} equity={m.get('total_equity')}")
        if latest_fin is None:
            latest_fin = fin
        if year and m:
            columns[f"FY{year}"] = m
            sources.append((f"FY{year}", os.path.basename(path), path))

    if latest_fin is None:
        _log(f"{name}: could not extract statements from any PDF")
        return

    import pandas as pd
    rows = []
    ordered = sorted(columns)
    for key, label, _is in fm.HIST_METRICS:
        vals = {c: columns[c].get(key) for c in ordered}
        if any(v is not None for v in vals.values()):
            rows.append({"Metric": label, **vals})
    hist = {"table": pd.DataFrame(rows, columns=["Metric"] + ordered) if rows else pd.DataFrame(),
            "sources": sources, "errors": []}

    entry = {
        "quote": {"ticker": ticker, "issuer_name": name, "price": None, "market_cap": None,
                  "issuer_code": None, "daily_change_pct": None, "ytd_change_pct": None,
                  "as_of": None, "volume": None, "avg_volume": None,
                  "daily_change": None, "ytd_change": None},
        "summary": {"sector": "Private holding", "industry": "", "isin": "",
                    "shares_outstanding": None, "listing_date": ""},
        "kind": "generic", "financials": latest_fin, "historical": hist,
        "history_all": None, "dividends": None, "documents": None, "notices": None,
        "order_book_depth": None, "private": True,
    }

    _log("  generating deep dive...")
    dd = analyst.generate_deep_dive_private(name, latest_fin, hist) \
        if hasattr(analyst, "generate_deep_dive_private") else None
    if dd is None:
        # fall back: reuse the report prompt with the available anchors
        eqm = analytics.earnings_quality(latest_fin)
        m = fm.extract_metrics(latest_fin)
        anchors = {"note": "PRIVATE company - no market price; valuation sections omitted",
                   "net_income_latest": m.get("net_income"),
                   "total_assets": m.get("total_assets"), "total_equity": m.get("total_equity"),
                   "revenue": m.get("revenue"),
                   "cash_conversion_pct": eqm.get("cash_conversion_pct"),
                   "ni_by_year": {c: columns[c].get("net_income") for c in ordered}}
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        prompt = (f"You are a private-company analyst. Metrics for {name} (private, Panama "
                  f"region):\n{json.dumps(anchors, indent=1)}\n\nReturn ONLY JSON: "
                  '{"business_description": "...", "moat": {"rating": "Wide|Narrow|None", '
                  '"rationale": "..."}, "bulls_say": [...], "bears_say": [...], '
                  '"thesis": "..."} — English, numbers only from the metrics block.')
        msg = client.messages.create(model=analyst.MODEL, max_tokens=2500,
                                     messages=[{"role": "user", "content": prompt}])
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        text = text[text.find("{"):text.rfind("}") + 1]
        rep = json.loads(text)
        rep["private"] = True
        entry["report"] = rep

    data = snap.load() or {}
    data.setdefault("tickers", {})[ticker] = entry
    snap.save(data)
    _log(f"  saved as {ticker}. It will appear in the Deep Dive picker.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("companies", nargs="*")
    a = p.parse_args()
    os.makedirs(PRIVATE_DIR, exist_ok=True)
    targets = ([os.path.join(PRIVATE_DIR, c) for c in a.companies] if a.companies
               else [os.path.join(PRIVATE_DIR, d) for d in os.listdir(PRIVATE_DIR)
                     if os.path.isdir(os.path.join(PRIVATE_DIR, d))])
    if not targets:
        _log(f"No company folders under {PRIVATE_DIR}. Create private/<Company>/ "
             "and drop its financial-statement PDFs inside.")
    for t in targets:
        try:
            ingest(t)
        except Exception as e:  # noqa: BLE001
            _log(f"{t}: FAILED {e}")
