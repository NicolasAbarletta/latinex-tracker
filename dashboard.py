# -*- coding: utf-8 -*-
"""
dashboard.py -- Latinex Equity Tracker v3 (Streamlit).

Paginas: Mercado | Empresa | Comparables | Exportar
Estilo claro profesional; analisis narrativo con Claude; peers internacionales.
"""

import io
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import latinex_api as api
import financials as fin_mod
import peers as peers_mod
import analyst
from latinex_api import LatinexAPIError

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

# Modo publico (sitio web): visitantes no pueden gastar credito del API ni
# editar el watchlist. En Streamlit Cloud se activa con el secret PUBLIC_MODE.
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")


def is_admin():
    if not PUBLIC_MODE:
        return True
    return bool(ADMIN_KEY) and st.session_state.get("admin_key_input", "") == ADMIN_KEY


AZUL = "#0B3D66"
VERDE = "#2E7D32"
ROJO = "#C62828"
GRIS = "#90A4AE"

st.set_page_config(page_title="Latinex Equity Tracker", page_icon=":bank:",
                   layout="wide", initial_sidebar_state="expanded")

CSS = """
<style>
/* numeros tabulares en metricas y tablas */
[data-testid="stMetricValue"], [data-testid="stDataFrame"] {
    font-variant-numeric: tabular-nums;
}
/* tarjetas de metricas */
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 3px rgba(11, 61, 102, 0.06);
}
[data-testid="stMetricLabel"] { color: #64748B; }
/* encabezados de seccion con linea azul */
h2, h3 { color: #0B3D66; }
h3 {
    border-bottom: 2px solid #DBEAFE;
    padding-bottom: 6px;
}
/* bloque de analisis */
.analysis-box {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-left: 4px solid #0B3D66;
    border-radius: 8px;
    padding: 22px 26px;
    box-shadow: 0 1px 3px rgba(11, 61, 102, 0.06);
}
.analysis-box h2 { font-size: 1.1rem; border: none; margin-top: 0.8em; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def style_fig(fig, height=380, title=None):
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=10, r=10, t=44 if title else 16, b=10),
        title=title,
        font=dict(family="Segoe UI, sans-serif", color="#1A202C"),
        title_font=dict(color=AZUL, size=15),
        plot_bgcolor="#FFFFFF",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.08),
    )
    return fig


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def universe(include_preferred):
    return api.get_equity_universe(include_preferred=include_preferred)


@st.cache_data(ttl=300, show_spinner=False)
def quote(nemo):
    return api.get_quote(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def summary(nemo):
    return api.get_summary(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def history(nemo, rango):
    return api.get_history(nemo, rango)


@st.cache_data(ttl=300, show_spinner=False)
def dividends(nemo):
    return api.get_dividends(nemo)


@st.cache_data(ttl=300, show_spinner=False)
def documents(issuer_code):
    return api.get_documents(issuer_code)


@st.cache_data(ttl=300, show_spinner=False)
def notices(issuer_name):
    return api.get_notices(issuer_filter=issuer_name)


@st.cache_data(ttl=1800, show_spinner=False)
def financials(nemo):
    return fin_mod.get_financials(nemo)


@st.cache_data(ttl=86400, show_spinner=False)
def historical(nemo):
    return fin_mod.get_historical(nemo)


@st.cache_data(ttl=3600, show_spinner=False)
def peer_metrics(kind):
    return peers_mod.get_peer_metrics(kind)


@st.cache_data(ttl=86400, show_spinner=False)
def index_history():
    return api.get_index_history("1Y")


@st.cache_data(ttl=86400, show_spinner=False)
def claude_analysis(nemo, cache_key):
    """cache_key = report_name del ultimo filing -> invalida al publicar uno nuevo."""
    return analyst.generate_analysis(nemo)


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


def money_style(df, money_cols=None, decimals=0):
    cols = money_cols or [c for c in df.columns if df[c].dtype.kind in "fi"]
    return df.style.format({c: ("${:,.%df}" % decimals).format for c in cols},
                           na_rep="-")


def local_metrics_row(nemo):
    """Metricas de la empresa local (datos Latinex parseados) para comparar."""
    q = quote(nemo)
    s = summary(nemo)
    fin = financials(nemo)
    r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
    divs = dividends(nemo)
    y = api.get_dividend_yield(divs, q["price"])
    return {
        "ticker": nemo,
        "name": q["issuer_name"],
        "market_cap": q["market_cap"],
        "pe": r["pe"],
        "pb": r["pb"],
        "roe_pct": r["roe_pct"],
        "div_yield_pct": y["total_yield_pct"],
        "profit_margin_pct": None,
    }


def peers_display(df, local_tickers):
    """Tabla de peers con $ y la(s) fila(s) locales resaltadas."""
    show = df.rename(columns={
        "ticker": "Ticker", "name": "Nombre", "country": "Pais", "market": "Mercado",
        "market_cap": "Market Cap", "pe": "P/E", "pb": "P/B", "roe_pct": "ROE %",
        "div_yield_pct": "Div Yield %", "profit_margin_pct": "Margen %"})

    def highlight(row):
        if row["Ticker"] in local_tickers:
            return ["background-color: #DBEAFE; font-weight: 600;"] * len(row)
        return [""] * len(row)

    return (show.style
            .apply(highlight, axis=1)
            .format({"Market Cap": lambda v: fmt(v, "${:,.0f}"),
                     "P/E": lambda v: fmt(v), "P/B": lambda v: fmt(v),
                     "ROE %": lambda v: fmt(v), "Div Yield %": lambda v: fmt(v),
                     "Margen %": lambda v: fmt(v)}, na_rep="-"))


def peers_charts(df, local_tickers, key_prefix):
    c1, c2 = st.columns(2)
    colors = [AZUL if t in local_tickers else GRIS for t in df["ticker"]]
    with c1:
        fig = go.Figure(go.Bar(x=df["ticker"], y=df["pe"], marker_color=colors,
                               text=[fmt(v) for v in df["pe"]], textposition="outside"))
        st.plotly_chart(style_fig(fig, 300, "P/E"), use_container_width=True,
                        key=f"{key_prefix}_pe")
    with c2:
        fig = go.Figure(go.Bar(x=df["ticker"], y=df["roe_pct"], marker_color=colors,
                               text=[fmt(v) for v in df["roe_pct"]], textposition="outside"))
        st.plotly_chart(style_fig(fig, 300, "ROE %"), use_container_width=True,
                        key=f"{key_prefix}_roe")


# ---------------------------------------------------------------------------
# Sidebar (comun a todas las paginas)
# ---------------------------------------------------------------------------

def render_sidebar():
    st.sidebar.markdown(f"<div style='font-size:1.25rem;font-weight:700;color:{AZUL};'>"
                        "Latinex Equity Tracker</div>", unsafe_allow_html=True)
    st.sidebar.caption("Bolsa Latinoamericana de Valores - Panama")

    if is_admin():
        if st.sidebar.button("Actualizar datos", use_container_width=True):
            st.cache_data.clear()
            st.session_state["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.rerun()
    st.sidebar.caption(f"Ultima actualizacion: {st.session_state.get('last_refresh', 'esta sesion')} "
                       "- precios con retraso de hasta 5 min")

    st.sidebar.divider()
    st.sidebar.subheader("Watchlist")
    wl_current = load_watchlist()
    if is_admin():
        try:
            all_tickers = universe(True)["ticker"].tolist()
        except LatinexAPIError:
            all_tickers = wl_current
        wl_selected = st.sidebar.multiselect(
            "Empresas con analisis financiero",
            options=sorted(set(all_tickers) | set(wl_current)),
            default=wl_current)
        if sorted(wl_selected) != sorted(wl_current) and wl_selected:
            save_watchlist(wl_selected)
            st.sidebar.success("Watchlist guardado")
    else:
        st.sidebar.caption("Empresas con analisis financiero: " + ", ".join(wl_current))

    st.sidebar.divider()
    st.sidebar.caption(f"Analisis generado con {analyst.MODEL}")
    if PUBLIC_MODE:
        with st.sidebar.expander("Administrador"):
            st.text_input("Clave", type="password", key="admin_key_input")


# ---------------------------------------------------------------------------
# Pagina 1: Mercado
# ---------------------------------------------------------------------------

def page_mercado():
    st.title("Mercado de acciones de Panama")

    try:
        idx = index_history()
    except LatinexAPIError:
        idx = pd.DataFrame()
    include_pref = st.toggle("Incluir acciones preferidas", value=False)
    try:
        uni = universe(include_pref)
    except LatinexAPIError as e:
        st.error(f"No se pudo cargar el universo de acciones: {e}")
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
        c1.metric("Indice BVPSI", fmt(last), f"{fmt(delta)}% 12m" if delta is not None else None)
    else:
        c1.metric("Indice BVPSI", "-")
    c2.metric("Emisores listados", len(uni))
    if not nonzero.empty:
        best = nonzero.loc[nonzero["ytd_pct"].idxmax()]
        worst = nonzero.loc[nonzero["ytd_pct"].idxmin()]
        c3.metric(f"Mejor YTD: {best['ticker']}", f"${fmt(best['price'])}", f"{best['ytd_pct']:+.1f}%")
        c4.metric(f"Peor YTD: {worst['ticker']}", f"${fmt(worst['price'])}", f"{worst['ytd_pct']:+.1f}%")

    if not idx.empty:
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=365))
        idx1y = idx[idx["date"] >= cutoff]
        fig = go.Figure(go.Scatter(x=idx1y["date"], y=idx1y["value"], mode="lines",
                                   line=dict(color=AZUL, width=2)))
        st.plotly_chart(style_fig(fig, 260, "Indice BVPSI (12 meses)"), use_container_width=True)

    st.subheader(f"Universo ({len(uni)} acciones - snapshot {uni['as_of'].max()})")
    view = uni.copy()
    view["ytd_pct"] = view["ytd"] * 100
    view["range_pos"] = ((view["price"] - view["low_52w"])
                         / (view["high_52w"] - view["low_52w"]).replace(0, pd.NA) * 100)
    styled = view[["ticker", "issuer", "price", "ytd_pct", "low_52w", "high_52w",
                   "range_pos", "volume", "sector"]].rename(columns={
        "ticker": "Ticker", "issuer": "Emisor", "price": "Precio",
        "ytd_pct": "YTD %", "low_52w": "Min 52s", "high_52w": "Max 52s",
        "range_pos": "Posicion rango", "volume": "Volumen", "sector": "Sector"})
    st.dataframe(
        styled, use_container_width=True, height=560, hide_index=True,
        column_config={
            "Precio": st.column_config.NumberColumn(format="dollar"),
            "YTD %": st.column_config.NumberColumn(format="%.1f%%"),
            "Min 52s": st.column_config.NumberColumn(format="dollar"),
            "Max 52s": st.column_config.NumberColumn(format="dollar"),
            "Posicion rango": st.column_config.ProgressColumn(
                format="%.0f%%", min_value=0, max_value=100,
                help="Donde esta el precio dentro de su rango de 52 semanas"),
            "Volumen": st.column_config.NumberColumn(format="%.0f"),
        })

    if not nonzero.empty:
        ranked = nonzero.sort_values("ytd_pct", ascending=False)
        fig = go.Figure(go.Bar(
            x=ranked["ytd_pct"], y=ranked["ticker"], orientation="h",
            marker_color=[VERDE if v >= 0 else ROJO for v in ranked["ytd_pct"]],
            text=[f"{v:+.1f}%" for v in ranked["ytd_pct"]], textposition="outside"))
        fig.update_layout(yaxis=dict(autorange="reversed"))
        st.plotly_chart(style_fig(fig, max(300, 26 * len(ranked)),
                                  "Rendimiento YTD (movers)"), use_container_width=True)


# ---------------------------------------------------------------------------
# Pagina 2: Empresa
# ---------------------------------------------------------------------------

def page_empresa():
    wl = load_watchlist()
    try:
        all_tickers = universe(True)["ticker"].tolist()
    except LatinexAPIError as e:
        st.error(f"No se pudieron cargar los tickers: {e}")
        return
    options = wl + [t for t in sorted(all_tickers) if t not in wl]
    nemo = st.selectbox("Empresa", options, index=options.index("BGFG") if "BGFG" in options else 0)

    try:
        q = quote(nemo)
        s = summary(nemo)
    except LatinexAPIError as e:
        st.error(f"No se pudo cargar {nemo}: {e}")
        return

    kind = fin_mod.sector_kind(s["sector"], s["industry"])
    kind_label = {"banking": "Banca", "insurance": "Seguros"}.get(kind, "General")

    st.title(q["issuer_name"] or nemo)
    st.caption(f"{s['sector']} / {s['industry']} - ISIN {s['isin']} - listada desde {s['listing_date']}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Precio", f"${fmt(q['price'])}",
              f"{q['daily_change_pct']:+.2f}% hoy" if q["daily_change_pct"] is not None else None)
    c2.metric("YTD", f"{fmt(q['ytd_change_pct'], '{:+.2f}')}%" if q["ytd_change_pct"] is not None else "-")
    c3.metric("Market cap", fmt(q["market_cap"], "${:,.0f}"))
    c4.metric("Acciones en circ.", fmt(s["shares_outstanding"], "{:,.0f}"))
    divs = dividends(nemo)
    y = api.get_dividend_yield(divs, q["price"])
    c5.metric("Div. yield 12m", f"{fmt(y['total_yield_pct'])}%",
              f"${fmt(y['total_12m'], '{:,.2f}')}/accion" if y["total_12m"] else None)

    # --- precio/volumen ---
    rango = st.radio("Rango", ["1M", "3M", "6M", "1Y", "5Y", "ALL"], index=3, horizontal=True)
    try:
        hist = history(nemo, rango)
    except LatinexAPIError:
        hist = pd.DataFrame()
    if not hist.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["close"], mode="lines",
                                 name="Precio", line=dict(color=AZUL, width=2)))
        fig.add_trace(go.Bar(x=hist["date"], y=hist["volume"], name="Volumen",
                             yaxis="y2", marker_color=GRIS, opacity=0.35))
        fig.update_layout(yaxis=dict(title="USD"),
                          yaxis2=dict(overlaying="y", side="right", showgrid=False))
        st.plotly_chart(style_fig(fig, 400), use_container_width=True)
    else:
        st.info("Sin operaciones en el rango seleccionado.")

    fin = financials(nemo)

    # --- analisis Claude ---
    st.subheader("Analisis del negocio")
    cache_key = fin.get("report_name") or "sin-reporte"
    if is_admin():
        col_a, col_b = st.columns([5, 1])
        with col_b:
            if st.button("Regenerar", use_container_width=True):
                claude_analysis.clear()
    if not is_admin() and nemo not in load_watchlist():
        st.info("El analisis con Claude esta disponible solo para las empresas del watchlist "
                f"({', '.join(load_watchlist())}).")
        result = {"error": "skip", "text": "", "model": analyst.MODEL}
    else:
        with st.spinner(f"Generando analisis con {analyst.MODEL}..."):
            result = claude_analysis(nemo, cache_key)
    if result["error"]:
        if result["error"] != "skip":
            st.warning(result["error"])
    else:
        # Escapar $ para que Streamlit no interprete "$x ... $y" como LaTeX
        safe_text = result["text"].replace("$", "\\$")
        st.markdown(f"<div class='analysis-box'>\n\n{safe_text}\n\n</div>",
                    unsafe_allow_html=True)
        st.caption(f"Generado con {result['model']} usando solo cifras reales de Latinex "
                   "y Yahoo Finance. Verifica contra los PDFs fuente antes de decidir.")
        st.session_state.setdefault("analyses", {})[nemo] = result["text"]

    # --- valoracion ---
    if fin["error"]:
        if "no text layer" in fin["error"]:
            st.warning(f"{nemo} presenta sus informes como PDF escaneado (imagen) - "
                       "los estados financieros no se pueden extraer automaticamente.")
        else:
            st.warning(f"Estados financieros no parseados: {fin['error']}")
        if fin["pdf_url"]:
            st.markdown(f"[Abrir PDF fuente: {fin['report_name']}]({fin['pdf_url']})")
    else:
        st.subheader("Valoracion y ratios")
        st.caption(f"Reporte **{fin['report_name']}** ({fin['report_date']}) - "
                   f"escala: **{fin['scale_label']}** - "
                   f"cifras {'trimestrales' if fin['is_quarterly'] else 'anuales'} - "
                   f"[PDF fuente]({fin['pdf_url']})")
        r = fin_mod.compute_ratios(fin, q["price"], s["shares_outstanding"])
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("EPS (anualizado)", f"${fmt(r['eps'])}" if r["eps"] is not None else "-")
        k2.metric("P/E", fmt(r["pe"]))
        k3.metric("BVPS", f"${fmt(r['bvps'])}" if r["bvps"] is not None else "-")
        k4.metric("P/B", fmt(r["pb"]))
        k5.metric("ROE", f"{fmt(r['roe_pct'])}%")
        k6.metric("ROA", f"{fmt(r['roa_pct'])}%")
        if r["note"]:
            st.caption(f"Nota: {r['note']}")

        sector_rows = fin_mod.compute_sector_ratios(fin, kind)
        if sector_rows:
            st.markdown(f"**Ratios de {kind_label.lower()}**")
            cols = st.columns(len(sector_rows))
            for col, (label, val, help_text) in zip(cols, sector_rows):
                col.metric(label, val, help=help_text)

        # --- historico ---
        st.subheader("Historico anual (USD)")
        with st.spinner("Cargando historico FY2023-FY2025..."):
            h = historical(nemo)
        if not h["table"].empty:
            money_cols = [c for c in h["table"].columns if c != "Metrica"]
            st.dataframe(money_style(h["table"], money_cols),
                         hide_index=True, use_container_width=True)
            srcs = " - ".join(f"[{col}]({url})" for col, _n, url in h["sources"])
            st.caption(f"Fuentes: {srcs}")
            ni_row = h["table"][h["table"]["Metrica"] == "Utilidad neta"]
            if not ni_row.empty:
                fy_cols = [c for c in money_cols if c.startswith("FY")]
                vals = [ni_row.iloc[0][c] for c in fy_cols]
                if any(v is not None and not pd.isna(v) for v in vals):
                    fig = go.Figure(go.Bar(
                        x=fy_cols, y=vals, marker_color=AZUL,
                        text=[f"${v/1e6:,.0f}M" if v else "-" for v in vals],
                        textposition="outside"))
                    st.plotly_chart(style_fig(fig, 300, "Utilidad neta anual (controladora)"),
                                    use_container_width=True, key=f"ni_{nemo}")
        else:
            st.info("Historico no disponible.")
        if h["errors"]:
            st.caption("Avisos: " + "; ".join(h["errors"]))

    # --- peers ---
    st.subheader("Comparables internacionales")
    try:
        local = local_metrics_row(nemo)
        comp = pd.concat([pd.DataFrame([{**local, "country": "Panama", "market": "Latinex"}]),
                          peer_metrics(kind)], ignore_index=True)
        st.dataframe(peers_display(comp, {nemo}), hide_index=True, use_container_width=True)
        st.caption("Datos de peers via Yahoo Finance (pueden diferir en metodologia). "
                   "La empresa local usa cifras parseadas de los filings de Latinex.")
        peers_charts(comp, {nemo}, f"emp_{nemo}")
    except Exception as e:
        st.warning(f"Comparables no disponibles: {e}")

    # --- dividendos y filings ---
    with st.expander("Dividendos (historial)"):
        if not divs.empty:
            show = divs.head(16).copy()
            show["payment_date"] = show["payment_date"].dt.strftime("%Y-%m-%d")
            show["record_date"] = show["record_date"].dt.strftime("%Y-%m-%d")
            st.dataframe(show.rename(columns={
                "record_date": "Registro", "payment_date": "Pago",
                "amount": "Monto", "type": "Tipo"}),
                hide_index=True, use_container_width=True)
        else:
            st.info("Sin historial de dividendos.")

    with st.expander("Filings y hechos relevantes"):
        if q["issuer_code"]:
            try:
                docs = documents(q["issuer_code"])
            except LatinexAPIError:
                docs = pd.DataFrame()
            if not docs.empty:
                for _, row in docs.head(10).iterrows():
                    st.markdown(f"- {row['date']} - *{row['type']}* - "
                                f"[{row['name'][:70]}]({row['pdf_url']})")
        issuer_key = (q["issuer_name"].split(",")[0] if q["issuer_name"] else nemo)
        try:
            nots = notices(issuer_key)
        except LatinexAPIError:
            nots = pd.DataFrame()
        if not nots.empty:
            st.markdown("**Hechos relevantes**")
            for _, row in nots.head(8).iterrows():
                st.markdown(f"- {row['date']} - [{row['title'][:80]}]({row['pdf_url']})")

    if not fin["error"]:
        with st.expander(f"Estados financieros como se presentaron ({fin['scale_label']})"):
            ci, cb = st.columns(2)
            with ci:
                st.markdown("**Estado de resultados**")
                if not fin["income"].empty:
                    st.dataframe(money_style(fin["income"]), hide_index=True,
                                 use_container_width=True, height=360)
            with cb:
                st.markdown("**Balance**")
                if not fin["balance"].empty:
                    st.dataframe(money_style(fin["balance"]), hide_index=True,
                                 use_container_width=True, height=360)


# ---------------------------------------------------------------------------
# Pagina 3: Comparables
# ---------------------------------------------------------------------------

def page_comparables():
    st.title("Comparables por sector")
    st.caption("Empresas del watchlist (cifras de filings Latinex) contra peers "
               "internacionales (Yahoo Finance).")

    wl = load_watchlist()
    by_kind = {}
    for nemo in wl:
        try:
            s = summary(nemo)
            kind = fin_mod.sector_kind(s["sector"], s["industry"])
            by_kind.setdefault(kind, []).append(nemo)
        except LatinexAPIError:
            continue

    kind_labels = {"banking": "Banca", "insurance": "Seguros", "generic": "Otros sectores"}
    for kind, nemos in by_kind.items():
        st.subheader(kind_labels.get(kind, kind))
        rows = []
        for nemo in nemos:
            try:
                local = local_metrics_row(nemo)
                rows.append({**local, "country": "Panama", "market": "Latinex"})
            except Exception:
                continue
        if not rows:
            continue
        comp = pd.concat([pd.DataFrame(rows), peer_metrics(kind)], ignore_index=True)
        local_set = set(nemos)
        st.dataframe(peers_display(comp, local_set), hide_index=True, use_container_width=True)
        peers_charts(comp, local_set, f"cmp_{kind}")
        st.divider()


# ---------------------------------------------------------------------------
# Pagina 4: Exportar
# ---------------------------------------------------------------------------

def page_exportar():
    st.title("Exportar a Excel")
    st.caption("Genera un libro con el mercado, financieros del watchlist, dividendos, "
               "peers y los analisis generados en esta sesion.")

    if st.button("Generar archivo Excel"):
        with st.spinner("Construyendo libro..."):
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                try:
                    universe(False).to_excel(writer, sheet_name="Mercado", index=False)
                except LatinexAPIError:
                    pass

                ratio_rows, analysis_rows = [], []
                kinds_used = set()
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
                        y = api.get_dividend_yield(divs, q["price"])
                        ratio_rows.append({
                            "Ticker": nemo, "Precio": q["price"], "Market cap": q["market_cap"],
                            "EPS": r["eps"], "P/E": r["pe"], "BVPS": r["bvps"], "P/B": r["pb"],
                            "ROE %": r["roe_pct"], "ROA %": r["roa_pct"],
                            "Div yield 12m %": y["total_yield_pct"],
                            "Escala": fin["scale_label"], "Reporte": fin["report_name"],
                        })
                    if not divs.empty:
                        d = divs.copy()
                        d["payment_date"] = d["payment_date"].dt.strftime("%Y-%m-%d")
                        d["record_date"] = d["record_date"].dt.strftime("%Y-%m-%d")
                        d.insert(0, "ticker", nemo)
                        sheet = "Dividendos"
                        startrow = 0
                        if sheet in writer.sheets:
                            startrow = writer.sheets[sheet].max_row + 1
                        d.to_excel(writer, sheet_name=sheet, index=False,
                                   header=startrow == 0, startrow=startrow)
                    text = st.session_state.get("analyses", {}).get(nemo)
                    if text:
                        analysis_rows.append({"Ticker": nemo, "Analisis": text})

                if ratio_rows:
                    pd.DataFrame(ratio_rows).to_excel(writer, sheet_name="Ratios", index=False)
                for kind in kinds_used:
                    p = peer_metrics(kind)
                    if not p.empty:
                        p.to_excel(writer, sheet_name=f"Peers {kind}"[:31], index=False)
                if analysis_rows:
                    pd.DataFrame(analysis_rows).to_excel(writer, sheet_name="Analisis", index=False)

            st.download_button(
                "Descargar latinex_snapshot.xlsx",
                data=buf.getvalue(),
                file_name=f"latinex_snapshot_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if not st.session_state.get("analyses"):
            st.caption("Tip: abre la pagina Empresa para generar los analisis de Claude "
                       "antes de exportar y se incluiran en la hoja 'Analisis'.")


# ---------------------------------------------------------------------------
# Navegacion
# ---------------------------------------------------------------------------

render_sidebar()
nav = st.navigation([
    st.Page(page_mercado, title="Mercado", icon=":material/monitoring:"),
    st.Page(page_empresa, title="Empresa", icon=":material/business_center:", default=True),
    st.Page(page_comparables, title="Comparables", icon=":material/balance:"),
    st.Page(page_exportar, title="Exportar", icon=":material/download:"),
])
nav.run()
