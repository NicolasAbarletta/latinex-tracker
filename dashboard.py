# -*- coding: utf-8 -*-
"""
dashboard.py -- Latinex Equity Tracker (Streamlit).

Pages: Market | Company Deep Dive | Comparables | Export
English UI, professional light theme. Live data from Latinex (undocumented
JSON endpoints), financial statements parsed from filing PDFs (with OCR
fallback for scanned reports), McKinsey-style deep dive + ROE/DuPont tree,
international peers and a narrative generated with Claude.
"""

import io
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Bridge Streamlit Cloud secrets -> environment variables BEFORE importing the
# data modules (analyst reads ANTHROPIC_API_KEY at import time). Locally the
# .env file is used instead; this is a no-op when no secrets.toml exists.
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except Exception:
    pass

import latinex_api as api  # noqa: E402
import financials as fin_mod  # noqa: E402
import peers as peers_mod  # noqa: E402
import analyst  # noqa: E402
import analytics  # noqa: E402
from latinex_api import LatinexAPIError  # noqa: E402

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

# Public mode (website): visitors can't spend API credit or edit the watchlist.
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")


def is_admin():
    if not PUBLIC_MODE:
        return True
    return bool(ADMIN_KEY) and st.session_state.get("admin_key_input", "") == ADMIN_KEY


# Palette (mirrors design/index.html)
AZUL = "#0B3D66"
AZUL2 = "#11507F"
VERDE = "#15803D"
ROJO = "#B91C1C"
AMBER = "#B45309"
GRIS = "#94A3B8"

st.set_page_config(page_title="Latinex Equity Tracker", page_icon=":bank:",
                   layout="wide", initial_sidebar_state="expanded")

CSS = """
<style>
:root{
  --azul:#0B3D66; --azul2:#11507F; --soft:#DBEAFE; --tint:#EEF4FA;
  --line:#E2E8F0; --line2:#EDF2F7; --ink:#1A202C; --ink2:#475569;
  --muted:#64748B; --muted2:#94A3B8; --verde:#15803D; --verdebg:#DCFCE7;
  --rojo:#B91C1C; --rojobg:#FEE2E2; --amber:#B45309; --amberbg:#FEF3C7;
}
html, body, [class*="css"]{font-variant-numeric:tabular-nums;}
.block-container{padding-top:2.2rem; padding-bottom:4rem; max-width:1500px;}
h1, h2, h3{color:var(--azul);}
h1{font-weight:800; letter-spacing:-.01em;}
h3{border-bottom:2px solid var(--soft); padding-bottom:6px;}

/* metric cards -> KPI strip look */
[data-testid="stMetric"]{
  background:#fff; border:1px solid var(--line); border-radius:14px;
  padding:14px 16px 12px; box-shadow:0 1px 3px rgba(11,61,102,.06);
  position:relative; overflow:hidden;
}
[data-testid="stMetric"]::before{content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--azul); opacity:.85;}
[data-testid="stMetricLabel"]{color:var(--muted); font-weight:600; white-space:normal;}
[data-testid="stMetricValue"]{font-weight:800; color:var(--ink); font-size:1.6rem; line-height:1.15;}
[data-testid="stMetricDelta"]{font-size:.82rem;}

/* tabs */
.stTabs [data-baseweb="tab-list"]{gap:4px; background:#fff; border:1px solid var(--line); border-radius:11px; padding:4px; box-shadow:0 1px 3px rgba(11,61,102,.06);}
.stTabs [data-baseweb="tab"]{height:auto; padding:8px 16px; border-radius:8px; font-weight:600; color:var(--ink2);}
.stTabs [aria-selected="true"]{background:var(--azul); color:#fff !important;}
.stTabs [data-baseweb="tab-highlight"]{display:none;}

/* generic card + callout used by custom HTML blocks */
.lx-card{background:#fff; border:1px solid var(--line); border-radius:14px; box-shadow:0 1px 3px rgba(11,61,102,.06);}
.lx-callout{display:flex; gap:12px; align-items:flex-start; background:linear-gradient(180deg,#FBFDFF,#F4F8FC);
  border:1px solid var(--line2); border-radius:12px; padding:16px 18px; font-size:13px; color:var(--ink2); line-height:1.55;}
.lx-callout .ic{flex:0 0 30px; width:30px; height:30px; border-radius:8px; background:var(--soft); color:var(--azul); display:grid; place-items:center; font-weight:800; font-size:15px;}
.lx-callout b{color:var(--azul);}

/* deep-dive header */
.dd-head{display:flex; justify-content:space-between; align-items:flex-start; gap:16px; padding:18px 22px; margin-bottom:4px;}
.dd-head .tk{font-size:13px; font-weight:800; color:#fff; background:var(--azul); border-radius:9px; padding:8px 11px;}
.dd-head .nm{font-size:19px; font-weight:800; color:var(--ink);}
.dd-head .cap{font-size:12px; color:var(--muted); margin-top:2px;}
.verdict{display:inline-flex; align-items:center; gap:8px; font-weight:800; font-size:12.5px; padding:8px 13px; border-radius:10px;}
.v-good{background:var(--verdebg); color:var(--verde);} .v-bad{background:var(--rojobg); color:var(--rojo);} .v-neutral{background:var(--tint); color:var(--azul);}

/* scorecard */
.scorecard{display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin:4px 0 6px;}
.score{background:#fff; border:1px solid var(--line); border-radius:14px; box-shadow:0 1px 3px rgba(11,61,102,.06); padding:15px 16px;}
.score .l{font-size:11px; color:var(--muted); font-weight:600;}
.score .row2{display:flex; align-items:baseline; justify-content:space-between; margin-top:8px;}
.score .grade{font-size:22px; font-weight:800;} .score .num{font-size:12px; color:var(--muted2); font-weight:700;}
.score .meter{height:7px; background:#EEF2F7; border-radius:5px; overflow:hidden; margin-top:10px;}
.score .meter>span{display:block; height:100%; border-radius:5px;}
.score .rat{font-size:11px; color:var(--muted); margin-top:8px; line-height:1.4;}

/* strengths / weaknesses */
.sw-grid{display:grid; grid-template-columns:1fr 1fr; gap:18px;}
.sw{background:#fff; border:1px solid var(--line); border-radius:14px; box-shadow:0 1px 3px rgba(11,61,102,.06); overflow:hidden;}
.sw h4{margin:0; padding:14px 18px; font-size:14px; display:flex; align-items:center; gap:9px; border-bottom:1px solid var(--line2);}
.sw h4 .dot{width:9px; height:9px; border-radius:50%;}
.sw ul{list-style:none; margin:0; padding:8px 10px 14px;}
.sw li{display:flex; gap:11px; padding:9px 10px; border-radius:9px; font-size:13px; line-height:1.45; color:var(--ink2);}
.sw li .ic{flex:0 0 20px; width:20px; height:20px; border-radius:50%; display:grid; place-items:center; font-size:12px; font-weight:800; margin-top:1px;}
.sw li b{color:var(--ink);}
.good-ic{background:var(--verdebg); color:var(--verde);} .bad-ic{background:var(--amberbg); color:var(--amber);}

/* order book ladder */
.ob-head{display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--line2);}
.ob-head .sel{display:flex; align-items:center; gap:10px;}
.ob-head .tk{font-size:11px; font-weight:800; color:#fff; background:var(--azul); border-radius:6px; padding:4px 8px;}
.ob-head .nm{font-weight:700; color:var(--ink);}
.ob-spread{display:flex; gap:18px; font-size:11px; color:var(--muted);} .ob-spread b{color:var(--ink);}
.ladder{display:grid; grid-template-columns:1fr 1fr;}
.ladder .sh{font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; font-weight:700; padding:10px 14px;}
.ladder .bidc .sh{color:var(--verde);} .ladder .askc .sh{color:var(--rojo);} .ladder .askc{border-left:1px solid var(--line2);}
.lvl{position:relative; display:flex; justify-content:space-between; align-items:center; padding:7px 14px; font-size:12px; z-index:1;}
.lvl .px{font-weight:700;} .lvl .qty{color:var(--muted2); font-size:11px;}
.lvl .fill{position:absolute; top:1px; bottom:1px; z-index:-1; border-radius:4px;}
.bidc .lvl .px{color:var(--verde);} .bidc .lvl .fill{right:0; background:var(--verdebg);}
.askc .lvl .px{color:var(--rojo);} .askc .lvl .fill{left:0; background:var(--rojobg);}
.pos{color:var(--verde); font-weight:700;} .neg{color:var(--rojo); font-weight:700;}

/* analysis box */
.analysis-box{background:#fff; border:1px solid var(--line); border-left:4px solid var(--azul); border-radius:12px; padding:20px 24px; box-shadow:0 1px 3px rgba(11,61,102,.06);}
.analysis-box h2{font-size:1.1rem; border:none; margin-top:.8em;}

/* sidebar brand */
.lx-brand{display:flex; gap:11px; align-items:center; margin-bottom:6px;}
.lx-brand .mark{width:36px; height:36px; border-radius:9px; background:linear-gradient(145deg,var(--azul),var(--azul2)); color:#fff; display:grid; place-items:center; font-weight:800; font-size:17px;}
.lx-brand b{color:var(--azul); font-size:14.5px; line-height:1.15; font-weight:800; display:block;}
.lx-brand span{color:var(--muted); font-size:11px;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def html(s):
    """Render an HTML block, escaping '$' so Streamlit doesn't treat it as LaTeX."""
    st.markdown(s.replace("$", "&#36;"), unsafe_allow_html=True)


