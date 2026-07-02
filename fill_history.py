# -*- coding: utf-8 -*-
"""
fill_history.py -- re-run ONLY the 3-year history for given tickers, using the
comparative-column fallback for years that predate the issuer's first filing.

    python fill_history.py CMBG
"""

import os
import sys

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")

import latinex_api as api
import rebuild_vision as rb
import snapshot as snap


def main(tickers):
    for tk in tickers:
        data = snap.load() or {}
        e = data.get("tickers", {}).get(tk)
        if not e:
            print(f"{tk}: not in snapshot"); continue
        docs = e.get("documents")
        if docs is None or docs.empty:
            docs = api.get_documents((e.get("quote") or {}).get("issuer_code"))
        print(f"=== {tk}: rebuilding 3-year history ===", flush=True)
        hist = rb.build_history(docs, e.get("financials"))
        e["historical"] = hist
        snap.save(data)
        t = hist.get("table")
        cols = [c for c in (t.columns if t is not None else []) if str(c).startswith("FY")]
        print(f"{tk}: FY columns now {cols} | errors: {hist.get('errors')}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["CMBG"])
