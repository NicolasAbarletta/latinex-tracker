# -*- coding: utf-8 -*-
"""
build_snapshot.py -- precompute the full data pack offline.

Does all the heavy lifting (PDF parsing + OCR, yfinance peers, and the Claude
deep dives) once and writes data/snapshot.pkl, which the dashboard then serves
instantly. Run it locally whenever new quarterly filings drop, then redeploy.

Usage:
    python build_snapshot.py                 # all common stocks; deep dive top 15 + watchlist
    python build_snapshot.py --deep-dive-top 33   # deep dive everything
    python build_snapshot.py --no-deep-dive       # data only, skip Claude
    python build_snapshot.py --tickers BGFG ASSA  # only these (debug)

The snapshot is saved incrementally after each ticker, so an interrupted run
keeps its progress and can be re-run to fill the rest.
"""

import argparse
import json
import multiprocessing as mp
import os
import queue as _queue
import sys
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd

# Whole-ticker work runs in a child process, and a child that exceeds the
# timeout is FORCE-KILLED (terminate/kill) so stuck OCR processes can't pile up
# and exhaust memory. Any native crash (MuPDF/pdfplumber/OCR/OOM) kills only the
# child; the parent records the ticker as failed and moves on.
TICKER_TIMEOUT = int(os.getenv("LATINEX_TICKER_TIMEOUT", "300"))

# Cap stored price history so the committed snapshot stays small; ~6 years
# comfortably covers the chart's longest (5Y) range.
HISTORY_DAYS = 2200


def _history_capped(nemo):
    df = api.get_history(nemo, "ALL")
    if df is not None and not df.empty:
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=HISTORY_DAYS))
        df = df[df["date"] >= cutoff].reset_index(drop=True)
    return df

import latinex_api as api
import financials as fin_mod
import peers as peers_mod
import analyst
import snapshot as snap

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_watchlist():
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            wl = json.load(f)
            return wl if isinstance(wl, list) else []
    except (OSError, ValueError):
        return []


def build_ticker(nemo):
    """Gather every per-company piece except the deep dive. Returns a dict."""
    t = {}
    q = api.get_quote(nemo)
    s = api.get_summary(nemo)
    t["quote"] = q
    t["summary"] = s
    t["kind"] = fin_mod.sector_kind(s["sector"], s["industry"])
    code = q.get("issuer_code")

    for label, fn in [
        ("history_all", lambda: _history_capped(nemo)),
        ("dividends", lambda: api.get_dividends(nemo)),
        ("financials", lambda: fin_mod.get_financials(nemo, issuer_code=code)),
        ("historical", lambda: fin_mod.get_historical(nemo, issuer_code=code)),
        ("documents", lambda: api.get_documents(code) if code else None),
        ("order_book_depth", lambda: api.get_order_book_depth(nemo)),
    ]:
        try:
            t[label] = fn()
        except Exception as e:
            _log(f"    {nemo}.{label} failed: {e}")
            t[label] = None

    try:
        issuer_key = (q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo)
        t["notices"] = api.get_notices(issuer_filter=issuer_key)
    except Exception as e:
        _log(f"    {nemo}.notices failed: {e}")
        t["notices"] = None
    return t


def _ticker_worker(nemo, q):
    """Child-process entry: build one ticker and return via queue."""
    os.environ["LATINEX_OCR_SUBPROCESS"] = "0"  # OCR in-process here (no nesting)
    try:
        q.put(("ok", build_ticker(nemo)))
    except Exception as e:  # noqa: BLE001
        q.put(("err", f"{type(e).__name__}: {e}"))


