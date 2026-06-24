# -*- coding: utf-8 -*-
"""
snapshot.py -- precomputed data pack for the Latinex Equity Tracker.

The dashboard is slow when it computes everything live (PDF parsing, OCR,
yfinance peers, two Claude calls per deep dive). Instead we precompute it all
offline with build_snapshot.py, persist it here, and have the dashboard serve
from it instantly. Refresh quarterly (when new filings drop) by re-running the
builder and redeploying.

The snapshot is a single pickle holding plain dicts and pandas objects:

    {
      "built_at": "2026-06-23T12:00:00",   # ISO string (no tz)
      "model": "claude-opus-4-6",
      "universe": {"common": df, "preferred": df},
      "index_history": df,
      "order_book": df,
      "peers": {"banking": df, "insurance": df, "generic": df},
      "tickers": {
        "BGFG": {
          "quote": {...}, "summary": {...}, "kind": "banking",
          "history_all": df, "dividends": df, "financials": {...},
          "historical": {...}, "documents": df, "notices": df,
          "order_book_depth": {...}, "deep_dive": {...} | None,
        }, ...
      },
    }
"""

import os
import pickle

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "snapshot.pkl")


def save(data, path=SNAPSHOT_PATH):
    """Atomically write the snapshot dict to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f, protocol=4)
    os.replace(tmp, path)


def load(path=SNAPSHOT_PATH):
    """Return the snapshot dict, or None if absent/unreadable."""
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError):
        return None


def exists(path=SNAPSHOT_PATH):
    return os.path.exists(path)
