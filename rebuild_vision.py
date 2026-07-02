# -*- coding: utf-8 -*-
"""
rebuild_vision.py -- rebuild a company's financials entirely with Claude vision.

Reads, via vision (reliable on scanned/varied PDFs), the latest quarterly report
AND the FY2023/24/25 annual (Q4) reports, then recomputes ratios + the 3-year
history and regenerates the deep dive. Writes the result into the snapshot.

    python rebuild_vision.py MELO CMBG PPHO BGFG EGIN ASSA

OCR is used only to locate the statement pages (isolated in a child process);
the numbers come from vision. Each report is one vision call.
"""

import argparse
import os

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")  # isolate page-finding OCR

import pandas as pd

import latinex_api as api
import financials as fm
import ocr
import vision_extract as vx
import analyst
import snapshot as snap

HISTORY_DAYS = 2200


def _log(m):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _annual_rank(text):
    """Higher = more likely the AUDITED ANNUAL statement (full year), lower =
    the quarterly summary. Q4 filings contain both."""
    n = fm._norm(text[:600])
    score = 0
    if "ano terminado" in n or "ano que termino" in n:
        score += 3
    if "acumulado" in n:
        score += 1
    if "31 de diciembre" in n:
        score += 1
    if "trimestres terminados" in n or "tres meses" in n or "para los trimestres" in n:
        score -= 3
    return score


def _is_cashflow_page(text):
    """Cash-flow statement page (the shared classifier deliberately skips it)."""
    lines = [l.strip() for l in fm._norm(text).split("\n") if l.strip()][:5]
    return any(l.startswith("estado") and "flujos de efectivo" in l and len(l) < 80
               for l in lines)


def _statement_pages(pdf_bytes, annual=False):
    texts = ocr.ocr_pdf_pages(pdf_bytes)
    inc = [i for i in sorted(texts) if fm._classify_page(texts[i]) == "income"]
    bal = [i for i in sorted(texts) if fm._classify_page(texts[i]) == "balance"]
    cfs = [i for i in sorted(texts) if _is_cashflow_page(texts[i])]
    if annual and inc:
        # Q4 reports include BOTH a quarterly summary and the audited annual
        # statements; pick the annual (year-ended) income page only, so vision
        # isn't fed the 3-month quarter columns.
        inc = [max(inc, key=lambda i: _annual_rank(texts[i]))]
    if annual and bal:
        bal = [max(bal, key=lambda i: _annual_rank(texts[i]))]
    if annual and cfs:
        cfs = [max(cfs, key=lambda i: _annual_rank(texts[i]))]
    # include the page after each statement (statements often span 2 pages)
    pages = sorted(set(p for x in (inc + bal + cfs) for p in (x, x + 1)))
    return pages


def _vision_fin(doc, is_quarterly, period_hint, prior_year=False):
    pdf = fm._get_pdf_cached(doc["name"], doc["pdf_url"])
    pages = _statement_pages(pdf, annual=not is_quarterly)
    if not pages:
        return {"error": "no statement pages found"}
    return vx.extract_statements(pdf, pages, report_name=doc["name"],
                                 pdf_url=doc["pdf_url"], is_quarterly=is_quarterly,
                                 period_hint=period_hint, dpi=200,
                                 prior_year=prior_year)


def _history_capped(nemo):
    df = api.get_history(nemo, "ALL")
    if df is not None and not df.empty:
        from datetime import datetime, timedelta
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=HISTORY_DAYS))
        df = df[df["date"] >= cutoff].reset_index(drop=True)
    return df


def build_history(docs, latest_fin, years=(2023, 2024, 2025)):
    """3-year annual history from vision-read Q4 reports + latest interim column."""
    columns, sources, errors = {}, [], []
    for y in years:
        match = docs[docs["name"].str.contains(f"{y}_Q4", case=False, na=False)]
        prior = False
        if match.empty:
            # Recently listed issuers have no FY{y} filing, but the NEXT year's
            # annual report carries FY{y} as the comparative column.
            match = docs[docs["name"].str.contains(f"{y + 1}_Q4", case=False, na=False)]
            prior = True
            if match.empty:
                errors.append(f"FY{y}: no Q4 report (listed later?)")
                continue
        doc = match.iloc[0]
        fin = _vision_fin(doc, is_quarterly=False, period_hint=str(y), prior_year=prior)
        if fin.get("error"):
            errors.append(f"FY{y}: {fin['error']}")
            continue
        m = fm.extract_metrics(fin)
        if m:
            columns[f"FY{y}"] = m
            tag = " (comparative)" if prior else ""
            sources.append((f"FY{y}{tag}", doc["name"], doc["pdf_url"]))
        _log(f"      FY{y}{' (from comparative)' if prior else ''}: "
             f"NI={m.get('net_income')} rev={m.get('revenue')} "
             f"assets={m.get('total_assets')} equity={m.get('total_equity')}")
    if latest_fin and not latest_fin.get("error"):
        m = fm.extract_metrics(latest_fin)
        if m:
            columns["Latest"] = m
            sources.append(("Latest", latest_fin.get("report_name", ""), latest_fin.get("pdf_url", "")))
    if not columns:
        return {"table": pd.DataFrame(), "sources": sources, "errors": errors}
    ordered = sorted([c for c in columns if c.startswith("FY")]) + \
        [c for c in columns if not c.startswith("FY")]
    rows = []
    for key, label, _is in fm.HIST_METRICS:
        vals = {c: columns[c].get(key) for c in ordered}
        if any(v is not None for v in vals.values()):
            rows.append({"Metric": label, **vals})
    return {"table": pd.DataFrame(rows, columns=["Metric"] + ordered),
            "sources": sources, "errors": errors}


