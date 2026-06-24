# -*- coding: utf-8 -*-
"""
fix_company.py -- recover a scanned company's statements with Claude vision and
write them (plus a deep dive) into the snapshot.

For issuers whose statements are images that Tesseract can't transcribe cleanly
(e.g. Grupo Melo), this finds the income/balance pages, has Claude vision read
them, computes ratios, and stores a proper financials dict + deep dive so the
company shows up fully in the dashboard.

    python fix_company.py MELO
    python fix_company.py MELO CMBG --no-deep-dive
"""

import argparse
import os

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "0")  # in-process OCR for page-finding

import latinex_api as api
import financials as fin_mod
import ocr
import vision_extract as vx
import analyst
import snapshot as snap


def _statement_pages(pdf_bytes):
    texts = ocr.ocr_pdf_pages(pdf_bytes)
    inc, bal = [], []
    for i in sorted(texts):
        k = fin_mod._classify_page(texts[i])
        if k == "income":
            inc.append(i)
        elif k == "balance":
            bal.append(i)
    return inc, bal


def fix(nemo, do_deep_dive=True, pages_override=None):
    print(f"\n=== {nemo} ===", flush=True)
    q = api.get_quote(nemo)
    s = api.get_summary(nemo)
    docs = api.get_documents(q["issuer_code"])
    quarterly = docs[docs["type"] == "Informe Trimestral"]
    if quarterly.empty:
        print("  no quarterly report"); return False
    doc = quarterly.iloc[0]
    pdf = fin_mod._get_pdf_cached(doc["name"], doc["pdf_url"])

    if pages_override:
        pages = sorted(set(pages_override))
        print(f"  statement pages (given): {pages}", flush=True)
    else:
        inc, bal = _statement_pages(pdf)
        pages = sorted(set(inc + bal))
        print(f"  statement pages (OCR-classified): income={inc} balance={bal}", flush=True)
    if not pages:
        print("  could not locate statement pages"); return False

    fin = vx.extract_statements(pdf, pages, report_name=doc["name"],
                                pdf_url=doc["pdf_url"], is_quarterly=True,
                                period_hint=doc.get("date", ""))
    if fin["error"]:
        print(f"  vision error: {fin['error']}"); return False
    print(f"  vision extracted: income={len(fin['income'])} rows, balance={len(fin['balance'])} rows "
          f"(scale {fin['scale_label']})", flush=True)

    kind = fin_mod.sector_kind(s["sector"], s["industry"])
    r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
    d = fin_mod.dupont_decomposition(fin, kind)
    print(f"  ratios: EPS={r['eps']} P/E={r['pe']} P/B={r['pb']} ROE={r['roe_pct']}%  "
          f"| DuPont ROE={d['roe_pct']} = ROA {d['roa_pct']} x Lev {d['leverage_x']}", flush=True)

    data = snap.load() or {}
    e = data.setdefault("tickers", {}).setdefault(nemo, {})
    e["quote"], e["summary"], e["kind"] = q, s, kind
    e["financials"] = fin
    e.pop("_failed", None)
    snap.save(data)
    print("  stored financials in snapshot", flush=True)

    if do_deep_dive:
        print("  generating deep dive (with vision financials)...", flush=True)
        dd = analyst.generate_deep_dive(nemo, fin_override=fin)
        e["deep_dive"] = dd
        snap.save(data)
        if dd.get("error"):
            print(f"  deep dive ERROR: {dd['error']}")
        else:
            v = (dd.get("data") or {}).get("verdict")
            print(f"  deep dive OK -> verdict: {v}", flush=True)
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="+")
    p.add_argument("--no-deep-dive", action="store_true")
    p.add_argument("--pages", nargs="*", type=int, default=None,
                   help="explicit statement page indices (skip OCR page-finding)")
    a = p.parse_args()
    for tk in a.tickers:
        try:
            fix(tk, do_deep_dive=not a.no_deep_dive, pages_override=a.pages)
        except Exception as ex:
            print(f"  {tk}: FAILED {ex}")
