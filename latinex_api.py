# -*- coding: utf-8 -*-
"""
latinex_api.py -- Data layer for the Latinex (Panama Stock Exchange) tracker.

Thin client over the public (undocumented) JSON endpoints at latinexbolsa.com.
All functions return pandas DataFrames or plain dicts with proper dtypes.
Network errors raise LatinexAPIError so the UI layer can surface them
instead of silently rendering empty tables.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import quote as _urlquote

import pandas as pd
import requests

BASE_URL = "https://www.latinexbolsa.com"
PDF_BASE_URL = "https://files.latinexbolsa.com/pabvprblob01/"

COMMON_CLASSES = {"ACCIONES COMUNES"}
PREFERRED_CLASSES = {"ACCIONES PREFERIDAS", "ACC PREFERENTES ACUM"}

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.latinexbolsa.com/en/",
})


class LatinexAPIError(Exception):
    """Raised when a Latinex endpoint cannot be fetched or parsed."""


def _pdf_url(path):
    """Build a markdown-safe filing URL (paths contain spaces)."""
    if not path:
        return ""
    return PDF_BASE_URL + _urlquote(path.lstrip("/"), safe="/")


def _get_json(path, params=None, timeout=30):
    url = f"{BASE_URL}{path}"
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        raise LatinexAPIError(f"Failed to fetch {path}: {e}") from e


def _to_float(val):
    """Coerce API values to float. The API mixes strings ('142.00') and numbers."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _first(lst):
    """The instrument endpoints wrap single objects in one-element arrays."""
    return lst[0] if isinstance(lst, list) and lst else {}


# ---------------------------------------------------------------------------
# Market universe
# ---------------------------------------------------------------------------

def get_equity_universe(include_preferred=False):
    """All Latinex equities (latest snapshot per ticker), excluding fund shares.

    Returns DataFrame: ticker, issuer, price, ytd, low_52w, high_52w,
    volume, sector, isin, instrument_class, as_of.
    """
    raw = _get_json("/emisor/mercado/accionario")
    records = raw.get("data", [])
    if not records:
        raise LatinexAPIError("/emisor/mercado/accionario returned no data")

    latest = {}
    for rec in records:
        nemo = rec.get("nemotecnicoCode")
        if not nemo:
            continue
        if nemo not in latest or (rec.get("fecha") or "") > (latest[nemo].get("fecha") or ""):
            latest[nemo] = rec

    wanted = set(COMMON_CLASSES)
    if include_preferred:
        wanted |= PREFERRED_CLASSES

    rows = []
    for nemo, rec in latest.items():
        if rec.get("instrumento") not in wanted:
            continue
        rows.append({
            "ticker": nemo,
            "issuer": rec.get("emisor", ""),
            "price": _to_float(rec.get("precio")),
            "ytd": _to_float(rec.get("ytd")),
            "low_52w": _to_float(rec.get("precio_minimo")),
            "high_52w": _to_float(rec.get("precio_maximo")),
            "volume": _to_float(rec.get("volumen")),
            "sector": rec.get("sector", ""),
            "isin": rec.get("isin", ""),
            "instrument_class": rec.get("instrumento", ""),
            "as_of": rec.get("fecha", ""),
        })
    df = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Per-instrument data
# ---------------------------------------------------------------------------

def get_quote(nemo):
    """Current quote: price, daily/YTD change, market cap, volume, issuer code."""
    raw = _get_json("/emisor/detalle/instrumento/top", params={"instrumento": nemo})
    top = _first(raw.get("datosTop"))
    left = _first(raw.get("datosTopLeft"))
    return {
        "ticker": nemo,
        "price": _to_float(top.get("precio_cierre_dia")),
        "daily_change": _to_float(top.get("variacion_diaria")),
        "daily_change_pct": _to_float(top.get("variacion_porcentual_diaria")),
        "ytd_change": _to_float(top.get("variacion")),
        "ytd_change_pct": _to_float(top.get("variacion_porcentual")),
        "market_cap": _to_float(top.get("capitalizacion")),
        "volume": _to_float(top.get("volumen")),
        "avg_volume": _to_float(top.get("volumen_promedio")),
        "as_of": top.get("fecha", ""),
        "issuer_name": left.get("nombre_emisor", ""),
        "issuer_code": left.get("code_emisor", ""),
    }


def get_summary(nemo):
    """Instrument summary: ISIN, sector, shares outstanding, last dividend."""
    raw = _get_json("/emisor/detalle/instrumento/resumen", params={"instrumento": nemo})
    s = _first(raw.get("resumen"))
    return {
        "isin": s.get("isin", ""),
        "sector": s.get("sector", ""),
        "industry": s.get("industria", ""),
        "country": s.get("pais", ""),
        "instrument_class": s.get("instrumentoClase", ""),
        "shares_outstanding": _to_float(s.get("accionesCirculacion")),
        "last_dividend_amount": _to_float(s.get("dividendo")),
        "last_dividend_date": s.get("ultimoDividendo", ""),
        "listing_date": s.get("fechaEmision", ""),
        "resolution": s.get("resolucion", ""),
    }


