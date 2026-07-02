# -*- coding: utf-8 -*-
"""
worker_task.py -- parallel-safe workers. Each worker READS the snapshot but
writes its result to data/parts/<task>.pkl; merge_parts.py applies all parts
to the snapshot in a single writer step (no concurrent-write races).

    python worker_task.py cmbg_hist      # CMBG FY2024 history via comparative
    python worker_task.py ppho_wc        # PPHO "what changed" retry
    python worker_task.py trenco_fix     # TRENCO quarterly re-extraction + dd + wc
"""

import os
import pickle
import sys

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")

import latinex_api as api
import financials as fm
import rebuild_vision as rb
import analytics
import analyst
import snapshot as snap

PARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "parts")


def save_part(name, updates):
    """updates: list of (ticker, field, value)."""
    os.makedirs(PARTS_DIR, exist_ok=True)
    tmp = os.path.join(PARTS_DIR, name + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(updates, f, protocol=4)
    os.replace(tmp, os.path.join(PARTS_DIR, name + ".pkl"))
    print(f"[part saved] {name}: {[(t, fld) for t, fld, _ in updates]}", flush=True)


def cmbg_hist():
    e = (snap.load() or {}).get("tickers", {}).get("CMBG", {})
    docs = e.get("documents")
    if docs is None or docs.empty:
        docs = api.get_documents((e.get("quote") or {}).get("issuer_code"))
    hist = rb.build_history(docs, e.get("financials"))
    t = hist.get("table")
    cols = [c for c in (t.columns if t is not None else []) if str(c).startswith("FY")]
    print("CMBG FY cols:", cols, "| errors:", hist.get("errors"), flush=True)
    save_part("cmbg_hist", [("CMBG", "historical", hist)])


def ppho_wc():
    e = (snap.load() or {}).get("tickers", {}).get("PPHO", {})
    fin = e.get("financials") or {}
    ht = (e.get("historical") or {}).get("table")
    deltas = analytics.quarter_deltas(ht, fin.get("is_quarterly", True))
    eq = analytics.earnings_quality(fin)
    wc = analyst.generate_whats_changed("PPHO", (e.get("quote") or {}).get("issuer_name", "PPHO"),
                                        deltas, eq)
    print("PPHO wc:", (wc.get("text") or wc.get("error") or "")[:150], flush=True)
    save_part("ppho_wc", [("PPHO", "whats_changed",
                           {"text": wc.get("text", ""), "deltas": deltas,
                            "error": wc.get("error")})])


def trenco_fix():
    """Re-extract TRENCO's quarterly (bad equity read), validate, regenerate
    deep dive + what-changed from the corrected financials."""
    e = (snap.load() or {}).get("tickers", {}).get("TRENCO", {})
    q, s = e.get("quote") or {}, e.get("summary") or {}
    docs = e.get("documents")
    if docs is None or docs.empty:
        docs = api.get_documents(q.get("issuer_code"))
    doc = docs[docs["type"] == "Informe Trimestral"].iloc[0]

    fin, m = None, {}
    for attempt in (1, 2):
        cand = rb._vision_fin(doc, True, doc.get("date", ""))
        if cand.get("error"):
            print(f"attempt {attempt}: vision error {cand['error']}", flush=True)
            continue
        mm = fm.extract_metrics(cand)
        eqty, assets = mm.get("total_equity"), mm.get("total_assets")
        # sanity: equity must be positive and a plausible share of assets
        if eqty and assets and 0.05 <= eqty / assets <= 0.95:
            fin, m = cand, mm
            break
        print(f"attempt {attempt}: implausible equity={eqty} assets={assets}; retrying", flush=True)
    if fin is None:
        print("TRENCO fix FAILED: no plausible extraction", flush=True)
        return
    r = fm.compute_ratios(fin, q.get("price"), s.get("shares_outstanding"))
    print(f"TRENCO fixed: NI={m.get('net_income')} equity={m.get('total_equity')} "
          f"P/B={r['pb']} ROE={r['roe_pct']}%", flush=True)

    dd = analyst.generate_deep_dive("TRENCO", fin_override=fin)
    ht = (e.get("historical") or {}).get("table")
    deltas = analytics.quarter_deltas(ht, True)
    eqm = analytics.earnings_quality(fin)
    wc = analyst.generate_whats_changed("TRENCO", q.get("issuer_name", "TRENCO"), deltas, eqm)
    save_part("trenco_fix", [
        ("TRENCO", "financials", fin),
        ("TRENCO", "deep_dive", dd),
        ("TRENCO", "whats_changed", {"text": wc.get("text", ""), "deltas": deltas,
                                     "error": wc.get("error")}),
    ])


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    {"cmbg_hist": cmbg_hist, "ppho_wc": ppho_wc, "trenco_fix": trenco_fix}[task]()
