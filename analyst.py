# -*- coding: utf-8 -*-
"""
analyst.py -- Analisis narrativo del negocio generado con Claude.

Arma un brief con TODOS los numeros reales ya parseados (mercado, historico
anual, ratios, dividendos, hechos relevantes, peers) y le pide a Claude un
analisis de equity en espanol que cuente la historia del negocio citando
solo cifras del brief.
"""

import logging
import os

import anthropic
import pandas as pd
from dotenv import load_dotenv

import latinex_api as api
import financials as fin_mod
import peers as peers_mod

# .env local primero; fallback al .env del taleb-dashboard (misma maquina)
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
load_dotenv("C:/Users/NicolasArditoBarlett/taleb-dashboard/.env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("LATINEX_MODEL", "claude-opus-4-6")

log = logging.getLogger("analyst")


def _fmt_money(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/d"
    return f"${v:,.0f}"


def _fmt(v, suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/d"
    return f"{v}{suffix}"


def build_data_brief(nemo, fin_override=None):
    """Brief de texto con todos los datos reales disponibles para un ticker.

    fin_override: a pre-parsed financials dict (e.g. vision-extracted) to use
    instead of re-parsing the PDF live. Returns (brief, context).
    """
    q = api.get_quote(nemo)
    s = api.get_summary(nemo)
    kind = fin_mod.sector_kind(s["sector"], s["industry"])

    lines = []
    lines.append(f"COMPANY: {q['issuer_name']} (ticker {nemo}, Latinex - Panama Stock Exchange)")
    lines.append(f"Sector: {s['sector']} / {s['industry']} | ISIN: {s['isin']} | "
                 f"Shares outstanding: {_fmt(s['shares_outstanding'])}")
    lines.append(f"Listed since: {s['listing_date']}")

    lines.append("\n--- MARKET (current) ---")
    lines.append(f"Price: ${q['price']} | Daily change: {_fmt(q['daily_change_pct'], '%')} | "
                 f"YTD: {_fmt(q['ytd_change_pct'], '%')}")
    lines.append(f"Market cap: {_fmt_money(q['market_cap'])} | "
                 f"Average volume: {_fmt(q['avg_volume'])} shares")

    # 52-week range from the universe
    try:
        uni = api.get_equity_universe(include_preferred=True)
        row = uni[uni["ticker"] == nemo]
        if not row.empty:
            lines.append(f"52-week range: ${row.iloc[0]['low_52w']} - ${row.iloc[0]['high_52w']}")
    except api.LatinexAPIError:
        pass

    # Dividends
    try:
        divs = api.get_dividends(nemo)
        y = api.get_dividend_yield(divs, q["price"])
        lines.append("\n--- DIVIDENDS ---")
        lines.append(f"Trailing 12 months: ordinary ${_fmt(y['ordinary_12m'])}/share "
                     f"(yield {_fmt(y['ordinary_yield_pct'], '%')}), "
                     f"total incl. special ${_fmt(y['total_12m'])}/share "
                     f"(yield {_fmt(y['total_yield_pct'], '%')})")
        if not divs.empty:
            recent = divs.head(8)
            for _, d in recent.iterrows():
                pdate = d["payment_date"].strftime("%Y-%m-%d") if pd.notna(d["payment_date"]) else "?"
                lines.append(f"  {pdate}: ${d['amount']} ({d['type']})")
    except api.LatinexAPIError:
        pass

    # Parsed financials (may fail for scanned PDFs)
    fin = fin_override if fin_override is not None else \
        fin_mod.get_financials(nemo, issuer_code=q["issuer_code"])
    if fin["error"]:
        lines.append(f"\n--- FINANCIAL STATEMENTS: NOT AVAILABLE ({fin['error']}) ---")
        lines.append("NOTE: analyze ONLY with market and dividend data; "
                     "be explicit about this limitation.")
    else:
        r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
        lines.append(f"\n--- VALUATION (from report {fin['report_name']}) ---")
        lines.append(f"Annualized EPS: ${_fmt(r['eps'])} | P/E: {_fmt(r['pe'])} | "
                     f"BVPS: ${_fmt(r['bvps'])} | P/B: {_fmt(r['pb'])}")
        lines.append(f"ROE: {_fmt(r['roe_pct'], '%')} | ROA: {_fmt(r['roa_pct'], '%')} | "
                     f"Equity/Assets: {_fmt(r['equity_to_assets_pct'], '%')}")
        if r["note"]:
            lines.append(f"Note: {r['note']}")

        sector_rows = fin_mod.compute_sector_ratios(fin, kind)
        if sector_rows:
            lines.append(f"\n--- SECTOR RATIOS ({kind}) ---")
            for label, val, help_text in sector_rows:
                lines.append(f"{label}: {val}  [{help_text}]")

        hist = fin_mod.get_historical(nemo, issuer_code=q["issuer_code"])
        if not hist["table"].empty:
            lines.append("\n--- ANNUAL HISTORY (full USD, audited) ---")
            t = hist["table"].copy()
            cols = [c for c in t.columns if c != "Metric"]
            for _, row in t.iterrows():
                vals = " | ".join(f"{c}: {_fmt_money(row[c])}" for c in cols)
                lines.append(f"{row['Metric']}: {vals}")
            if hist["errors"]:
                lines.append(f"Notes: {'; '.join(hist['errors'])}")

    # Material disclosures (hechos relevantes)
    try:
        issuer_key = (q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo)
        nots = api.get_notices(issuer_filter=issuer_key)
        if not nots.empty:
            lines.append("\n--- RECENT MATERIAL DISCLOSURES ---")
            for _, n in nots.head(8).iterrows():
                lines.append(f"{n['date']}: {n['title']}")
    except api.LatinexAPIError:
        pass

    # International peers
    try:
        pdf_ = peers_mod.get_peer_metrics(kind)
        if not pdf_.empty:
            lines.append(f"\n--- INTERNATIONAL PEERS ({kind}, via Yahoo Finance) ---")
            for _, p in pdf_.iterrows():
                lines.append(
                    f"{p['ticker']} ({p['name']}, {p['country']}): "
                    f"P/E {_fmt(p['pe'])} | P/B {_fmt(p['pb'])} | ROE {_fmt(p['roe_pct'], '%')} | "
                    f"Div yield {_fmt(p['div_yield_pct'], '%')} | MCap {_fmt_money(p['market_cap'])}")
    except Exception as e:
        log.warning(f"peers for brief failed: {e}")

    context = {"quote": q, "summary": s, "kind": kind}
    return "\n".join(lines), context


PROMPT_TEMPLATE = """You are a senior equity analyst covering the Panamanian stock market (Latinex). \
A sophisticated local investor asks you for an analysis of {name} ({nemo}).

REAL DATA (the only allowed source -- do NOT invent or estimate figures that are not here):

{brief}

Write the analysis in English, Markdown format, ~500-700 words, telling the STORY of the \
business with the numbers. Structure:

## Trajectory
How the business evolved 2023->today (growth in earnings, assets, revenue -- cite figures and compute % changes).

## Drivers and profitability
What moves results; sector margins/ratios and what they say about the business.

## Balance-sheet strength
Capitalization, leverage, balance-sheet quality.

## Dividends and shareholder return
Observed dividend policy, yield, sustainability (payout vs earnings).

## Relative valuation
P/E, P/B vs the international peers in the brief: is it cheap or expensive, and why might the discount/premium be justified (local market liquidity, size, country risk).

## Risks and what to watch
3-4 concrete risks and signals to monitor (use the material disclosures if relevant).

Rules: EXACT figures from the brief (you may round to millions with one decimal); if a datum is missing, say so; \
be direct and opinionated, not promotional; compute the % changes yourself from the brief figures."""


def generate_analysis(nemo, brief=None, context=None):
    """Genera el analisis narrativo. Returns dict {text, model, error}."""
    out = {"text": "", "model": MODEL, "error": None}

    if not ANTHROPIC_API_KEY:
        out["error"] = ("ANTHROPIC_API_KEY no configurada. Copia el archivo .env del "
                        "taleb-dashboard a la carpeta latinex-tracker.")
        return out

    try:
        if brief is None:
            brief, context = build_data_brief(nemo)
        name = (context or {}).get("quote", {}).get("issuer_name", nemo)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        # max_tokens cubre pensamiento adaptativo + ~700 palabras de analisis;
        # 3000 cortaba la conclusion.
        msg = client.messages.create(
            model=MODEL,
            max_tokens=6000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user",
                       "content": PROMPT_TEMPLATE.format(name=name, nemo=nemo, brief=brief)}],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        out["text"] = text.strip()
        if not out["text"]:
            out["error"] = "Claude no devolvio texto."
    except anthropic.AuthenticationError:
        out["error"] = "API key invalida (AuthenticationError)."
    except anthropic.RateLimitError:
        out["error"] = "Limite de tasa alcanzado -- intenta de nuevo en unos minutos."
    except anthropic.APIStatusError as e:
        out["error"] = f"Error del API de Anthropic ({e.status_code}): {e.message}"
        log.error(f"[Claude] {nemo}: {e}")
    except anthropic.APIConnectionError:
        out["error"] = "Sin conexion con el API de Anthropic."
    except Exception as e:
        out["error"] = f"Error generando analisis: {e}"
        log.error(f"[Claude] {nemo}: {e}")
    return out


# ---------------------------------------------------------------------------
# Deep dive (structured, McKinsey-style) -- feeds the Company Deep Dive page
# ---------------------------------------------------------------------------

def _fmt_pct(v):
    return "n/d" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v}%"


def build_dupont_brief(nemo, context=None, fin_override=None):
    """Text block describing the ROE value-driver tree for the deep-dive prompt.

    Returns (text, dupont_dict). dupont_dict is the raw decomposition so the
    UI can draw the tree from numbers, not from the model's prose.
    """
    if context is None:
        q = api.get_quote(nemo)
        s = api.get_summary(nemo)
        kind = fin_mod.sector_kind(s["sector"], s["industry"])
    else:
        q, s, kind = context["quote"], context["summary"], context["kind"]

    fin = fin_override if fin_override is not None else \
        fin_mod.get_financials(nemo, issuer_code=q["issuer_code"])
    if fin["error"]:
        return f"\n--- ROE TREE: no disponible ({fin['error']}) ---", {}
    d = fin_mod.dupont_decomposition(fin, kind)

    lines = ["\n--- ROE DECOMPOSITION (DuPont, annualized) ---",
             "ROE = ROA x Leverage ; ROA = Net margin x Asset yield",
             f"ROE: {_fmt_pct(d['roe_pct'])} | ROA: {_fmt_pct(d['roa_pct'])} | "
             f"Leverage (assets/equity): {_fmt(d['leverage_x'], 'x')}",
             f"Net margin: {_fmt_pct(d['net_margin_pct'])} | "
             f"Asset yield (revenue/assets): {_fmt_pct(d['asset_yield_pct'])}",
             f"Bank levers -> NIM: {_fmt_pct(d['nim_pct'])} | "
             f"Fees/assets: {_fmt_pct(d['fee_to_assets_pct'])} | "
             f"Efficiency (cost/income): {_fmt_pct(d['cost_income_pct'])} | "
             f"Cost of risk: {_fmt_pct(d['cost_of_risk_pct'])} | "
             f"Effective tax: {_fmt_pct(d['effective_tax_pct'])}"]
    if d.get("note"):
        lines.append(f"Note: {d['note']}")
    return "\n".join(lines), d


DEEP_DIVE_PROMPT = """You are a strategy consultant (McKinsey style) and equity analyst \
covering Latinex (Panama Stock Exchange). Prepare a "deep dive" on {name} ({nemo}).

REAL DATA (the only allowed source -- do NOT invent figures):

{brief}

Return ONLY a valid JSON object (no text before or after, no ``` ) with this EXACT shape:
{{
  "verdict": "short 2-4 word label (e.g. 'Quality compounder', 'Cyclical value', 'Value trap')",
  "verdict_tone": "good | neutral | bad",
  "executive_summary": "2-3 sentences synthesizing the thesis using real figures",
  "scorecard": [
    {{"dimension": "Profitability", "grade": "A-", "score": 82, "rationale": "one sentence"}},
    {{"dimension": "Growth", "grade": "B", "score": 68, "rationale": "one sentence"}},
    {{"dimension": "Balance sheet", "grade": "A", "score": 88, "rationale": "one sentence"}},
    {{"dimension": "Capital return", "grade": "B", "score": 71, "rationale": "one sentence"}},
    {{"dimension": "Valuation", "grade": "C+", "score": 55, "rationale": "one sentence"}}
  ],
  "strengths": [{{"title": "short title", "detail": "one sentence with a figure"}}],
  "weaknesses": [{{"title": "short title", "detail": "one sentence with a figure"}}]
}}

Rules: write everything in English; 4-6 strengths and 4-6 weaknesses; score 0-100 consistent with the grade; \
EXACT figures from the brief; if financial statements are unavailable, base the scorecard on market/dividend \
data and say so in the rationales."""


def generate_deep_dive(nemo, fin_override=None):
    """Structured McKinsey-style deep dive. Returns dict:
    {error, model, data, dupont, narrative}. `data` is the parsed JSON above;
    `dupont` is the raw ROE-tree decomposition; `narrative` is the long-form
    markdown analysis (reuses generate_analysis). fin_override lets callers pass
    pre-parsed (e.g. vision-extracted) financials instead of re-parsing live."""
    import json as _json

    out = {"error": None, "model": MODEL, "data": None, "dupont": {}, "narrative": ""}
    if not ANTHROPIC_API_KEY:
        out["error"] = ("ANTHROPIC_API_KEY no configurada. Copia el .env del "
                        "taleb-dashboard a la carpeta latinex-tracker.")
        return out

    try:
        brief, context = build_data_brief(nemo, fin_override=fin_override)
        dupont_text, dupont = build_dupont_brief(nemo, context=context, fin_override=fin_override)
        out["dupont"] = dupont
        full_brief = brief + "\n" + dupont_text
        name = context.get("quote", {}).get("issuer_name", nemo)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user",
                       "content": DEEP_DIVE_PROMPT.format(name=name, nemo=nemo, brief=full_brief)}],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        # Be tolerant of stray code fences / prose around the JSON.
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            out["data"] = _json.loads(text[start:end + 1])
        else:
            out["error"] = "El modelo no devolvio JSON valido para el deep dive."

        # Long-form narrative reuses the existing analysis (same real brief).
        narr = generate_analysis(nemo, brief=brief, context=context)
        out["narrative"] = narr.get("text", "")
    except _json.JSONDecodeError as e:
        out["error"] = f"No se pudo parsear el JSON del deep dive: {e}"
    except anthropic.AuthenticationError:
        out["error"] = "API key invalida (AuthenticationError)."
    except anthropic.RateLimitError:
        out["error"] = "Limite de tasa alcanzado -- intenta de nuevo en unos minutos."
    except anthropic.APIStatusError as e:
        out["error"] = f"Error del API de Anthropic ({e.status_code}): {e.message}"
    except Exception as e:
        out["error"] = f"Error generando deep dive: {e}"
        log.error(f"[deep-dive] {nemo}: {e}")
    return out


