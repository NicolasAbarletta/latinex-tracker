# -*- coding: utf-8 -*-
"""
peers.py -- Comparables internacionales via Yahoo Finance.

Sets curados por sector. Cada empresa local del watchlist se compara contra
peers LatAm + referencias globales usando metricas de yfinance, con filtros
de sanidad (yfinance a veces devuelve P/B basura por clases de accion).
"""

import pandas as pd
import yfinance as yf

PEER_SETS = {
    "banking": [
        ("BLX", "NYSE"),       # Bladex - banco panameno listado en NYSE
        ("CIB", "NYSE"),       # Bancolombia / Cibest
        ("BAP", "NYSE"),       # Credicorp (Peru)
        ("ITUB", "NYSE"),      # Itau Unibanco (Brasil)
        ("JPM", "NYSE"),       # JPMorgan - referencia global
    ],
    "insurance": [
        ("CB", "NYSE"),        # Chubb
        ("TRV", "NYSE"),       # Travelers
        ("MAP.MC", "BME"),     # MAPFRE (Madrid)
    ],
    "generic": [
        ("AES", "NYSE"),       # AES Corp - opera generacion en Panama
        ("NEE", "NYSE"),       # NextEra Energy
    ],
}


def _sane(value, lo=0.05, hi=200.0):
    """yfinance devuelve multiplos absurdos para algunas clases de accion."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if lo < v < hi else None


def get_peer_metrics(kind):
    """Metricas de valoracion para el set de peers de un sector.

    Returns DataFrame: ticker, name, country, market, market_cap,
    pe, pb, roe_pct, div_yield_pct, profit_margin_pct.
    Un ticker caido no tumba la tabla (fila omitida).
    """
    rows = []
    for ticker, market in PEER_SETS.get(kind, PEER_SETS["generic"]):
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            continue
        if not info.get("shortName") and not info.get("longName"):
            continue
        roe = info.get("returnOnEquity")
        margin = info.get("profitMargins")
        rows.append({
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName"),
            "country": info.get("country", ""),
            "market": market,
            "market_cap": info.get("marketCap"),
            "pe": _sane(info.get("trailingPE")),
            "pb": _sane(info.get("priceToBook")),
            "roe_pct": round(roe * 100, 2) if roe is not None else None,
            "div_yield_pct": _sane(info.get("dividendYield"), lo=0.0, hi=30.0),
            "profit_margin_pct": round(margin * 100, 2) if margin is not None else None,
        })
    return pd.DataFrame(rows)


def comparison_table(local_row, kind):
    """Tabla comparativa con la empresa local en la primera fila.

    local_row: dict con ticker, name, market_cap, pe, pb, roe_pct,
    div_yield_pct (calculados con NUESTROS datos parseados de Latinex).
    """
    local = pd.DataFrame([{
        "ticker": local_row.get("ticker", ""),
        "name": local_row.get("name", ""),
        "country": "Panama",
        "market": "Latinex",
        "market_cap": local_row.get("market_cap"),
        "pe": local_row.get("pe"),
        "pb": local_row.get("pb"),
        "roe_pct": local_row.get("roe_pct"),
        "div_yield_pct": local_row.get("div_yield_pct"),
        "profit_margin_pct": local_row.get("profit_margin_pct"),
    }])
    peers = get_peer_metrics(kind)
    return pd.concat([local, peers], ignore_index=True)


if __name__ == "__main__":
    for kind in ["banking", "insurance", "generic"]:
        print(f"\n=== {kind} ===")
        df = get_peer_metrics(kind)
        with pd.option_context("display.float_format", "{:,.2f}".format, "display.width", 160):
            print(df.to_string(index=False))