def style_fig(fig, height=380, title=None):
    fig.update_layout(
        template="plotly_white", height=height,
        margin=dict(l=10, r=10, t=44 if title else 16, b=10), title=title,
        font=dict(family="Segoe UI, sans-serif", color="#1A202C"),
        title_font=dict(color=AZUL, size=15),
        plot_bgcolor="#FFFFFF", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.08),
    )
    return fig


# ---------------------------------------------------------------------------
# Precomputed snapshot (built offline by build_snapshot.py). When present, the
# fetchers serve from it instantly; otherwise they fall back to live calls.
# ---------------------------------------------------------------------------

import snapshot as _snap_mod

SNAP = _snap_mod.load() or {}


def snapshot_built_at():
    return SNAP.get("built_at")


def _tk(nemo):
    return SNAP.get("tickers", {}).get(nemo, {})


def verified_companies():
    """Tickers whose financials were read by Claude vision and parsed cleanly --
    the set we trust enough to surface in the Deep Dive picker."""
    out = []
    for tk, e in SNAP.get("tickers", {}).items():
        fin = e.get("financials") or {}
        m = fin_mod.extract_metrics(fin) if not fin.get("error") else {}
        if fin.get("vision_used") and not fin.get("error") and m.get("net_income") is not None:
            out.append(tk)
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def deep_analytics(nemo):
    """All snapshot-derived analytics for one company (no API calls)."""
    e = _tk(nemo)
    q, s = e.get("quote") or {}, e.get("summary") or {}
    fin = e.get("financials") or {}
    h, div = e.get("history_all"), e.get("dividends")
    ht = (e.get("historical") or {}).get("table")
    quarterly = fin.get("is_quarterly", True)
    return {
        "tr": analytics.total_return(h, div),
        "vb": analytics.valuation_bands(h, ht, s.get("shares_outstanding"), quarterly),
        "dp": analytics.dividend_profile(div, q.get("price"), ht,
                                         s.get("shares_outstanding"), quarterly),
        "lq": analytics.liquidity_score(h, q, e.get("order_book_depth")),
        "eq": analytics.earnings_quality(fin),
        "deltas": analytics.quarter_deltas(ht, quarterly),
    }


def _have(val):
    """True if a snapshot value is actually populated (not None/empty)."""
    if val is None:
        return False
    if isinstance(val, pd.DataFrame):
        return not val.empty
    return True


def _slice_history(df, rango):
    if df is None or df.empty or rango == "ALL":
        return df
    days = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "5Y": 1825}.get(rango)
    if not days:
        return df
    cutoff = pd.Timestamp(datetime.now() - timedelta(days=days))
    return df[df["date"] >= cutoff].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Cached fetchers (snapshot first, live fallback)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def universe(include_preferred):
    u = SNAP.get("universe", {})
    key = "preferred" if include_preferred else "common"
    if _have(u.get(key)):
        return u[key]
    return api.get_equity_universe(include_preferred=include_preferred)


