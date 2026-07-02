# -*- coding: utf-8 -*-
"""merge_parts.py -- apply all data/parts/*.pkl to the snapshot (single writer)."""

import glob
import os
import pickle

import snapshot as snap

PARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "parts")


def main():
    files = sorted(glob.glob(os.path.join(PARTS_DIR, "*.pkl")))
    if not files:
        print("no parts to merge")
        return
    data = snap.load() or {}
    for path in files:
        with open(path, "rb") as f:
            updates = pickle.load(f)
        for ticker, field, value in updates:
            data.setdefault("tickers", {}).setdefault(ticker, {})[field] = value
            print(f"merged {ticker}.{field} from {os.path.basename(path)}")
    snap.save(data)
    for path in files:
        os.remove(path)
    print(f"snapshot updated; {len(files)} part file(s) consumed")


if __name__ == "__main__":
    main()
