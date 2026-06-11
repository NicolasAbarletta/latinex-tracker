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


def build_data_brief(nemo):
    """Brief de texto con todos los datos reales disponibles para un ticker.

    Returns (brief: str, context: dict) -- context trae piezas reutilizables
    (quote, summary, kind) para no re-fetchear en el dashboard.
    """
    q = api.get_quote(nemo)
    s = api.get_summary(nemo)
    kind = fin_mod.sector_kind(s["sector"], s["industry"])

    lines = []
    lines.append(f"EMPRESA: {q['issuer_name']} (ticker {nemo}, Latinex - Bolsa de Valores de Panama)")
    lines.append(f"Sector: {s['sector']} / {s['industry']} | ISIN: {s['isin']} | "
                 f"Acciones en circulacion: {_fmt(s['shares_outstanding'])}")
    lines.append(f"Listada desde: {s['listing_date']}")

    lines.append("\n--- MERCADO (actual) ---")
    lines.append(f"Precio: ${q['price']} | Variacion dia: {_fmt(q['daily_change_pct'], '%')} | "
                 f"YTD: {_fmt(q['ytd_change_pct'], '%')}")
    lines.append(f"Capitalizacion de mercado: {_fmt_money(q['market_cap'])} | "
                 f"Volumen promedio: {_fmt(q['avg_volume'])} acciones")

    # Rango 52 semanas desde el universo
    try:
        uni = api.get_equity_universe(include_preferred=True)
        row = uni[uni["ticker"] == nemo]
        if not row.empty:
            lines.append(f"Rango 52 semanas: ${row.iloc[0]['low_52w']} - ${row.iloc[0]['high_52w']}")
    except api.LatinexAPIError:
        pass

    # Dividendos
    try:
        divs = api.get_dividends(nemo)
        y = api.get_dividend_yield(divs, q["price"])
        lines.append("\n--- DIVIDENDOS ---")
        lines.append(f"Ultimos 12 meses: ordinarios ${_fmt(y['ordinary_12m'])}/accion "
                     f"(yield {_fmt(y['ordinary_yield_pct'], '%')}), "
                     f"total con extraordinarios ${_fmt(y['total_12m'])}/accion "
                     f"(yield {_fmt(y['total_yield_pct'], '%')})")
        if not divs.empty:
            recent = divs.head(8)
            for _, d in recent.iterrows():
                pdate = d["payment_date"].strftime("%Y-%m-%d") if pd.notna(d["payment_date"]) else "?"
                lines.append(f"  {pdate}: ${d['amount']} ({d['type']})")
    except api.LatinexAPIError:
        pass

    # Financials parseados (puede fallar para PDFs escaneados)
    fin = fin_mod.get_financials(nemo, issuer_code=q["issuer_code"])
    if fin["error"]:
        lines.append(f"\n--- ESTADOS FINANCIEROS: NO DISPONIBLES ({fin['error']}) ---")
        lines.append("NOTA: analiza SOLO con datos de mercado y dividendos; "
                     "se explicito sobre esta limitacion.")
    else:
        r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
        lines.append(f"\n--- VALORACION (del reporte {fin['report_name']}) ---")
        lines.append(f"EPS anualizado: ${_fmt(r['eps'])} | P/E: {_fmt(r['pe'])} | "
                     f"BVPS: ${_fmt(r['bvps'])} | P/B: {_fmt(r['pb'])}")
        lines.append(f"ROE: {_fmt(r['roe_pct'], '%')} | ROA: {_fmt(r['roa_pct'], '%')} | "
                     f"Patrimonio/Activos: {_fmt(r['equity_to_assets_pct'], '%')}")
        if r["note"]:
            lines.append(f"Nota: {r['note']}")

        sector_rows = fin_mod.compute_sector_ratios(fin, kind)
        if sector_rows:
            lines.append(f"\n--- RATIOS SECTORIALES ({kind}) ---")
            for label, val, help_text in sector_rows:
                lines.append(f"{label}: {val}  [{help_text}]")

        hist = fin_mod.get_historical(nemo, issuer_code=q["issuer_code"])
        if not hist["table"].empty:
            lines.append("\n--- HISTORICO ANUAL (USD completos, auditado) ---")
            t = hist["table"].copy()
            cols = [c for c in t.columns if c != "Metrica"]
            for _, row in t.iterrows():
                vals = " | ".join(f"{c}: {_fmt_money(row[c])}" for c in cols)
                lines.append(f"{row['Metrica']}: {vals}")
            if hist["errors"]:
                lines.append(f"Avisos: {'; '.join(hist['errors'])}")

    # Hechos relevantes
    try:
        issuer_key = (q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo)
        nots = api.get_notices(issuer_filter=issuer_key)
        if not nots.empty:
            lines.append("\n--- HECHOS RELEVANTES RECIENTES ---")
            for _, n in nots.head(8).iterrows():
                lines.append(f"{n['date']}: {n['title']}")
    except api.LatinexAPIError:
        pass

    # Peers internacionales
    try:
        pdf_ = peers_mod.get_peer_metrics(kind)
        if not pdf_.empty:
            lines.append(f"\n--- PEERS INTERNACIONALES ({kind}, via Yahoo Finance) ---")
            for _, p in pdf_.iterrows():
                lines.append(
                    f"{p['ticker']} ({p['name']}, {p['country']}): "
                    f"P/E {_fmt(p['pe'])} | P/B {_fmt(p['pb'])} | ROE {_fmt(p['roe_pct'], '%')} | "
                    f"Div yield {_fmt(p['div_yield_pct'], '%')} | MCap {_fmt_money(p['market_cap'])}")
    except Exception as e:
        log.warning(f"peers for brief failed: {e}")

    context = {"quote": q, "summary": s, "kind": kind}
    return "\n".join(lines), context


PROMPT_TEMPLATE = """Eres un analista de equity senior que cubre el mercado de valores panameno (Latinex). \
Un inversionista local sofisticado te pide un analisis de {name} ({nemo}).

DATOS REALES (unica fuente permitida -- NO inventes ni estimes cifras que no esten aqui):

{brief}

Escribe un analisis en espanol, formato Markdown, ~500-700 palabras, que CUENTE LA HISTORIA \
del negocio con los numeros. Estructura:

## Trayectoria
Como evoluciono el negocio 2023->hoy (crecimiento de utilidades, activos, ingresos -- cita cifras y calcula variaciones %).

## Drivers y rentabilidad
Que mueve los resultados; margenes/ratios sectoriales y que dicen del negocio.

## Solidez de balance
Capitalizacion, apalancamiento, calidad del balance.

## Dividendos y retorno al accionista
Politica de dividendos observada, yield, sostenibilidad (payout vs utilidades).

## Valoracion relativa
P/E, P/B vs los peers internacionales del brief: esta cara o barata y por que podria justificarse el descuento/premio (liquidez del mercado local, tamano, riesgo pais).

## Riesgos y que vigilar
3-4 riesgos concretos y senales a monitorear (usa los hechos relevantes si aportan).

Reglas: cifras EXACTAS del brief (puedes redondear a millones con un decimal); si un dato no esta, dilo; \
se directo y con opinion fundamentada, no promocional; los porcentajes de variacion calculalos tu a partir de las cifras del brief."""


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