@st.cache_data(ttl=300, show_spinner=False)
def quote(nemo):
    return _tk(nemo).get("quote") or api.get_quote(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def summary(nemo):
    return _tk(nemo).get("summary") or api.get_summary(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def history(nemo, rango):
    h = _tk(nemo).get("history_all")
    if _have(h):
        return _slice_history(h, rango)
    return api.get_history(nemo, rango)


@st.cache_data(ttl=300, show_spinner=False)
def dividends(nemo):
    d = _tk(nemo).get("dividends")
    return d if d is not None else api.get_dividends(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def documents(issuer_code):
    return api.get_documents(issuer_code)


@st.cache_data(ttl=300, show_spinner=False)
def documents_for(nemo, issuer_code):
    d = _tk(nemo).get("documents")
    if d is not None:
        return d
    return api.get_documents(issuer_code) if issuer_code else pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def notices(issuer_name):
    return api.get_notices(issuer_filter=issuer_name)


@st.cache_data(ttl=300, show_spinner=False)
def notices_for(nemo, issuer_name):
    n = _tk(nemo).get("notices")
    if n is not None:
        return n
    return api.get_notices(issuer_filter=issuer_name)


@st.cache_data(ttl=120, show_spinner=False)
def order_book():
    ob = SNAP.get("order_book")
    return ob if _have(ob) else api.get_order_book()


@st.cache_data(ttl=120, show_spinner=False)
def order_book_depth(nemo):
    d = _tk(nemo).get("order_book_depth")
    return d if d is not None else api.get_order_book_depth(nemo)


@st.cache_data(ttl=1800, show_spinner=False)
def financials(nemo):
    f = _tk(nemo).get("financials")
    return f if f is not None else fin_mod.get_financials(nemo)


@st.cache_data(ttl=86400, show_spinner=False)
def historical(nemo):
    h = _tk(nemo).get("historical")
    return h if h is not None else fin_mod.get_historical(nemo)


@st.cache_data(ttl=1800, show_spinner=False)
def dupont(nemo, cache_key, kind):
    return fin_mod.dupont_decomposition(financials(nemo), kind)


@st.cache_data(ttl=3600, show_spinner=False)
def peer_metrics(kind):
    p = SNAP.get("peers", {}).get(kind)
    return p if _have(p) else peers_mod.get_peer_metrics(kind)


@st.cache_data(ttl=86400, show_spinner=False)
def index_history():
    ih = SNAP.get("index_history")
    return ih if _have(ih) else api.get_index_history("1Y")


# Deep-dive reports are PRELOADED from the snapshot and shown read-only. The app
# never generates them on page load. _deep_dive_live exists only for the optional
# local-admin "Regenerate" button (a single manual API call); reports are normally
# built offline by build_snapshot.py / fix_company.py.
@st.cache_data(ttl=86400, show_spinner=False)
def _deep_dive_live(nemo, cache_key):
    return analyst.generate_deep_dive(nemo)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_watchlist():
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            wl = json.load(f)
            return wl if isinstance(wl, list) else ["ASSA", "BGFG", "EGIN"]
    except (OSError, ValueError):
        return ["ASSA", "BGFG", "EGIN"]


def save_watchlist(tickers):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(tickers), f, indent=2)


def fmt(val, pattern="{:,.2f}", dash="-"):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return dash
    return pattern.format(val)


def fmt_money_compact(val, dash="-"):
    """$21.2B / $844.6M / $451K -- fits metric cards and reads faster."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return dash
    sign = "-" if val < 0 else ""
    a = abs(val)
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.0f}K"
    return f"{sign}${a:,.0f}"


def money_style(df, money_cols=None, decimals=0):
    cols = money_cols or [c for c in df.columns if df[c].dtype.kind in "fi"]
    return df.style.format({c: ("${:,.%df}" % decimals).format for c in cols}, na_rep="-")


def local_metrics_row(nemo):
    q = quote(nemo)
    s = summary(nemo)
    fin = financials(nemo)
    r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
    divs = dividends(nemo)
    y = api.get_dividend_yield(divs, q["price"])
    return {"ticker": nemo, "name": q["issuer_name"], "market_cap": q["market_cap"],
            "pe": r["pe"], "pb": r["pb"], "roe_pct": r["roe_pct"],
            "div_yield_pct": y["total_yield_pct"], "profit_margin_pct": None}


def peers_display(df, local_tickers):
    show = df.rename(columns={
        "ticker": "Ticker", "name": "Name", "country": "Country", "market": "Market",
        "market_cap": "Market cap", "pe": "P/E", "pb": "P/B", "roe_pct": "ROE %",
        "div_yield_pct": "Div yield %", "profit_margin_pct": "Margin %"})

    def highlight(row):
        if row["Ticker"] in local_tickers:
            return ["background-color: #DBEAFE; font-weight: 600;"] * len(row)
        return [""] * len(row)

    return (show.style.apply(highlight, axis=1)
            .format({"Market cap": lambda v: fmt_money_compact(v),
                     "P/E": lambda v: fmt(v), "P/B": lambda v: fmt(v),
                     "ROE %": lambda v: fmt(v), "Div yield %": lambda v: fmt(v),
                     "Margin %": lambda v: fmt(v)}, na_rep="-"))


# ---------------------------------------------------------------------------
# Custom HTML builders (deep dive + order book)
# ---------------------------------------------------------------------------

def order_book_html(depth, last_price, ticker, name):
    bids, asks = depth["bids"], depth["asks"]
    if not bids and not asks:
        return ("<div class='lx-card' style='padding:18px;color:#64748B'>"
                f"No open orders for {ticker} right now.</div>")
    qtys = [q for _, q in bids + asks] or [1]
    maxq = max(qtys) or 1
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None
    spread_pct = (spread / last_price * 100) if (spread and last_price) else None
    imb = depth.get("imbalance_pct")

    def levels_html(levels, side):
        out = []
        for px, q in levels:
            w = int(q / maxq * 100)
            if side == "bid":
                out.append(f"<div class='lvl'><span class='fill' style='width:{w}%'></span>"
                           f"<span class='px'>${px:,.2f}</span><span class='qty'>{q:,.0f}</span></div>")
            else:
                out.append(f"<div class='lvl'><span class='fill' style='width:{w}%'></span>"
                           f"<span class='qty'>{q:,.0f}</span><span class='px'>${px:,.2f}</span></div>")
        return "".join(out) or "<div class='lvl'><span class='qty'>—</span></div>"

    spread_txt = (f"${spread:,.2f} · {spread_pct:.2f}%" if spread is not None else "—")
    imb_cls = "pos" if (imb or 0) >= 0 else "neg"
    imb_txt = (f"{'+' if (imb or 0) >= 0 else ''}{imb}% "
               f"{'bid' if (imb or 0) >= 0 else 'ask'}") if imb is not None else "—"
    return f"""
<div class="lx-card">
  <div class="ob-head">
    <div class="sel"><span class="tk">{ticker}</span><span class="nm">{name}</span></div>
    <div class="ob-spread">
      <span>Last <b>${fmt(last_price)}</b></span>
      <span>Spread <b>{spread_txt}</b></span>
      <span>Imbalance <b class="{imb_cls}">{imb_txt}</b></span>
    </div>
  </div>
  <div class="ladder">
    <div class="bidc"><div class="sh">Bids (demand)</div>{levels_html(bids, "bid")}</div>
    <div class="askc"><div class="sh">Offers (supply)</div>{levels_html(asks, "ask")}</div>
  </div>
</div>"""


def scorecard_html(scorecard):
    def color(n):
        return VERDE if n >= 80 else AZUL if n >= 65 else AMBER if n >= 50 else ROJO
    cells = []
    for s in scorecard:
        n = int(s.get("score", 0) or 0)
        cells.append(
            f"<div class='score'><div class='l'>{s.get('dimension','')}</div>"
            f"<div class='row2'><span class='grade' style='color:{color(n)}'>{s.get('grade','')}</span>"
            f"<span class='num'>{n}/100</span></div>"
            f"<div class='meter'><span style='width:{n}%;background:{color(n)}'></span></div>"
            f"<div class='rat'>{s.get('rationale','')}</div></div>")
    return "<div class='scorecard'>" + "".join(cells) + "</div>"


def sw_html(strengths, weaknesses):
    def items(lst, ic_cls, mark):
        out = []
        for it in lst:
            out.append(f"<li><span class='ic {ic_cls}'>{mark}</span>"
                       f"<div><b>{it.get('title','')}.</b> {it.get('detail','')}</div></li>")
        return "".join(out)
    return f"""
<div class="sw-grid">
  <div class="sw"><h4><span class="dot" style="background:{VERDE}"></span>What's working</h4>
    <ul>{items(strengths, 'good-ic', '✓')}</ul></div>
  <div class="sw"><h4><span class="dot" style="background:{AMBER}"></span>What needs watching</h4>
    <ul>{items(weaknesses, 'bad-ic', '!')}</ul></div>
</div>"""


def _stars_txt(n):
    return ("★" * n + "☆" * (5 - n)) if n else "—"


_RATING_COLORS = {"Wide": VERDE, "Narrow": AZUL, "None": GRIS,
                  "Low": VERDE, "Medium": AZUL, "High": AMBER, "Very High": ROJO,
                  "Exemplary": VERDE, "Standard": AZUL, "Poor": ROJO}


def report_header_html(rep):
    fv = rep.get("fair_value") or {}
    price = rep.get("price_at_report")
    p_fv = round(price / fv["mid"], 2) if (price and fv.get("mid")) else None
    moat = (rep.get("moat") or {}).get("rating", "—")
    unc = (rep.get("uncertainty") or {}).get("rating", "—")
    ca = (rep.get("capital_allocation") or {}).get("rating", "—")

    def cell(label, value, color=AZUL, sub=""):
        return (f"<div style='flex:1;min-width:130px;background:#fff;border:1px solid #E2E8F0;"
                f"border-radius:12px;padding:12px 14px;box-shadow:0 1px 3px rgba(11,61,102,.06)'>"
                f"<div style='font-size:10.5px;color:#64748B;font-weight:700;text-transform:uppercase;"
                f"letter-spacing:.04em'>{label}</div>"
                f"<div style='font-size:19px;font-weight:800;color:{color};margin-top:5px'>{value}</div>"
                f"<div style='font-size:10.5px;color:#94A3B8;margin-top:3px'>{sub}</div></div>")

    cells = [
        cell("Fair value est.", f"${fv.get('mid', '—')}",
             AZUL, f"range ${fv.get('low', '—')} – ${fv.get('high', '—')}"),
        cell("Price / Fair value", p_fv if p_fv is not None else "—",
             (ROJO if (p_fv or 0) > 1.15 else VERDE if (p_fv or 9) < 0.85 else AZUL),
             f"price ${price}"),
        cell("Rating", _stars_txt(rep.get("stars")), "#B45309", "price vs. fair value"),
        cell("Economic moat", moat, _RATING_COLORS.get(moat, AZUL), ""),
        cell("Uncertainty", unc, _RATING_COLORS.get(unc, AZUL), ""),
        cell("Capital allocation", ca, _RATING_COLORS.get(ca, AZUL), ""),
    ]
    return ("<div style='display:flex;gap:12px;flex-wrap:wrap;margin:14px 0 6px'>"
            + "".join(cells) + "</div>")


def bulls_bears_html(bulls, bears):
    def items(lst, ic_cls, mark):
        return "".join(f"<li><span class='ic {ic_cls}'>{mark}</span><div>{b}</div></li>"
                       for b in lst)
    return f"""
<div class="sw-grid">
  <div class="sw"><h4><span class="dot" style="background:{VERDE}"></span>Bulls say</h4>
    <ul>{items(bulls, 'good-ic', '▲')}</ul></div>
  <div class="sw"><h4><span class="dot" style="background:{ROJO}"></span>Bears say</h4>
    <ul>{items(bears, 'bad-ic', '▼')}</ul></div>
</div>"""


def build_tear_sheet(nemo, e, rep, an):
    """Self-contained, print-ready HTML report (browser -> print -> PDF)."""
    q = e.get("quote") or {}
    fv = rep.get("fair_value") or {}
    tr, dp, lq, eq_, vb = an["tr"], an["dp"], an["lq"], an["eq"], an["vb"]
    ht = (e.get("historical") or {}).get("table")
    ni_years = ""
    if ht is not None:
        row = ht[ht["Metric"] == "Net income"]
        if not row.empty:
            cells = "".join(
                f"<td style='text-align:right'>{fmt_money_compact(row.iloc[0][c])}</td>"
                for c in ht.columns if str(c).startswith("FY"))
            heads = "".join(f"<th style='text-align:right'>{c}</th>"
                            for c in ht.columns if str(c).startswith("FY"))
            ni_years = f"<tr><th>Net income</th>{heads}</tr><tr><td></td>{cells}</tr>"
    segs = "".join(
        f"<li><b>{s.get('name')}</b>"
        + (f" ({s['revenue_share_pct']}% of revenue)" if s.get("revenue_share_pct") else "")
        + f" — {s.get('detail', '')}</li>" for s in rep.get("segments", []))
    comps = "".join(
        f"<li><b>{c.get('area')}</b>: {', '.join(c.get('names', []))} — {c.get('positioning', '')}</li>"
        for c in rep.get("competitors", []))
    bulls = "".join(f"<li>{b}</li>" for b in rep.get("bulls_say", []))
    bears = "".join(f"<li>{b}</li>" for b in rep.get("bears_say", []))
    wc = (e.get("whats_changed") or {}).get("text", "")
    from datetime import datetime as _dt
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{nemo} — Company Report</title>
<style>
 body{{font-family:'Segoe UI',Arial,sans-serif;color:#1A202C;max-width:840px;margin:24px auto;padding:0 18px;font-size:13.5px;line-height:1.55}}
 h1{{color:#0B3D66;margin-bottom:2px}} h2{{color:#0B3D66;border-bottom:2px solid #DBEAFE;padding-bottom:4px;margin-top:26px;font-size:17px}}
 .grid{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}}
 .cell{{flex:1;min-width:120px;border:1px solid #E2E8F0;border-radius:10px;padding:10px 12px}}
 .cell .l{{font-size:10px;color:#64748B;text-transform:uppercase;font-weight:700}}
 .cell .v{{font-size:17px;font-weight:800;color:#0B3D66;margin-top:3px}}
 table{{border-collapse:collapse;width:100%}} th,td{{padding:6px 10px;border-bottom:1px solid #EDF2F7;font-size:12.5px}}
 ul{{padding-left:20px}} li{{margin-bottom:5px}}
 .muted{{color:#64748B;font-size:11px}}
 @media print {{ body{{margin:8px auto}} }}
</style></head><body>
<h1>{q.get('issuer_name') or nemo} ({nemo})</h1>
<div class="muted">Latinex · Panama · report generated {_dt.now().strftime('%Y-%m-%d')} · source filing: {rep.get('source_report', '')}</div>
<div class="grid">
 <div class="cell"><div class="l">Price</div><div class="v">${q.get('price')}</div></div>
 <div class="cell"><div class="l">Fair value</div><div class="v">${fv.get('mid', '—')}</div><div class="muted">${fv.get('low')} – ${fv.get('high')}</div></div>
 <div class="cell"><div class="l">Rating</div><div class="v">{_stars_txt(rep.get('stars'))}</div></div>
 <div class="cell"><div class="l">Moat</div><div class="v">{(rep.get('moat') or {}).get('rating', '—')}</div></div>
 <div class="cell"><div class="l">Uncertainty</div><div class="v">{(rep.get('uncertainty') or {}).get('rating', '—')}</div></div>
 <div class="cell"><div class="l">Capital allocation</div><div class="v">{(rep.get('capital_allocation') or {}).get('rating', '—')}</div></div>
</div>
<h2>Business</h2><p>{rep.get('business_description', '')}</p>
<h3>Lines of business</h3><ul>{segs}</ul>
<h3>Competition <span class="muted">(analyst market knowledge, not from filings)</span></h3><ul>{comps}</ul>
<h2>Analyst thesis</h2><p>{rep.get('thesis', '')}</p>
{f'<p><b>What changed this quarter:</b> {wc}</p>' if wc else ''}
<h2>Bulls say</h2><ul>{bulls}</ul>
<h2>Bears say</h2><ul>{bears}</ul>
<h2>Fair value methodology</h2><p>{fv.get('methods', '')}</p>
<h2>Key figures</h2>
<table>{ni_years}</table>
<div class="grid">
 <div class="cell"><div class="l">ROE</div><div class="v">{(rep.get('anchors') or {}).get('roe_pct', '—')}%</div></div>
 <div class="cell"><div class="l">P/E</div><div class="v">{(rep.get('anchors') or {}).get('pe_now', '—')}x</div></div>
 <div class="cell"><div class="l">Yield (TTM)</div><div class="v">{dp.get('ttm_yield_pct', '—')}%</div></div>
 <div class="cell"><div class="l">Cash conversion</div><div class="v">{eq_.get('cash_conversion_pct', '—')}%</div></div>
 <div class="cell"><div class="l">Total return 1y</div><div class="v">{tr.get('tr_1y_pct', '—')}%</div></div>
 <div class="cell"><div class="l">Liquidity</div><div class="v">{lq.get('grade', '—')}</div></div>
</div>
<p class="muted">Figures parsed from audited Latinex filings via Claude vision; analytics computed from prices and dividend history. Fair value and ratings are model-generated estimates, not investment advice. Verify against source PDFs before acting.</p>
</body></html>"""


def roe_tree_svg(d, peer_roe_median=None):
    """Build the ROE/DuPont tree as inline SVG from a dupont_decomposition dict."""
    def v(x, suf="%"):
        return "n/a" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{x:g}{suf}"

    # tone heuristics (defensible): ROE vs peers; cost ratios by convention.
    roe_tone = "neutral"
    roe_delta = ""
    if d.get("roe_pct") is not None and peer_roe_median is not None:
        diff = d["roe_pct"] - peer_roe_median
        roe_tone = "good" if diff >= 0 else "bad"
        roe_delta = f"{'+' if diff >= 0 else ''}{diff:.1f}pp vs peers"
    ci = d.get("cost_income_pct")
    ci_tone = "good" if (ci is not None and ci < 50) else "bad" if (ci is not None and ci > 65) else "neutral"
    cor = d.get("cost_of_risk_pct")
    cor_tone = "good" if (cor is not None and cor < 1.0) else "bad" if (cor is not None and cor > 2.0) else "neutral"

    nodes = {
        "roe": (630, 36, 170, 62, "ROE", v(d.get("roe_pct")), roe_delta, roe_tone, True),
        "roa": (300, 170, 160, 58, "ROA", v(d.get("roa_pct")), "", "neutral", False),
        "lev": (840, 170, 160, 58, "Leverage (assets/equity)", v(d.get("leverage_x"), "x"), "", "neutral", False),
        "nm": (560, 300, 160, 58, "Net profit margin", v(d.get("net_margin_pct")), "", "neutral", False),
        "ay": (170, 300, 160, 58, "Asset yield (rev/assets)", v(d.get("asset_yield_pct")), "", "neutral", False),
        "nim": (90, 438, 150, 62, "Net interest margin", v(d.get("nim_pct")), "", "neutral", False),
        "fee": (255, 438, 150, 62, "Fee income / assets", v(d.get("fee_to_assets_pct")), "", "neutral", False),
        "ci": (470, 438, 150, 62, "Cost / income", v(ci), "", ci_tone, False),
        "cor": (645, 438, 150, 62, "Cost of risk", v(cor), "", cor_tone, False),
        "tax": (820, 438, 150, 62, "Effective tax", v(d.get("effective_tax_pct")), "", "neutral", False),
    }
    edges = [("roe", "roa"), ("roe", "lev"), ("roa", "nm"), ("roa", "ay"),
             ("nm", "ci"), ("nm", "cor"), ("nm", "tax"), ("ay", "nim"), ("ay", "fee")]
    stroke = {"good": "#15803D", "bad": "#B91C1C", "neutral": "#0B3D66"}
    bg = {"good": "#F0FBF3", "bad": "#FEF4F4", "neutral": "#F4F8FC"}

    parts = ['<svg viewBox="0 0 1000 510" width="100%" style="min-width:900px;font-family:Segoe UI,sans-serif">']
    for a, b in edges:
        pa, pb_ = nodes[a], nodes[b]
        x1, y1 = pa[0], pa[1] + pa[3]
        x2, y2 = pb_[0], pb_[1]
        my = (y1 + y2) / 2
        parts.append(f'<path d="M {x1} {y1} L {x1} {my} L {x2} {my} L {x2} {y2}" '
                     f'fill="none" stroke="#CBD5E1" stroke-width="1.6"/>')
    # operators
    parts.append(f'<text x="{(nodes["roa"][0]+nodes["lev"][0])/2}" y="{nodes["roa"][1]+34}" '
                 f'text-anchor="middle" font-size="20" font-weight="800" fill="#94A3B8">×</text>')
    parts.append(f'<text x="{(nodes["ay"][0]+nodes["nm"][0])/2}" y="{nodes["ay"][1]+34}" '
                 f'text-anchor="middle" font-size="20" font-weight="800" fill="#94A3B8">×</text>')
    for cx, y, w, h, title, val, delta, tone, big in nodes.values():
        x = cx - w / 2
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="11" fill="{bg[tone]}" '
                     f'stroke="{stroke[tone]}" stroke-width="{2.4 if big else 1.6}"/>')
        parts.append(f'<text x="{cx}" y="{y+18}" text-anchor="middle" font-size="10.5" '
                     f'font-weight="700" fill="#64748B">{title}</text>')
        parts.append(f'<text x="{cx}" y="{y+(44 if big else 40)}" text-anchor="middle" '
                     f'font-size="{24 if big else 20}" font-weight="800" fill="{stroke[tone]}">{val}</text>')
        if delta:
            parts.append(f'<text x="{cx}" y="{y+h-9}" text-anchor="middle" font-size="10" '
                         f'font-weight="700" fill="{stroke[tone]}">{delta}</text>')
    parts.append(f'<text x="{nodes["roe"][0]}" y="{nodes["roe"][1]+nodes["roe"][3]+22}" '
                 f'text-anchor="middle" font-size="11" fill="#94A3B8">'
                 f'ROE = ROA × Leverage      ·      ROA = Net margin × Asset yield</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar():
    st.sidebar.markdown(
        "<div class='lx-brand'><div class='mark'>L</div><div>"
        "<b>Latinex Equity Tracker</b><span>Latin American Stock Exchange · Panama</span>"
        "</div></div>", unsafe_allow_html=True)

    if is_admin():
        if st.sidebar.button("Refresh data", width="stretch"):
            st.cache_data.clear()
            st.session_state["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.rerun()
    built = snapshot_built_at()
    if built:
        st.sidebar.caption(f"📦 Precomputed snapshot · built {built[:16].replace('T', ' ')}")
    st.sidebar.caption(f"Last update: {st.session_state.get('last_refresh', 'this session')} "
                       "· prices delayed up to 5 min")

    st.sidebar.divider()
    st.sidebar.subheader("Watchlist")
    wl_current = load_watchlist()
    if is_admin():
        try:
            all_tickers = universe(False)["ticker"].tolist()
        except LatinexAPIError:
            all_tickers = wl_current
        wl_selected = st.sidebar.multiselect(
            "Companies with financial analysis",
            options=sorted(set(all_tickers) | set(wl_current)), default=wl_current)
        if sorted(wl_selected) != sorted(wl_current) and wl_selected:
            save_watchlist(wl_selected)
            st.sidebar.success("Watchlist saved")
    else:
        st.sidebar.caption("Companies with financial analysis: " + ", ".join(wl_current))

    st.sidebar.divider()
    st.sidebar.caption(f"Analysis generated with {analyst.MODEL}")
    if PUBLIC_MODE:
        with st.sidebar.expander("Administrator"):
            st.text_input("Key", type="password", key="admin_key_input")


# ---------------------------------------------------------------------------
# Page 1: Market
# ---------------------------------------------------------------------------

def page_market():
    st.title("Panama equity market")
    st.caption("Live snapshot of every Latinex-listed common stock, with the current "
               "order book (bids & offers) so you can see how volume is building.")

    try:
        idx = index_history()
    except LatinexAPIError:
        idx = pd.DataFrame()
    include_pref = st.toggle("Include preferred shares", value=False)
    try:
        uni = universe(include_pref)
    except LatinexAPIError as e:
        st.error(f"Could not load the equity universe: {e}")
        return

    movers = uni.dropna(subset=["ytd"]).copy()
    movers["ytd_pct"] = movers["ytd"] * 100
    nonzero = movers[movers["ytd_pct"] != 0]

    c1, c2, c3, c4 = st.columns(4)
    if not idx.empty:
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=365))
        idx1y = idx[idx["date"] >= cutoff]
        last = idx1y["value"].iloc[-1] if not idx1y.empty else None
        first = idx1y["value"].iloc[0] if not idx1y.empty else None
        delta = (last / first - 1) * 100 if last and first else None
        c1.metric("BVPSI index", fmt(last), f"{fmt(delta)}% 12m" if delta is not None else None)
    else:
        c1.metric("BVPSI index", "-")
    c2.metric("Listed issuers", len(uni))
    if not nonzero.empty:
        best = nonzero.loc[nonzero["ytd_pct"].idxmax()]
        worst = nonzero.loc[nonzero["ytd_pct"].idxmin()]
        c3.metric(f"Best YTD · {best['ticker']}", f"${fmt(best['price'])}", f"{best['ytd_pct']:+.1f}%")
        c4.metric(f"Worst YTD · {worst['ticker']}", f"${fmt(worst['price'])}", f"{worst['ytd_pct']:+.1f}%")

    if not idx.empty:
        idx1y = idx[idx["date"] >= pd.Timestamp(datetime.now() - timedelta(days=365))]
        fig = go.Figure(go.Scatter(x=idx1y["date"], y=idx1y["value"], mode="lines",
                                   line=dict(color=AZUL, width=2)))
        st.plotly_chart(style_fig(fig, 240, "BVPSI index (12 months)"), width="stretch")

    # Merge best bid/ask into the universe table.
    try:
        ob = order_book().set_index("ticker")
    except LatinexAPIError:
        ob = pd.DataFrame()

    left, right = st.columns([1.6, 1])
    with left:
        st.subheader(f"Universe ({len(uni)} stocks · snapshot {uni['as_of'].max()})")
        view = uni.copy()
        view["ytd_pct"] = view["ytd"] * 100
        view["range_pos"] = ((view["price"] - view["low_52w"])
                             / (view["high_52w"] - view["low_52w"]).replace(0, pd.NA) * 100)
        view["bid"] = view["ticker"].map(ob["bid"]) if "bid" in ob else pd.NA
        view["ask"] = view["ticker"].map(ob["ask"]) if "ask" in ob else pd.NA
        styled = view[["ticker", "issuer", "price", "ytd_pct", "range_pos",
                       "bid", "ask", "volume"]].rename(columns={
            "ticker": "Ticker", "issuer": "Issuer", "price": "Price", "ytd_pct": "YTD %",
            "range_pos": "52-wk range", "bid": "Bid", "ask": "Ask", "volume": "Volume"})
        st.dataframe(
            styled, width="stretch", height=520, hide_index=True,
            column_config={
                "Price": st.column_config.NumberColumn(format="dollar"),
                "YTD %": st.column_config.NumberColumn(format="%.1f%%"),
                "52-wk range": st.column_config.ProgressColumn(
                    format="%.0f%%", min_value=0, max_value=100,
                    help="Where the price sits within its 52-week range"),
                "Bid": st.column_config.NumberColumn(format="dollar"),
                "Ask": st.column_config.NumberColumn(format="dollar"),
                "Volume": st.column_config.NumberColumn(format="%.0f"),
            })

    with right:
        st.subheader("Order book")
        tickers = uni["ticker"].tolist()
        default = "BGFG" if "BGFG" in tickers else (tickers[0] if tickers else None)
        sel = st.selectbox("Instrument", tickers,
                           index=tickers.index(default) if default in tickers else 0)
        if sel:
            try:
                depth = order_book_depth(sel)
                last_px = float(uni.loc[uni["ticker"] == sel, "price"].iloc[0])
                issuer = uni.loc[uni["ticker"] == sel, "issuer"].iloc[0]
                html(order_book_html(depth, last_px, sel, issuer))
                imb = depth.get("imbalance_pct")
                if imb is not None:
                    side = "buyers are leaning in" if imb >= 0 else "sellers are pressing"
                    html(f"<div class='lx-callout' style='margin-top:14px'><div class='ic'>↕</div>"
                         f"<div>Across the visible levels, {'bid' if imb>=0 else 'offer'} depth "
                         f"outweighs the other side by <b>{abs(imb)}%</b> — {side}.</div></div>")
            except LatinexAPIError as e:
                st.warning(f"Order book unavailable: {e}")

    # ----- House view (cross-company ranking, precomputed offline) -----
    hv = SNAP.get("house_view") or {}
    if hv.get("ranking"):
        st.subheader("House view · covered companies ranked")
        if hv.get("overview"):
            html(f"<p style='font-size:13.5px;color:#475569;line-height:1.6;max-width:1000px'>"
                 f"{hv['overview']}</p>")
        stance_color = {"top pick": VERDE, "attractive": VERDE, "hold": AZUL,
                        "neutral": AZUL, "expensive": AMBER, "avoid": ROJO}
        cards = []
        for item in hv["ranking"]:
            col = stance_color.get(str(item.get("stance", "")).lower(), AZUL)
            cards.append(
                f"<div style='background:#fff;border:1px solid #E2E8F0;border-left:4px solid {col};"
                f"border-radius:12px;padding:12px 16px;margin-bottom:9px;"
                f"box-shadow:0 1px 3px rgba(11,61,102,.06)'>"
                f"<b style='color:{AZUL}'>#{item.get('rank')} {item.get('ticker')}</b> "
                f"<span style='font-size:11px;font-weight:700;color:{col};margin-left:6px'>"
                f"{str(item.get('stance','')).upper()}</span>"
                f"<div style='font-size:13px;color:#475569;margin-top:4px'>{item.get('one_liner','')}</div></div>")
        html("".join(cards))
        st.caption(f"Generated offline with {SNAP.get('model', 'Claude')} from verified filings; "
                   "not investment advice.")

    # ----- total-return league table (verified companies) -----
    league = []
    for tk in verified_companies():
        a = deep_analytics(tk)
        tr_, lq_, dp_ = a["tr"], a["lq"], a["dp"]
        vb_ = a["vb"]
        league.append({"Ticker": tk,
                       "Total return 1y %": tr_["tr_1y_pct"],
                       "Total return 5y %/yr": tr_["tr_5y_pct"],
                       "Yield TTM %": dp_["ttm_yield_pct"],
                       "P/E pctile vs self": (vb_["pe"] or {}).get("percentile"),
                       "Liquidity": lq_["grade"]})
    if league:
        st.subheader("Total return league table (verified coverage)")
        ldf = pd.DataFrame(league).sort_values("Total return 1y %", ascending=False)
        st.dataframe(ldf, hide_index=True, width="stretch",
                     column_config={
                         "Total return 1y %": st.column_config.NumberColumn(format="%.1f%%"),
                         "Total return 5y %/yr": st.column_config.NumberColumn(format="%.1f%%"),
                         "Yield TTM %": st.column_config.NumberColumn(format="%.2f%%"),
                         "P/E pctile vs self": st.column_config.ProgressColumn(
                             format="%.0f", min_value=0, max_value=100,
                             help="Where today's P/E sits in the stock's own 2023-todate range; low = cheap vs. itself"),
                     })
        st.caption("Total return = price + dividends reinvested. Source: Latinex prices & dividend history.")

    if not nonzero.empty:
        ranked = nonzero.sort_values("ytd_pct", ascending=False)
        fig = go.Figure(go.Bar(
            x=ranked["ytd_pct"], y=ranked["ticker"], orientation="h",
            marker_color=[VERDE if v >= 0 else ROJO for v in ranked["ytd_pct"]],
            text=[f"{v:+.1f}%" for v in ranked["ytd_pct"]], textposition="outside"))
        fig.update_layout(yaxis=dict(autorange="reversed"))
        st.plotly_chart(style_fig(fig, max(300, 26 * len(ranked)), "YTD performance (movers)"),
                        width="stretch")


# ---------------------------------------------------------------------------
# Page 2: Company Deep Dive
# ---------------------------------------------------------------------------

def page_deepdive():
    wl = load_watchlist()
    # Show only companies with fully verified (vision-read) financials, so every
    # name in the picker has correct, current data + a 3-year history.
    verified = set(verified_companies())
    if verified:
        options = [t for t in wl if t in verified] + sorted(t for t in verified if t not in wl)
    else:
        try:
            options = universe(False)["ticker"].tolist()
        except LatinexAPIError as e:
            st.error(f"Could not load tickers: {e}")
            return
    nemo = st.selectbox("Company", options, index=options.index("BGFG") if "BGFG" in options else 0)

    try:
        q = quote(nemo)
        s = summary(nemo)
    except LatinexAPIError as e:
        st.error(f"Could not load {nemo}: {e}")
        return

    kind = fin_mod.sector_kind(s["sector"], s["industry"])
    kind_label = {"banking": "Banking", "insurance": "Insurance"}.get(kind, "General")
    fin = financials(nemo)
    divs = dividends(nemo)
    y = api.get_dividend_yield(divs, q["price"])
    r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"]) if not fin["error"] else {}

    # ----- McKinsey deep dive (preloaded only -- NEVER calls the API at view
    # time; reports are precomputed offline by build_snapshot.py / fix_company.py) -----
    cache_key = fin.get("report_name") or "no-report"
    stored_dd = _tk(nemo).get("deep_dive")
    has_report = bool(stored_dd and not stored_dd.get("error"))
    dd = stored_dd if has_report else {"error": None, "data": None, "narrative": ""}
    if not has_report:
        st.info("The AI deep-dive report for this company hasn't been precomputed yet. "
                "Reports are generated offline (not on page load), so this page never "
                "spends API credits. The financials, ratios and ROE tree below are always shown.")
    # Optional manual refresh: local admin only (never auto-runs, never in the
    # deployed/public app); fires a single API call only when clicked.
    if has_report and is_admin() and not PUBLIC_MODE:
        _c1, _c2 = st.columns([5, 1])
        with _c2:
            if st.button("Regenerate", width="stretch",
                         help="Admin only: regenerate this report via the API (one call)"):
                _deep_dive_live.clear()
                dd = _deep_dive_live(nemo, cache_key)

    data = dd.get("data") or {}
    verdict = data.get("verdict", "Deep dive")
    vtone = {"good": "v-good", "bad": "v-bad"}.get(data.get("verdict_tone"), "v-neutral")

    # ----- header -----
    mcap_txt = fmt_money_compact(q["market_cap"])
    listed_txt = f" · listed {s['listing_date']}" if s.get("listing_date") else ""
    html(f"""<div class="lx-card dd-head">
      <div style="display:flex;gap:14px;align-items:center">
        <span class="tk">{nemo}</span>
        <div><div class="nm">{q['issuer_name'] or nemo}</div>
        <div class="cap">{s['sector']} · {kind_label} · Panama · {mcap_txt} market cap{listed_txt}</div></div>
      </div>
      <span class="verdict {vtone}">★ {verdict}</span>
    </div>""")

    # ----- KPI strip -----
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Price", f"${fmt(q['price'])}",
              f"{q['daily_change_pct']:+.2f}% today" if q["daily_change_pct"] is not None else None)
    k2.metric("YTD", f"{fmt(q['ytd_change_pct'], '{:+.2f}')}%" if q["ytd_change_pct"] is not None else "-")
    k3.metric("Market cap", fmt_money_compact(q["market_cap"]))
    k4.metric("Div. yield 12m", f"{fmt(y['total_yield_pct'])}%")
    k5.metric("P/E", fmt(r.get("pe")) if r else "-")
    an = deep_analytics(nemo)
    lq = an["lq"]
    k6.metric("Liquidity", lq["grade"] or "-", help=lq["grade_reason"] or None)

    if dd.get("error"):
        st.warning(dd["error"])

    # ----- Morningstar-style report (precomputed offline) -----
    rep = _tk(nemo).get("report") or {}
    if rep.get("fair_value"):
        html(report_header_html(rep))
        st.download_button(
            "⬇ Download company report (HTML → print to PDF)",
            data=build_tear_sheet(nemo, _tk(nemo), rep, an),
            file_name=f"{nemo}_report.html", mime="text/html")
        st.subheader("Business")
        html(f"<p style='font-size:14px;color:#475569;line-height:1.65;max-width:1000px'>"
             f"{rep.get('business_description', '')}</p>")
        segs = rep.get("segments") or []
        if segs:
            shares = [s.get("revenue_share_pct") for s in segs]
            sc1, sc2 = st.columns([1, 1.4]) if any(shares) else (None, st.container())
            if any(shares):
                with sc1:
                    known = [s for s in segs if s.get("revenue_share_pct")]
                    fig = go.Figure(go.Pie(
                        labels=[s["name"] for s in known],
                        values=[s["revenue_share_pct"] for s in known], hole=0.55,
                        marker=dict(colors=[AZUL, "#2563EB", VERDE, AMBER, GRIS, ROJO])))
                    st.plotly_chart(style_fig(fig, 280, "Revenue by segment"),
                                    width="stretch", key=f"seg_{nemo}")
            with sc2:
                html("".join(
                    f"<div style='background:#fff;border:1px solid #E2E8F0;border-radius:10px;"
                    f"padding:9px 13px;margin-bottom:7px;font-size:13px'>"
                    f"<b style='color:{AZUL}'>{s.get('name')}</b>"
                    + (f" <span style='color:{VERDE};font-weight:700'>· {s['revenue_share_pct']}%</span>"
                       if s.get("revenue_share_pct") else "")
                    + f"<div style='color:#475569;margin-top:2px'>{s.get('detail', '')}</div></div>"
                    for s in segs))
        comps = rep.get("competitors") or []
        if comps:
            with st.expander("Competition (analyst market knowledge, not from filings)"):
                for c in comps:
                    st.markdown(f"- **{c.get('area')}**: {', '.join(c.get('names', []))} — "
                                f"{c.get('positioning', '')}")
        if rep.get("bulls_say") and rep.get("bears_say"):
            html(bulls_bears_html(rep["bulls_say"], rep["bears_say"]))
        if rep.get("thesis"):
            html(f"<div class='lx-callout' style='margin-top:14px'><div class='ic'>✎</div>"
                 f"<div><b>Analyst thesis</b><br>{rep['thesis']}</div></div>")
        st.caption(f"Fair value methodology: {(rep.get('fair_value') or {}).get('methods', '')} "
                   f"Ratings and fair value are model-generated estimates from verified filing "
                   f"data — not investment advice.")
        if data.get("scorecard"):
            html(scorecard_html(data["scorecard"]))
    else:
        # fall back to the original deep-dive summary blocks
        if data.get("executive_summary"):
            html(f"<p style='font-size:14px;color:#475569;line-height:1.6;margin:16px 0 4px;"
                 f"max-width:1000px'>{data['executive_summary']}</p>")
        if data.get("scorecard"):
            html(scorecard_html(data["scorecard"]))
        if data.get("strengths") and data.get("weaknesses"):
            st.write("")
            html(sw_html(data["strengths"], data["weaknesses"]))

    # ----- What changed this quarter (precomputed at build time) -----
    wc = _tk(nemo).get("whats_changed") or {}
    if wc.get("text"):
        html(f"<div class='lx-callout' style='margin-top:14px'><div class='ic'>Δ</div>"
             f"<div><b>What changed this quarter</b><br>{wc['text']}</div></div>")

    # ----- ROE / DuPont tree (always, from parsed financials) -----
    st.subheader("ROE value-driver tree (DuPont)")
    if fin["error"]:
        msg = ("reports are published as scanned images" if "no text layer" in fin["error"]
               else fin["error"])
        st.info(f"ROE tree unavailable — {msg}. "
                + (f"[Open source PDF]({fin['pdf_url']})" if fin.get("pdf_url") else ""))
    else:
        d = dupont(nemo, cache_key, kind)
        try:
            peer_roe_med = peer_metrics(kind)["roe_pct"].median()
        except Exception:
            peer_roe_med = None
        html(roe_tree_svg(d, peer_roe_med))
        st.caption("Annualized, from the latest parsed filing. Colors: green = ahead of the peer "
                   "ROE median / favorable cost ratio, red = behind, grey = neutral."
                   + (f" {d['note']}." if d.get("note") else ""))

    # ----- price & volume (preserved) -----
    st.subheader("Price & volume")
    rango = st.radio("Range", ["1M", "3M", "6M", "1Y", "5Y", "ALL"], index=3, horizontal=True)
    try:
        hist = history(nemo, rango)
    except LatinexAPIError:
        hist = pd.DataFrame()
    if not hist.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["close"], mode="lines",
                                 name="Price", line=dict(color=AZUL, width=2)))
        fig.add_trace(go.Bar(x=hist["date"], y=hist["volume"], name="Volume",
                             yaxis="y2", marker_color=GRIS, opacity=0.35))
        fig.update_layout(yaxis=dict(title="USD"),
                          yaxis2=dict(overlaying="y", side="right", showgrid=False))
        st.plotly_chart(style_fig(fig, 380), width="stretch")
    else:
        st.info("No trades in the selected range.")

    # ----- total return (dividends reinvested) -----
    tr = an["tr"]
    if tr["series"] is not None:
        st.subheader("Total return (dividends reinvested)")
        c1, c2 = st.columns([1.7, 1])
        with c1:
            srs = tr["series"]
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=srs["date"], y=srs["tr_idx"], mode="lines",
                                     name="Total return", line=dict(color=AZUL, width=2.4)))
            fig.add_trace(go.Scatter(x=srs["date"], y=srs["price_idx"], mode="lines",
                                     name="Price only", line=dict(color=GRIS, width=1.6, dash="dot")))
            fig.update_layout(yaxis_title="Indexed to 100")
            st.plotly_chart(style_fig(fig, 330), width="stretch", key=f"tr_{nemo}")
        with c2:
            m1, m2 = st.columns(2)
            m1.metric("Total return 1y", f"{fmt(tr['tr_1y_pct'], '{:+.1f}')}%" if tr["tr_1y_pct"] is not None else "-")
            m2.metric("Price only 1y", f"{fmt(tr['pr_1y_pct'], '{:+.1f}')}%" if tr["pr_1y_pct"] is not None else "-")
            m3, m4 = st.columns(2)
            m3.metric("Total return 5y (ann.)", f"{fmt(tr['tr_5y_pct'], '{:+.1f}')}%" if tr["tr_5y_pct"] is not None else "-")
            m4.metric("Price only 5y (ann.)", f"{fmt(tr['pr_5y_pct'], '{:+.1f}')}%" if tr["pr_5y_pct"] is not None else "-")
            if tr["value_10k"]:
                start_yr = tr["start"].year if tr["start"] is not None else ""
                html(f"<div class='lx-callout'><div class='ic'>$</div><div>"
                     f"$10,000 invested in {start_yr} with dividends reinvested is worth "
                     f"<b>${tr['value_10k']:,}</b> today"
                     + (f" — plus <b>${tr['div_cash_10k']:,}</b> of dividend cash if taken as income."
                        if tr["div_cash_10k"] else ".") + "</div></div>")

    # ----- valuation vs. its own history -----
    vb = an["vb"]
    if vb["pe"] or vb["pb"]:
        st.subheader("Valuation vs. its own history")
        vc1, vc2 = st.columns([1.7, 1])
        band = vb["pe"] or vb["pb"]
        band_name = "P/E" if vb["pe"] else "P/B"
        with vc1:
            s = band["series"]
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=s["date"], y=s["mult"], mode="lines",
                                     name=band_name, line=dict(color=AZUL, width=2)))
            fig.add_hline(y=band["median"], line=dict(color=GRIS, dash="dash"),
                          annotation_text=f"median {band['median']}x")
            fig.update_layout(yaxis_title=f"{band_name} (x)")
            st.plotly_chart(style_fig(fig, 330), width="stretch", key=f"band_{nemo}")
        with vc2:
            for key, label in [("pe", "P/E"), ("pb", "P/B")]:
                b = vb.get(key)
                if not b:
                    continue
                tone = ("expensive" if b["percentile"] >= 80 else
                        "cheap" if b["percentile"] <= 20 else "mid-range")
                st.metric(f"{label} today", f"{b['current']}x",
                          f"{b['percentile']}th pct of own history",
                          delta_color="inverse" if b["percentile"] >= 80 else
                          ("normal" if b["percentile"] <= 20 else "off"))
                st.caption(f"{label} range since 2023: {b['min']}x – {b['max']}x "
                           f"(median {b['median']}x) → **{tone}** vs. itself")

    # ----- valuation & ratios (preserved) -----
    if not fin["error"]:
        st.subheader("Valuation & ratios")
        ocr_tag = " · parsed via OCR" if fin.get("ocr_used") else ""
        st.caption(f"Report **{fin['report_name']}** ({fin['report_date']}) · scale: "
                   f"**{fin['scale_label']}** · {'quarterly' if fin['is_quarterly'] else 'annual'} figures"
                   f"{ocr_tag} · [source PDF]({fin['pdf_url']})")
        k = st.columns(6)
        k[0].metric("EPS (annualized)", f"${fmt(r['eps'])}" if r.get("eps") is not None else "-")
        k[1].metric("P/E", fmt(r.get("pe")))
        k[2].metric("BVPS", f"${fmt(r['bvps'])}" if r.get("bvps") is not None else "-")
        k[3].metric("P/B", fmt(r.get("pb")))
        k[4].metric("ROE", f"{fmt(r.get('roe_pct'))}%")
        k[5].metric("ROA", f"{fmt(r.get('roa_pct'))}%")

        sector_rows = fin_mod.compute_sector_ratios(fin, kind)
        if sector_rows:
            st.markdown(f"**{kind_label} ratios**")
            cols = st.columns(len(sector_rows))
            for col, (label, val, help_text) in zip(cols, sector_rows):
                col.metric(label, val, help=help_text)

        # ----- 3-year history (preserved) -----
        st.subheader("Annual history (USD)")
        with st.spinner("Loading FY2023-FY2025 history..."):
            h = historical(nemo)
        if not h["table"].empty:
            money_cols = [c for c in h["table"].columns if c != "Metric"]
            st.dataframe(money_style(h["table"], money_cols), hide_index=True, width="stretch")
            srcs = " · ".join(f"[{col}]({url})" for col, _n, url in h["sources"])
            st.caption(f"Sources: {srcs}")
            ni_row = h["table"][h["table"]["Metric"] == "Net income"]
            if not ni_row.empty:
                fy_cols = [c for c in money_cols if c.startswith("FY")]
                vals = [ni_row.iloc[0][c] for c in fy_cols]
                if any(v is not None and not pd.isna(v) for v in vals):
                    fig = go.Figure(go.Bar(
                        x=fy_cols, y=vals, marker_color=AZUL,
                        text=[f"${v/1e6:,.0f}M" if v else "-" for v in vals], textposition="outside"))
                    st.plotly_chart(style_fig(fig, 300, "Annual net income (controlling)"),
                                    width="stretch", key=f"ni_{nemo}")
        else:
            st.info("History not available.")
        if h["errors"]:
            st.caption("Notes: " + "; ".join(h["errors"]))
    else:
        if "no text layer" in fin["error"]:
            st.warning(f"{nemo} publishes its reports as scanned PDFs. With the OCR engine "
                       "installed (Tesseract) these are parsed automatically; otherwise the "
                       "statements can't be extracted.")
        else:
            st.warning(f"Financial statements not parsed: {fin['error']}")
        if fin.get("pdf_url"):
            st.markdown(f"[Open source PDF: {fin['report_name']}]({fin['pdf_url']})")

    # ----- dividend record & sustainability -----
    dp = an["dp"]
    if dp["per_year"]:
        st.subheader("Dividend record & sustainability")
        dcols = st.columns(5)
        dcols[0].metric("Dividends / share (TTM)", f"${fmt(dp['ttm_dps'])}" if dp["ttm_dps"] is not None else "-")
        dcols[1].metric("Yield (TTM)", f"{fmt(dp['ttm_yield_pct'])}%" if dp["ttm_yield_pct"] is not None else "-")
        dcols[2].metric("Payout vs FY2025 EPS", f"{fmt(dp['payout_pct'], '{:,.0f}')}%" if dp["payout_pct"] is not None else "-",
                        help="Trailing dividends per share / FY2025 earnings per share")
        dcols[3].metric("Growth streak", f"{dp['growth_streak_years']} yrs" if dp["growth_streak_years"] is not None else "-",
                        help="Consecutive complete years of rising dividends per share")
        dcols[4].metric("DPS CAGR 3y", f"{fmt(dp['dps_cagr_3y_pct'], '{:+.1f}')}%" if dp["dps_cagr_3y_pct"] is not None else "-")
        years = [y for y in dp["per_year"] if y >= datetime.now().year - 6]
        if years:
            fig = go.Figure(go.Bar(x=[str(y) for y in years],
                                   y=[dp["per_year"][y] for y in years],
                                   marker_color=AZUL,
                                   text=[f"${dp['per_year'][y]:,.2f}" for y in years],
                                   textposition="outside"))
            st.plotly_chart(style_fig(fig, 260, "Dividends per share by year"),
                            width="stretch", key=f"dps_{nemo}")

    # ----- earnings quality (cash-flow statement, when extracted) -----
    eq = an["eq"]
    if eq["cfo"] is not None:
        st.subheader("Earnings quality (cash flow)")
        qc = st.columns(5)
        qc[0].metric("Operating cash flow", fmt_money_compact(eq["cfo"]))
        qc[1].metric("Cash conversion", f"{fmt(eq['cash_conversion_pct'], '{:,.0f}')}%" if eq["cash_conversion_pct"] is not None else "-",
                     help="Operating cash flow / net income — near or above 100% means earnings are backed by cash")
        qc[2].metric("Free cash flow", fmt_money_compact(eq["fcf"]) if eq["fcf"] is not None else "-",
                     help="Operating cash flow − capital expenditures")
        qc[3].metric("Dividends paid", fmt_money_compact(abs(eq["dividends_paid"])) if eq["dividends_paid"] else "-")
        qc[4].metric("Dividend coverage", f"{fmt(eq['div_coverage_x'])}x" if eq["div_coverage_x"] is not None else "-",
                     help="Operating cash flow / dividends paid — above 1x means dividends are cash-funded")
        cc = eq["cash_conversion_pct"]
        if cc is not None:
            tone = ("Earnings are fully cash-backed." if cc >= 90 else
                    "Earnings are partially cash-backed — watch accruals." if cc >= 50 else
                    "Weak cash conversion — reported earnings are well ahead of cash generation.")
            st.caption(f"Cash conversion {cc:,.0f}%: {tone} (Period figures from the latest filing.)")

    # ----- long-form narrative (preserved) -----
    if dd.get("narrative"):
        st.subheader("Business analysis")
        html(f"<div class='analysis-box'>\n\n{dd['narrative']}\n\n</div>")
        st.caption(f"Generated with {dd['model']} using only real figures from Latinex and "
                   "Yahoo Finance. Verify against the source PDFs before deciding.")
        st.session_state.setdefault("analyses", {})[nemo] = dd["narrative"]

    # ----- peers (preserved) -----
    st.subheader("International peers")
    try:
        local = local_metrics_row(nemo)
        comp = pd.concat([pd.DataFrame([{**local, "country": "Panama", "market": "Latinex"}]),
                          peer_metrics(kind)], ignore_index=True)
        st.dataframe(peers_display(comp, {nemo}), hide_index=True, width="stretch")
        st.caption("Peer data via Yahoo Finance (may differ in methodology). The local company "
                   "uses figures parsed from Latinex filings.")
    except Exception as e:
        st.warning(f"Peers unavailable: {e}")

    # ----- dividends, filings, statements (preserved) -----
    with st.expander("Dividends (history)"):
        if not divs.empty:
            show = divs.head(16).copy()
            show["payment_date"] = show["payment_date"].dt.strftime("%Y-%m-%d")
            show["record_date"] = show["record_date"].dt.strftime("%Y-%m-%d")
            st.dataframe(show.rename(columns={"record_date": "Record", "payment_date": "Payment",
                                              "amount": "Amount", "type": "Type"}),
                         hide_index=True, width="stretch")
        else:
            st.info("No dividend history.")

    with st.expander("Filings & material disclosures"):
        try:
            docs = documents_for(nemo, q["issuer_code"])
        except LatinexAPIError:
            docs = pd.DataFrame()
        if docs is not None and not docs.empty:
            for _, row in docs.head(10).iterrows():
                st.markdown(f"- {row['date']} · *{row['type']}* · "
                            f"[{row['name'][:70]}]({row['pdf_url']})")
        issuer_key = (q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo)
        try:
            nots = notices_for(nemo, issuer_key)
        except LatinexAPIError:
            nots = pd.DataFrame()
        if not nots.empty:
            st.markdown("**Material disclosures (hechos relevantes)**")
            for _, row in nots.head(8).iterrows():
                st.markdown(f"- {row['date']} · [{row['title'][:80]}]({row['pdf_url']})")

    if not fin["error"]:
        with st.expander(f"Financial statements as reported ({fin['scale_label']})"):
            ci, cb = st.columns(2)
            with ci:
                st.markdown("**Income statement**")
                if not fin["income"].empty:
                    st.dataframe(money_style(fin["income"]), hide_index=True,
                                 width="stretch", height=360)
            with cb:
                st.markdown("**Balance sheet**")
                if not fin["balance"].empty:
                    st.dataframe(money_style(fin["balance"]), hide_index=True,
                                 width="stretch", height=360)