def get_history(nemo, rango="1Y"):
    """OHLC price history. rango: 1M, 3M, 6M, 1Y, 5Y, ALL.

    Returns DataFrame: date, open, high, low, close, volume, amount (sorted by date).
    """
    raw = _get_json("/emisor/detalle/instrumento/historico",
                    params={"instrumento": nemo, "rango": rango})
    rows = []
    for rec in raw.get("data", []):
        rows.append({
            "date": rec.get("fecha", ""),
            "open": _to_float(rec.get("apertura")),
            "high": _to_float(rec.get("alto")),
            "low": _to_float(rec.get("bajo")),
            "close": _to_float(rec.get("ultimoPrecio")),
            "volume": _to_float(rec.get("cantidad")),
            "amount": _to_float(rec.get("monto")),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


def get_dividends(nemo):
    """Full dividend history. Returns DataFrame:
    record_date, payment_date, amount, type (Ordinario/Extraordinario)."""
    raw = _get_json("/emisor/detalle/instrumento/pago",
                    params={"instrumento": nemo, "rango": "ALL"})
    rows = []
    for rec in raw.get("data", []):
        rows.append({
            "record_date": rec.get("fechaRegistro", ""),
            "payment_date": rec.get("fechaPago", ""),
            "amount": _to_float(rec.get("monto")),
            "type": rec.get("tipo", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        # Dates come as '27-Feb-2026'
        df["payment_date"] = pd.to_datetime(df["payment_date"], format="%d-%b-%Y", errors="coerce")
        df["record_date"] = pd.to_datetime(df["record_date"], format="%d-%b-%Y", errors="coerce")
        df = df.sort_values("payment_date", ascending=False).reset_index(drop=True)
    return df


def get_dividend_yield(dividends_df, price):
    """Trailing 12-month dividend yield from a get_dividends() frame.

    Returns dict: ordinary_12m, total_12m, ordinary_yield_pct, total_yield_pct.
    Uses a 365-day payment-date window — not 'last N payments'.
    """
    out = {"ordinary_12m": None, "total_12m": None,
           "ordinary_yield_pct": None, "total_yield_pct": None}
    if dividends_df is None or dividends_df.empty or not price:
        return out

    cutoff = pd.Timestamp(datetime.now() - timedelta(days=365))
    recent = dividends_df[dividends_df["payment_date"] >= cutoff]
    if recent.empty:
        return out

    total = recent["amount"].sum()
    ordinary = recent.loc[recent["type"].str.lower() == "ordinario", "amount"].sum()
    out["ordinary_12m"] = round(float(ordinary), 4)
    out["total_12m"] = round(float(total), 4)
    out["ordinary_yield_pct"] = round(float(ordinary) / price * 100, 2)
    out["total_yield_pct"] = round(float(total) / price * 100, 2)
    return out


# ---------------------------------------------------------------------------
# Issuer documents and market-wide feeds
# ---------------------------------------------------------------------------

def get_documents(issuer_code):
    """Regulatory filings for an issuer. Returns DataFrame:
    date, type, name, pdf_url (sorted newest first, as the API returns them)."""
    raw = _get_json("/emisor/detalle/emisor/documentos", params={"code": issuer_code})
    rows = []
    for rec in raw.get("data", []):
        rows.append({
            "date": rec.get("registro", ""),
            "type": rec.get("documento", ""),
            "name": rec.get("nombre", ""),
            "pdf_url": _pdf_url(rec.get("path")),
        })
    return pd.DataFrame(rows)


def get_notices(issuer_filter=None):
    """Hechos relevantes (corporate disclosures). Optionally filter by issuer
    substring (case-insensitive). Returns DataFrame: date, issuer, title, pdf_url."""
    raw = _get_json("/emisor/hechos/relevantes")
    rows = []
    for rec in raw.get("data", []):
        issuer = rec.get("emisor") or ""
        if issuer_filter and issuer_filter.lower() not in issuer.lower():
            continue
        rows.append({
            "date": rec.get("registro", ""),
            "issuer": issuer,
            "title": rec.get("nombre", ""),
            "pdf_url": _pdf_url(rec.get("path")),
        })
    return pd.DataFrame(rows)


def _ob_levels(rec):
    """All bid/ask price levels for one /ofertas/mercado record.

    The top-level fields are the best level (order_depth 0); `detail` carries
    the deeper levels (order_depth 1, 2, ...), each possibly one-sided.
    Returns (bids, asks) as lists of (price, qty), bids high->low, asks low->high.
    """
    bids, asks = [], []
    for lvl in [rec] + (rec.get("detail") or []):
        b, bq = _to_float(lvl.get("compra")), _to_float(lvl.get("cantidadCompra"))
        if b:
            bids.append((b, bq or 0.0))
        a, aq = _to_float(lvl.get("venta")), _to_float(lvl.get("cantidadVenta"))
        if a:
            asks.append((a, aq or 0.0))
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return bids, asks


def get_order_book():
    """Current market offers (best bid/ask per instrument).
    Returns DataFrame: ticker, bid, bid_qty, ask, ask_qty, bid_amount, ask_amount."""
    raw = _get_json("/ofertas/mercado")
    rows = []
    for rec in raw.get("data", []):
        bids, asks = _ob_levels(rec)
        best_bid = bids[0] if bids else (None, None)
        best_ask = asks[0] if asks else (None, None)
        rows.append({
            "ticker": rec.get("nemotecnico", ""),
            "bid": best_bid[0], "bid_qty": best_bid[1],
            "ask": best_ask[0], "ask_qty": best_ask[1],
            "bid_amount": _to_float(rec.get("montoCompra")),
            "ask_amount": _to_float(rec.get("montoVenta")),
        })
    return pd.DataFrame(rows)


def get_order_book_depth(nemo, levels=6):
    """Bid/ask ladder (depth of book) for a single instrument.

    Returns dict: bids, asks (each list of (price, qty)); bid_total, ask_total
    (summed quantities across the returned levels); imbalance_pct (positive =
    more demand than supply). Empty lists when the instrument has no open orders.
    """
    raw = _get_json("/ofertas/mercado")
    for rec in raw.get("data", []):
        if rec.get("nemotecnico") == nemo:
            bids, asks = _ob_levels(rec)
            bids, asks = bids[:levels], asks[:levels]
            bt = sum(q for _, q in bids)
            at = sum(q for _, q in asks)
            imb = round((bt - at) / (bt + at) * 100) if (bt + at) else None
            return {"bids": bids, "asks": asks, "bid_total": bt,
                    "ask_total": at, "imbalance_pct": imb}
    return {"bids": [], "asks": [], "bid_total": 0, "ask_total": 0,
            "imbalance_pct": None}


def get_index_history(period="1Y"):
    """BVPSI market index time series. period: D, 1M, 3M, 6M, 1Y, 5Y, ALL.

    Returns DataFrame: date, value (sorted by date).
    """
    raw = _get_json(f"/chart/home/get/chart-{period}")
    data = raw.get("data", raw)
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)
    rows = []
    for rec in data or []:
        rows.append({"date": rec.get("fecha", ""), "value": _to_float(rec.get("indice"))})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce")
        df = df.dropna(subset=["date", "value"]).sort_values("date").reset_index(drop=True)
    return df


def download_pdf_bytes(pdf_url, timeout=60):
    """Download a filing PDF. Returns raw bytes."""
    try:
        resp = _session.get(pdf_url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        raise LatinexAPIError(f"Failed to download PDF {pdf_url}: {e}") from e


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Latinex API smoke test ===")

    uni = get_equity_universe()
    print(f"\nCommon stocks: {len(uni)}")
    targets = uni[uni["ticker"].isin(["ASSA", "BGFG", "EGIN"])]
    print(targets[["ticker", "price", "ytd", "low_52w", "high_52w", "as_of"]].to_string(index=False))

    uni_pref = get_equity_universe(include_preferred=True)
    print(f"Common + preferred: {len(uni_pref)}")

    q = get_quote("BGFG")
    print(f"\nBGFG quote: price={q['price']} mcap={q['market_cap']} issuer_code={q['issuer_code']}")

    s = get_summary("BGFG")
    print(f"BGFG shares outstanding: {s['shares_outstanding']}")

    h = get_history("BGFG", "1Y")
    print(f"BGFG 1Y history: {len(h)} rows, last close={h['close'].iloc[-1] if not h.empty else 'N/A'}")

    d = get_dividends("BGFG")
    y = get_dividend_yield(d, q["price"])
    print(f"BGFG dividends: {len(d)} records, trailing-12m total={y['total_12m']}, yield={y['total_yield_pct']}%")

    docs = get_documents(q["issuer_code"])
    qr = docs[docs["type"] == "Informe Trimestral"]
    print(f"BGFG documents: {len(docs)}, latest quarterly: {qr.iloc[0]['name'] if not qr.empty else 'N/A'}")

    ob = get_order_book()
    print(f"Order book entries: {len(ob)}")
    print("\nSmoke test PASSED")