# ---------------------------------------------------------------------------
# "What changed this quarter" -- short delta narrative from computed numbers
# ---------------------------------------------------------------------------

WHATS_CHANGED_PROMPT = """You are an equity analyst covering Latinex (Panama). Below are the \
COMPUTED changes for {name} ({nemo}): the latest quarter's run-rate (annualized flows) and \
balance-sheet levels versus fiscal year 2025, plus cash-flow quality metrics when available.

{table}

Write 2-4 sentences in English, plain prose (no headers/bullets), telling an investor what \
actually changed and whether it is good or bad. Use ONLY these numbers; quote the most \
important 2-3 figures (percent changes). Be direct -- 'run-rate earnings are tracking X% \
above/below last year' style. Do not invent causes you cannot see in the numbers."""


def generate_whats_changed(nemo, name, deltas, eq=None):
    """One short Claude call narrating the computed quarter deltas.
    Returns {text, error}."""
    out = {"text": "", "error": None}
    if not ANTHROPIC_API_KEY:
        out["error"] = "ANTHROPIC_API_KEY not set"
        return out
    if not deltas:
        out["error"] = "no computed deltas"
        return out
    lines = [f"{m}: latest {v['latest']:,.0f} vs FY2025 {v['fy2025']:,.0f} "
             f"({v['delta_pct']:+.1f}%)" for m, v in deltas.items()]
    if eq and eq.get("cash_conversion_pct") is not None:
        lines.append(f"Cash conversion (CFO/net income): {eq['cash_conversion_pct']}%")
    if eq and eq.get("div_coverage_x") is not None:
        lines.append(f"Dividend coverage (CFO/dividends): {eq['div_coverage_x']}x")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=MODEL, max_tokens=600,
            messages=[{"role": "user", "content": WHATS_CHANGED_PROMPT.format(
                name=name, nemo=nemo, table="\n".join(lines))}])
        out["text"] = next((b.text for b in msg.content if b.type == "text"), "").strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"what-changed generation failed: {e}"
    return out