# ---------------------------------------------------------------------------
# Page 3: Comparables (3 views)
# ---------------------------------------------------------------------------

def _comp_frame(nemos, kind):
    rows = []
    for nemo in nemos:
        try:
            local = local_metrics_row(nemo)
            rows.append({**local, "country": "Panama", "market": "Latinex"})
        except Exception:
            continue
    if not rows:
        return None
    return pd.concat([pd.DataFrame(rows), peer_metrics(kind)], ignore_index=True)


def _view_refined(comp, local_set):
    peers_only = comp[~comp["ticker"].isin(local_set)]
    local_row = comp[comp["ticker"].isin(local_set)].iloc[0] if not comp[comp["ticker"].isin(local_set)].empty else None

    def med(col):
        return peers_only[col].median()

    cols = st.columns(5)
    specs = [("P/E · median", med("pe"), local_row["pe"] if local_row is not None else None, "x", False),
             ("P/B · median", med("pb"), local_row["pb"] if local_row is not None else None, "x", False),
             ("ROE · median", med("roe_pct"), local_row["roe_pct"] if local_row is not None else None, "%", True),
             ("Div yield · median", med("div_yield_pct"), local_row["div_yield_pct"] if local_row is not None else None, "%", True),
             ("Market cap · median", med("market_cap"), None, "$", False)]
    for col, (label, mval, lval, unit, higher_better) in zip(cols, specs):
        if unit == "$":
            col.metric(label, fmt_money_compact(mval))
        else:
            delta = None
            if lval is not None and mval:
                diff = lval - mval
                delta = f"{diff:+.1f}{'pp' if unit=='%' else unit} vs local"
            col.metric(label, f"{fmt(mval)}{unit}", delta,
                       delta_color="normal" if higher_better else "inverse")
    st.dataframe(peers_display(comp, local_set), hide_index=True, width="stretch")


