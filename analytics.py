# -*- coding: utf-8 -*-
"""
analytics.py -- investor-grade analytics computed from data already in the
snapshot (no API calls): total return with dividends reinvested, historical
valuation bands (P/E, P/B vs. the stock's own history), dividend
sustainability, liquidity scoring, and earnings-quality checks from the
cash-flow statement when available.

Everything degrades gracefully: any function returns a dict whose fields are
None when the underlying data is missing, so the UI can grey out sections.
"""

from datetime import datetime, timedelta

import pandas as pd


def _none_if_nan(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _fy_value(hist_table, metric, col):
    """Value of `metric` in column `col` of the 3-year history table."""
    if hist_table is None or hist_table.empty or col not in hist_table.columns:
        return None
    row = hist_table[hist_table["Metric"] == metric]
    if row.empty:
        return None
    return _none_if_nan(row.iloc[0][col])


# ---------------------------------------------------------------------------
# Total return (dividends reinvested)
# ---------------------------------------------------------------------------

def total_return(history, dividends):
    """Total-return index vs. price index.

    Reinvests each dividend into additional shares at the first close on/after
    the payment date. Returns dict with:
      series: DataFrame [date, price_idx, tr_idx]  (both start at 100)
      tr_1y/3y/5y_pct, pr_1y/3y/5y_pct: annualized total/price returns
      value_10k: what $10,000 at the series start is worth today (TR)
      div_cash_10k: cumulative dividend cash a non-reinvesting holder got
    """
    out = {"series": None, "tr_1y_pct": None, "tr_3y_pct": None, "tr_5y_pct": None,
           "pr_1y_pct": None, "pr_3y_pct": None, "pr_5y_pct": None,
           "value_10k": None, "div_cash_10k": None, "start": None}
    if history is None or history.empty:
        return out
    h = history.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if len(h) < 10:
        return out

    divs = pd.DataFrame()
    if dividends is not None and not dividends.empty:
        divs = dividends.dropna(subset=["payment_date", "amount"]).copy()
        divs = divs[divs["payment_date"] >= h["date"].iloc[0]].sort_values("payment_date")

    start_price = float(h["close"].iloc[0])
    shares = 1.0                       # reinvesting holder
    shares_flat = 1.0                  # non-reinvesting holder (for cash yield)
    cash = 0.0
    di = 0
    div_rows = list(divs.itertuples()) if not divs.empty else []
    tr_vals, pr_vals = [], []
    for row in h.itertuples():
        while di < len(div_rows) and div_rows[di].payment_date <= row.date:
            amt = float(div_rows[di].amount)
            shares += shares * amt / float(row.close)      # reinvest at close
            cash += shares_flat * amt
            di += 1
        tr_vals.append(shares * float(row.close))
        pr_vals.append(float(row.close))

    series = pd.DataFrame({"date": h["date"],
                           "price_idx": [v / start_price * 100 for v in pr_vals],
                           "tr_idx": [v / start_price * 100 for v in tr_vals]})
    out["series"] = series
    out["start"] = h["date"].iloc[0]
    out["value_10k"] = round(series["tr_idx"].iloc[-1] / 100 * 10_000)
    out["div_cash_10k"] = round(cash / start_price * 10_000)

    last_date = h["date"].iloc[-1]
    for years, key_tr, key_pr in [(1, "tr_1y_pct", "pr_1y_pct"),
                                  (3, "tr_3y_pct", "pr_3y_pct"),
                                  (5, "tr_5y_pct", "pr_5y_pct")]:
        cutoff = last_date - timedelta(days=int(365.25 * years))
        window = series[series["date"] >= cutoff]
        if len(window) < 10 or window["date"].iloc[0] > cutoff + timedelta(days=200):
            continue  # not enough history for this horizon
        yrs = (last_date - window["date"].iloc[0]).days / 365.25
        if yrs <= 0.5:
            continue
        for idx_col, key in [("tr_idx", key_tr), ("price_idx", key_pr)]:
            ratio = window[idx_col].iloc[-1] / window[idx_col].iloc[0]
            out[key] = round(((ratio ** (1 / yrs)) - 1) * 100, 1)
    return out


# ---------------------------------------------------------------------------
# Valuation bands (vs. the stock's OWN history)
# ---------------------------------------------------------------------------

def valuation_bands(history, hist_table, shares_outstanding, latest_is_quarterly=True):
    """Historical P/E and P/B ranges built from the price series and the FY
    fundamental anchors (FY2023/24/25 EPS & BVPS; current year uses the
    annualized latest quarter).

    Returns dict: pe {series, min, median, max, current, percentile}, pb {...}.
    Percentile = where today's multiple sits in its own history (0 = cheapest).
    """
    out = {"pe": None, "pb": None}
    if (history is None or history.empty or hist_table is None
            or hist_table.empty or not shares_outstanding):
        return out

    anchors = {}
    for year in (2023, 2024, 2025):
        ni = _fy_value(hist_table, "Net income", f"FY{year}")
        eq = _fy_value(hist_table, "Equity (controlling)", f"FY{year}")
        anchors[year] = {"eps": ni / shares_outstanding if ni else None,
                         "bvps": eq / shares_outstanding if eq else None}
    ni_l = _fy_value(hist_table, "Net income", "Latest")
    eq_l = _fy_value(hist_table, "Equity (controlling)", "Latest")
    ann = 4 if latest_is_quarterly else 1
    anchors["latest"] = {"eps": ni_l * ann / shares_outstanding if ni_l else None,
                         "bvps": eq_l / shares_outstanding if eq_l else None}

    h = history.dropna(subset=["close"]).sort_values("date")
    h = h[h["date"] >= pd.Timestamp("2023-01-01")]
    if h.empty:
        return out

    def anchor_for(ts):
        return anchors.get(ts.year, anchors["latest"]) if ts.year <= 2025 else anchors["latest"]

    for key, field in [("pe", "eps"), ("pb", "bvps")]:
        rows = []
        for r in h.itertuples():
            base = anchor_for(r.date).get(field)
            if base and base > 0:
                rows.append((r.date, float(r.close) / base))
        if len(rows) < 20:
            continue
        s = pd.DataFrame(rows, columns=["date", "mult"])
        current = s["mult"].iloc[-1]
        pct = float((s["mult"] < current).mean() * 100)
        out[key] = {"series": s, "min": round(float(s["mult"].min()), 2),
                    "median": round(float(s["mult"].median()), 2),
                    "max": round(float(s["mult"].max()), 2),
                    "current": round(float(current), 2),
                    "percentile": round(pct)}
    return out


# ---------------------------------------------------------------------------
# Dividend sustainability
# ---------------------------------------------------------------------------

def dividend_profile(dividends, price, hist_table, shares_outstanding,
                     latest_is_quarterly=True):
    """Dividend record and sustainability. Returns dict:
    ttm_dps, ttm_yield_pct, payout_pct (vs FY2025 EPS), per_year {year: dps},
    growth_streak_years, dps_cagr_3y_pct."""
    out = {"ttm_dps": None, "ttm_yield_pct": None, "payout_pct": None,
           "per_year": {}, "growth_streak_years": None, "dps_cagr_3y_pct": None}
    if dividends is None or dividends.empty:
        return out
    d = dividends.dropna(subset=["payment_date", "amount"]).copy()
    if d.empty:
        return out

    now = pd.Timestamp(datetime.now())
    ttm = d[d["payment_date"] >= now - timedelta(days=365)]
    out["ttm_dps"] = round(float(ttm["amount"].sum()), 4) if not ttm.empty else 0.0
    if price and out["ttm_dps"]:
        out["ttm_yield_pct"] = round(out["ttm_dps"] / price * 100, 2)

    d["year"] = d["payment_date"].dt.year
    per_year = d.groupby("year")["amount"].sum().to_dict()
    current_year = now.year
    out["per_year"] = {int(y): round(float(v), 4) for y, v in sorted(per_year.items())}

    # growth streak on COMPLETE years only
    years = [y for y in sorted(per_year) if y < current_year]
    streak = 0
    for i in range(len(years) - 1, 0, -1):
        if per_year[years[i]] > per_year[years[i - 1]]:
            streak += 1
        else:
            break
    out["growth_streak_years"] = streak
    if len(years) >= 4 and per_year[years[-4]] > 0:
        out["dps_cagr_3y_pct"] = round(
            ((per_year[years[-1]] / per_year[years[-4]]) ** (1 / 3) - 1) * 100, 1)

    ni25 = _fy_value(hist_table, "Net income", "FY2025") if hist_table is not None else None
    if ni25 and shares_outstanding and out["ttm_dps"]:
        eps25 = ni25 / shares_outstanding
        if eps25 > 0:
            out["payout_pct"] = round(out["ttm_dps"] / eps25 * 100, 1)
    return out


# ---------------------------------------------------------------------------
# Liquidity score
# ---------------------------------------------------------------------------

def liquidity_score(history, quote, depth, position_usd=100_000):
    """How hard is it to build/exit a position? Returns dict:
    adv_usd (90d), days_to_exit (at 20% participation), spread_pct,
    bid_depth_usd, grade ('A'..'F'), grade_reason."""
    out = {"adv_usd": None, "days_to_exit": None, "spread_pct": None,
           "bid_depth_usd": None, "grade": None, "grade_reason": ""}
    price = (quote or {}).get("price")

    if history is not None and not history.empty:
        h = history.sort_values("date")
        recent = h[h["date"] >= h["date"].iloc[-1] - timedelta(days=180)]
        if not recent.empty:
            # traded USD per calendar day over the window (zero-trade days count)
            days = max((recent["date"].iloc[-1] - recent["date"].iloc[0]).days, 1)
            total_usd = float(recent["amount"].fillna(0).sum())
            adv = total_usd / days * 7 / 5          # per trading day approx
            out["adv_usd"] = round(adv)
            if adv > 0:
                out["days_to_exit"] = round(position_usd / (0.2 * adv), 1)

    if depth:
        bids, asks = depth.get("bids") or [], depth.get("asks") or []
        if bids and asks and price:
            spread = asks[0][0] - bids[0][0]
            if spread >= 0:
                out["spread_pct"] = round(spread / price * 100, 2)
        if bids:
            out["bid_depth_usd"] = round(sum(p * q for p, q in bids))

    d2e = out["days_to_exit"]
    if d2e is None:
        out["grade"], out["grade_reason"] = "F", "No recent trading activity"
    elif d2e <= 5:
        out["grade"], out["grade_reason"] = "A", f"~{d2e:g} days to exit $100K"
    elif d2e <= 15:
        out["grade"], out["grade_reason"] = "B", f"~{d2e:g} days to exit $100K"
    elif d2e <= 45:
        out["grade"], out["grade_reason"] = "C", f"~{d2e:g} days to exit $100K"
    elif d2e <= 120:
        out["grade"], out["grade_reason"] = "D", f"~{d2e:g} days to exit $100K"
    else:
        out["grade"], out["grade_reason"] = "F", f"~{d2e:g} days to exit $100K"
    return out


# ---------------------------------------------------------------------------
# Earnings quality (cash-flow statement, when vision has extracted it)
# ---------------------------------------------------------------------------

def earnings_quality(fin):
    """Cash-conversion checks from the cash-flow statement. Returns dict:
    cfo, cfi, cff, capex, dividends_paid, net_income, cash_conversion_pct
    (CFO / net income), fcf (CFO - capex), div_coverage_x (CFO / dividends)."""
    out = {"cfo": None, "cfi": None, "cff": None, "capex": None,
           "dividends_paid": None, "net_income": None,
           "cash_conversion_pct": None, "fcf": None, "div_coverage_x": None}
    cf = (fin or {}).get("cashflow")
    if cf is None or (hasattr(cf, "empty") and cf.empty):
        return out
    periods = fin.get("periods") or []
    if not periods or periods[0] not in cf.columns:
        return out
    col = periods[0]
    factor = fin.get("scale_factor") or 1

    import financials as fm

    def find(include, exclude=()):
        for _, row in cf.iterrows():
            n = fm._norm(str(row["Line Item"]))
            if any(k in n for k in exclude):
                continue
            if all(k in n for k in include):
                v = _none_if_nan(row[col])
                if v is not None:
                    return float(v) * factor
        return None

    out["cfo"] = (find(["actividades de operacion"]) or find(["operacion", "neto"])
                  or find(["actividades operativas"]))
    out["cfi"] = find(["actividades de inversion"]) or find(["actividades de inversion", "neto"])
    out["cff"] = find(["actividades de financiamiento"]) or find(["financiamiento", "neto"])
    out["capex"] = (find(["adquisicion", "inmuebles"]) or find(["compra", "inmuebles"])
                    or find(["adquisicion", "propiedades"]) or find(["compras de activo fijo"]))
    out["dividends_paid"] = find(["dividendos pagados"]) or find(["pago de dividendos"])

    m = fm.extract_metrics(fin)
    ni = m.get("net_income")
    out["net_income"] = ni
    if out["cfo"] is not None and ni:
        out["cash_conversion_pct"] = round(out["cfo"] / ni * 100, 1)
    if out["cfo"] is not None and out["capex"] is not None:
        out["fcf"] = out["cfo"] - abs(out["capex"])
    if out["cfo"] is not None and out["dividends_paid"]:
        out["div_coverage_x"] = round(out["cfo"] / abs(out["dividends_paid"]), 2)
    return out


# ---------------------------------------------------------------------------
# "What changed" -- numeric deltas that feed the quarterly change narrative
# ---------------------------------------------------------------------------

def quarter_deltas(hist_table, latest_is_quarterly=True):
    """Numeric change picture: latest run-rate vs FY2025 and balance-sheet
    moves since year-end. Returns {metric: {latest, fy2025, delta_pct}}."""
    out = {}
    if hist_table is None or hist_table.empty:
        return out
    flow_metrics = {"Net income", "Net interest income", "Fees & commissions",
                    "Operating expenses", "Insurance revenue"}
    ann = 4 if latest_is_quarterly else 1
    for _, row in hist_table.iterrows():
        metric = row["Metric"]
        latest = _none_if_nan(row.get("Latest"))
        fy25 = _none_if_nan(row.get("FY2025"))
        if latest is None or not fy25:
            continue
        comparable = latest * ann if metric in flow_metrics else latest
        out[metric] = {"latest": comparable, "fy2025": fy25,
                       "delta_pct": round((comparable / fy25 - 1) * 100, 1)}
    return out