# ---------------------------------------------------------------------------
# House view -- rank all covered companies against each other
# ---------------------------------------------------------------------------

HOUSE_VIEW_PROMPT = """You are the head of research for a Panama-focused equity shop. Below \
are the verified, filing-derived metrics for every company you cover on Latinex.

{table}

Rank ALL of them from most to least attractive on a risk-adjusted basis, weighing valuation \
(absolute and vs. the stock's own history), profitability and its trend, dividend yield and \
sustainability, balance-sheet strength, and liquidity (illiquid names deserve a discount).

Return ONLY a JSON object (no prose, no code fences):
{{
  "overview": "3-4 sentences: the current state of the Latinex market and where the value is",
  "ranking": [
    {{"rank": 1, "ticker": "XXX", "stance": "Top pick | Attractive | Hold | Expensive | Avoid",
      "one_liner": "one crisp sentence with the 1-2 numbers that decide it"}}
  ]
}}

Rules: every company exactly once; stances must be internally consistent with the ranking; \
use ONLY figures from the table; write in English."""


def generate_house_view(companies_table):
    """One Claude call ranking the covered companies. companies_table is a
    preformatted text block of per-company metrics. Returns {overview, ranking,
    error}."""
    import json as _json
    out = {"overview": "", "ranking": [], "error": None}
    if not ANTHROPIC_API_KEY:
        out["error"] = "ANTHROPIC_API_KEY not set"
        return out
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=MODEL, max_tokens=2500,
            messages=[{"role": "user",
                       "content": HOUSE_VIEW_PROMPT.format(table=companies_table)}])
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        start, end = text.find("{"), text.rfind("}")
        data = _json.loads(text[start:end + 1])
        out["overview"] = data.get("overview", "")
        out["ranking"] = data.get("ranking", [])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"house view generation failed: {e}"
    return out


if __name__ == "__main__":
    import sys
    nemo = sys.argv[1] if len(sys.argv) > 1 else "BGFG"
    print(f"=== Brief {nemo} ===")
    brief, ctx = build_data_brief(nemo)
    print(brief)
    print(f"\n=== Analisis ({MODEL}) ===")
    result = generate_analysis(nemo, brief=brief, context=ctx)
    if result["error"]:
        print("ERROR:", result["error"])
    else:
        print(result["text"])