def _view_map(comp, local_set, prefix=""):
    d = comp.dropna(subset=["roe_pct", "pe"]).copy()
    if d.empty:
        st.info("Not enough data to plot the valuation map.")
        return
    sizes = d["market_cap"].fillna(d["market_cap"].median())
    smax = sizes.max() or 1
    fig = go.Figure()
    is_local = d["ticker"].isin(local_set)
    fig.add_trace(go.Scatter(
        x=d["roe_pct"], y=d["pe"], mode="markers+text",
        text=d["ticker"], textposition="top center",
        marker=dict(size=12 + 38 * (sizes / smax),
                    color=[AZUL if l else "#CBD5E1" for l in is_local],
                    line=dict(width=2, color="#fff")),
        hovertext=d["name"], hoverinfo="text"))
    fig.add_vline(x=d["roe_pct"].median(), line=dict(color="#CBD5E1", dash="dash"))
    fig.add_hline(y=d["pe"].median(), line=dict(color="#CBD5E1", dash="dash"))
    fig.update_layout(xaxis_title="ROE % (return on equity) →",
                      yaxis_title="↑ P/E (more expensive)")
    st.plotly_chart(style_fig(fig, 460, "Valuation map"), width="stretch",
                    key=f"map_{prefix}")
    st.caption("Bubble size ∝ market cap. Dashed lines = group medians. "
               "Top-left = cheap & profitable; top-right = expensive & profitable.")