def run_ticker(nemo, timeout):
    """Run build_ticker in a child process, force-killing it if it exceeds
    `timeout`. Returns the result dict, or raises on timeout/crash/error."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_ticker_worker, args=(nemo, q), daemon=True)
    p.start()
    try:
        status, payload = q.get(timeout=timeout)  # drains queue -> no feeder deadlock
    except _queue.Empty:
        p.terminate(); p.join(5)
        if p.is_alive():
            p.kill(); p.join()
        raise TimeoutError(f"exceeded {timeout}s")
    finally:
        if p.is_alive():
            p.join(5)
            if p.is_alive():
                p.terminate()
    if status == "ok":
        return payload
    raise RuntimeError(payload)


def build(deep_dive_top=15, deep_dive_all=False, do_deep_dive=True, only=None, resume=False):
    t0 = time.time()
    data = snap.load() or {}
    data.setdefault("tickers", {})
    data["built_at"] = datetime.now().isoformat(timespec="seconds")
    data["model"] = analyst.MODEL

    _log("Fetching market-wide data...")
    try:
        data["universe"] = {"common": api.get_equity_universe(False),
                            "preferred": api.get_equity_universe(True)}
    except Exception as e:
        _log(f"  universe failed: {e}")
        data.setdefault("universe", {})
    for key, fn in [("index_history", lambda: api.get_index_history("1Y")),
                    ("order_book", lambda: api.get_order_book())]:
        try:
            data[key] = fn()
        except Exception as e:
            _log(f"  {key} failed: {e}")

    common = data.get("universe", {}).get("common")
    tickers = common["ticker"].tolist() if common is not None else _load_watchlist()
    if only:
        tickers = [t for t in tickers if t in only] or only

    # ---- per-ticker data ----
    _log(f"Building per-company data for {len(tickers)} tickers (resume={resume})...")
    for i, nemo in enumerate(tickers, 1):
        existing = data["tickers"].get(nemo)
        if resume and existing is not None and (
                existing.get("financials") is not None or existing.get("_failed")):
            _log(f"  [{i}/{len(tickers)}] {nemo}: skip (already attempted)")
            continue
        # Mark failed BEFORE processing: a malformed PDF can hard-crash the C
        # libraries (MuPDF/Tesseract) and kill the process; the marker means a
        # resumed run skips it instead of crashing on the same file again.
        data["tickers"].setdefault(nemo, {})["_failed"] = True
        snap.save(data)
        try:
            result = run_ticker(nemo, TICKER_TIMEOUT)
            entry = data["tickers"].get(nemo, {})
            entry.update(result)
            entry.pop("_failed", None)
            data["tickers"][nemo] = entry
            fin = entry.get("financials") or {}
            tag = "OCR" if fin.get("ocr_used") else ("parsed" if not fin.get("error") else "no-fin")
            _log(f"  [{i}/{len(tickers)}] {nemo}: {tag}")
        except TimeoutError:
            _log(f"  [{i}/{len(tickers)}] {nemo}: TIMEOUT {TICKER_TIMEOUT}s -> killed child, left as failed")
        except Exception as e:
            _log(f"  [{i}/{len(tickers)}] {nemo}: crash/err contained ({e}); left as failed")
        snap.save(data)  # incremental

    # ---- peers per sector ----
    kinds = sorted({e.get("kind", "generic") for e in data["tickers"].values()})
    data.setdefault("peers", {})
    for kind in kinds:
        if resume and data["peers"].get(kind) is not None:
            continue
        try:
            _log(f"Peers for {kind}...")
            data["peers"][kind] = peers_mod.get_peer_metrics(kind)
        except Exception as e:
            _log(f"  peers {kind} failed: {e}")
    snap.save(data)

    # ---- deep dives (expensive: 2 Claude calls each) ----
    if do_deep_dive:
        wl = set(_load_watchlist())
        # rank by market cap
        def mcap(nemo):
            return (data["tickers"].get(nemo, {}).get("quote", {}) or {}).get("market_cap") or 0
        ranked = sorted(tickers, key=mcap, reverse=True)
        selected = ranked if deep_dive_all else ranked[:deep_dive_top]
        selected = list(dict.fromkeys(selected + [t for t in tickers if t in wl]))
        _log(f"Generating deep dives for {len(selected)} companies "
             f"(top {len(selected)} by market cap + watchlist)...")
        for i, nemo in enumerate(selected, 1):
            entry = data["tickers"].setdefault(nemo, {})
            if entry.get("deep_dive") and not entry["deep_dive"].get("error"):
                _log(f"  [{i}/{len(selected)}] {nemo}: deep dive cached, skipping")
                continue
            if resume and entry.get("_dd_failed"):
                _log(f"  [{i}/{len(selected)}] {nemo}: deep dive previously failed, skipping")
                continue
            entry["_dd_failed"] = True  # crash marker (build_data_brief parses PDFs)
            snap.save(data)
            try:
                _log(f"  [{i}/{len(selected)}] {nemo}: generating deep dive...")
                fin_ov = entry.get("financials")  # reuse parsed/vision data; no re-parse/OCR
                dd = analyst.generate_deep_dive(nemo, fin_override=fin_ov)
                entry["deep_dive"] = dd
                if not dd.get("error"):
                    entry.pop("_dd_failed", None)
                status = "OK" if not dd.get("error") else f"ERR {dd['error']}"
                _log(f"      -> {status}")
            except Exception as e:
                _log(f"      -> EXC {e}")
            snap.save(data)  # incremental

    snap.save(data)
    dd_done = sum(1 for e in data["tickers"].values()
                  if (e.get("deep_dive") and not e["deep_dive"].get("error")))
    fin_done = sum(1 for e in data["tickers"].values()
                   if (e.get("financials") and not e["financials"].get("error")))
    _log(f"DONE in {time.time()-t0:.0f}s. {len(data['tickers'])} tickers, "
         f"{fin_done} with parsed financials, {dd_done} with deep dives.")
    _log(f"Saved to {snap.SNAPSHOT_PATH}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--deep-dive-top", type=int, default=15)
    p.add_argument("--deep-dive-all", action="store_true")
    p.add_argument("--no-deep-dive", action="store_true")
    p.add_argument("--resume", action="store_true",
                   help="skip tickers already attempted (for crash recovery)")
    p.add_argument("--tickers", nargs="*", default=None)
    a = p.parse_args()
    build(deep_dive_top=a.deep_dive_top, deep_dive_all=a.deep_dive_all,
          do_deep_dive=not a.no_deep_dive, only=a.tickers, resume=a.resume)