def rebuild(nemo, do_deep_dive=True):
    _log(f"=== {nemo} ===")
    q = api.get_quote(nemo)
    s = api.get_summary(nemo)
    kind = fm.sector_kind(s["sector"], s["industry"])
    code = q.get("issuer_code")
    docs = api.get_documents(code) if code else pd.DataFrame()

    entry = {"quote": q, "summary": s, "kind": kind}
    for label, fn in [("history_all", lambda: _history_capped(nemo)),
                      ("dividends", lambda: api.get_dividends(nemo)),
                      ("documents", lambda: docs),
                      ("order_book_depth", lambda: api.get_order_book_depth(nemo))]:
        try:
            entry[label] = fn()
        except Exception as e:
            _log(f"    {label} failed: {e}"); entry[label] = None
    try:
        issuer_key = q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo
        entry["notices"] = api.get_notices(issuer_filter=issuer_key)
    except Exception:
        entry["notices"] = None

    # latest quarterly via vision
    quarterly = docs[docs["type"] == "Informe Trimestral"] if not docs.empty else pd.DataFrame()
    if quarterly.empty:
        _log("    no quarterly report; skipping financials")
        entry["financials"] = fm._empty_result("No quarterly report")
    else:
        doc = quarterly.iloc[0]
        _log(f"    quarterly: {doc['name']}")
        fin = _vision_fin(doc, is_quarterly=True, period_hint=doc.get("date", ""))
        entry["financials"] = fin
        if fin.get("error"):
            _log(f"    quarterly vision error: {fin['error']}")
        else:
            r = fm.compute_ratios(fin, q["price"], s["shares_outstanding"])
            _log(f"    income={len(fin['income'])} balance={len(fin['balance'])} | "
                 f"EPS={r['eps']} P/E={r['pe']} P/B={r['pb']} ROE={r['roe_pct']}%")
        _log("    building 3-year history (vision)...")
        entry["historical"] = build_history(docs, fin)

    # persist data first (so a deep-dive failure doesn't lose the financials)
    data = snap.load() or {}
    data.setdefault("tickers", {})[nemo] = {**data.get("tickers", {}).get(nemo, {}), **entry}
    snap.save(data)

    if do_deep_dive and not entry["financials"].get("error"):
        _log("    generating deep dive (vision financials)...")
        dd = analyst.generate_deep_dive(nemo, fin_override=entry["financials"])
        data = snap.load() or {}
        data["tickers"].setdefault(nemo, {})["deep_dive"] = dd
        snap.save(data)
        _log(f"    deep dive: {'OK -> ' + str((dd.get('data') or {}).get('verdict')) if not dd.get('error') else 'ERR ' + str(dd['error'])}")

        # "What changed this quarter" -- computed deltas narrated in one short call
        try:
            import analytics
            ht = (entry.get("historical") or {}).get("table")
            deltas = analytics.quarter_deltas(ht, entry["financials"].get("is_quarterly", True))
            eqm = analytics.earnings_quality(entry["financials"])
            wc = analyst.generate_whats_changed(nemo, q.get("issuer_name") or nemo, deltas, eqm)
            data = snap.load() or {}
            data["tickers"].setdefault(nemo, {})["whats_changed"] = {
                "text": wc.get("text", ""), "deltas": deltas, "error": wc.get("error")}
            snap.save(data)
            _log(f"    what changed: {'OK' if not wc.get('error') else 'ERR ' + str(wc['error'])}")
        except Exception as e:  # noqa: BLE001
            _log(f"    what changed failed: {e}")
    _log(f"    {nemo} done.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="+")
    p.add_argument("--no-deep-dive", action="store_true")
    a = p.parse_args()
    for tk in a.tickers:
        try:
            rebuild(tk, do_deep_dive=not a.no_deep_dive)
        except Exception as e:
            _log(f"  {tk}: FAILED {e}")
            import traceback; traceback.print_exc()