def _view_relative(comp, local_set, prefix=""):
    metrics = [("market_cap", "Market cap", None), ("pe", "P/E", "{:.1f}x"),
               ("pb", "P/B", "{:.2f}x"), ("roe_pct", "ROE %", "{:.1f}%"),
               ("div_yield_pct", "Div yield %", "{:.1f}%")]
    grid = st.columns(2)
    for i, (col, label, pat) in enumerate(metrics):
        d = comp.dropna(subset=[col]).sort_values(col)
        if d.empty:
            continue
        colors = [AZUL if t in local_set else "#CBD5E1" for t in d["ticker"]]
        labels = [fmt_money_compact(v) if pat is None else pat.format(v) for v in d[col]]
        fig = go.Figure(go.Bar(x=d[col], y=d["ticker"], orientation="h", marker_color=colors,
                               text=labels, textposition="outside"))
        fig.add_vline(x=d[col].median(), line=dict(color=ROJO, dash="dash", width=1))
        with grid[i % 2]:
            st.plotly_chart(style_fig(fig, 300, label), width="stretch",
                            key=f"rel_{prefix}_{col}")


def page_comparables():
    st.title("Comparables by sector")
    st.caption("Watchlist names (figures from Latinex filings) against a group of "
               "international peers (Yahoo Finance).")

    wl = load_watchlist()
    by_kind = {}
    for nemo in wl:
        try:
            s = summary(nemo)
            by_kind.setdefault(fin_mod.sector_kind(s["sector"], s["industry"]), []).append(nemo)
        except LatinexAPIError:
            continue
    if not by_kind:
        st.info("No watchlist companies could be classified.")
        return

    labels = {"banking": "Banking", "insurance": "Insurance", "generic": "Other sectors"}
    order = [k for k in ("banking", "insurance", "generic") if k in by_kind]
    sector_tabs = st.tabs([labels.get(k, k) for k in order])
    for tab, kind in zip(sector_tabs, order):
        with tab:
            comp = _comp_frame(by_kind[kind], kind)
            if comp is None or comp.empty:
                st.info("No comparable data for this sector.")
                continue
            local_set = set(by_kind[kind])
            va, vb, vc = st.tabs(["A · Refined sheet", "B · Valuation map", "C · Relative position"])
            with va:
                _view_refined(comp, local_set)
            with vb:
                _view_map(comp, local_set, prefix=kind)
            with vc:
                _view_relative(comp, local_set, prefix=kind)


# ---------------------------------------------------------------------------
# Page 4: Export
# ---------------------------------------------------------------------------

def page_export():
    st.title("Export to Excel")
    st.caption("Build a workbook with the market snapshot, watchlist financials, dividends, "
               "peers and the analyses generated this session.")

    if st.button("Generate Excel file"):
        with st.spinner("Building workbook..."):
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                try:
                    universe(False).to_excel(writer, sheet_name="Market", index=False)
                except LatinexAPIError:
                    pass
                try:
                    order_book().to_excel(writer, sheet_name="Order book", index=False)
                except LatinexAPIError:
                    pass

                ratio_rows, analysis_rows, kinds_used = [], [], set()
                for nemo in load_watchlist():
                    try:
                        q = quote(nemo)
                        s = summary(nemo)
                        fin = financials(nemo)
                        divs = dividends(nemo)
                    except LatinexAPIError:
                        continue
                    kinds_used.add(fin_mod.sector_kind(s["sector"], s["industry"]))
                    if not fin["error"]:
                        if not fin["income"].empty:
                            fin["income"].to_excel(writer, sheet_name=f"{nemo} Income"[:31], index=False)
                        if not fin["balance"].empty:
                            fin["balance"].to_excel(writer, sheet_name=f"{nemo} Balance"[:31], index=False)
                        h = historical(nemo)
                        if not h["table"].empty:
                            h["table"].to_excel(writer, sheet_name=f"{nemo} Hist USD"[:31], index=False)
                        r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
                        yld = api.get_dividend_yield(divs, q["price"])
                        ratio_rows.append({
                            "Ticker": nemo, "Price": q["price"], "Market cap": q["market_cap"],
                            "EPS": r["eps"], "P/E": r["pe"], "BVPS": r["bvps"], "P/B": r["pb"],
                            "ROE %": r["roe_pct"], "ROA %": r["roa_pct"],
                            "Div yield 12m %": yld["total_yield_pct"],
                            "Scale": fin["scale_label"], "Report": fin["report_name"]})
                    if not divs.empty:
                        d = divs.copy()
                        d["payment_date"] = d["payment_date"].dt.strftime("%Y-%m-%d")
                        d["record_date"] = d["record_date"].dt.strftime("%Y-%m-%d")
                        d.insert(0, "ticker", nemo)
                        sheet, startrow = "Dividends", 0
                        if sheet in writer.sheets:
                            startrow = writer.sheets[sheet].max_row + 1
                        d.to_excel(writer, sheet_name=sheet, index=False,
                                   header=startrow == 0, startrow=startrow)
                    text = st.session_state.get("analyses", {}).get(nemo)
                    if text:
                        analysis_rows.append({"Ticker": nemo, "Analysis": text})

                if ratio_rows:
                    pd.DataFrame(ratio_rows).to_excel(writer, sheet_name="Ratios", index=False)
                for kind in kinds_used:
                    p = peer_metrics(kind)
                    if not p.empty:
                        p.to_excel(writer, sheet_name=f"Peers {kind}"[:31], index=False)
                if analysis_rows:
                    pd.DataFrame(analysis_rows).to_excel(writer, sheet_name="Analyses", index=False)

            st.download_button(
                "Download latinex_snapshot.xlsx", data=buf.getvalue(),
                file_name=f"latinex_snapshot_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if not st.session_state.get("analyses"):
            st.caption("Tip: open the Company Deep Dive page to generate analyses before "
                       "exporting; they'll be included in the 'Analyses' sheet.")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

render_sidebar()
nav = st.navigation([
    st.Page(page_market, title="Market", icon=":material/monitoring:", default=True),
    st.Page(page_deepdive, title="Company Deep Dive", icon=":material/insights:"),
    st.Page(page_comparables, title="Comparables", icon=":material/balance:"),
    st.Page(page_export, title="Export", icon=":material/download:"),
])
nav.run()
