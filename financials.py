# -*- coding: utf-8 -*-
"""
financials.py -- Financial statement extraction from Latinex quarterly PDFs.

Downloads "Informe Trimestral" filings and extracts the income statement and
balance sheet via text parsing (the PDFs have no extractable table objects).

Capabilities:
- Latest interim statements (quarterly figures) per ticker.
- Historical ANNUAL statements: each Q4 filing contains the audited full-year
  statements; parsing Q4-2023/2024/2025 yields FY2023-FY2025.
- Sector-specific ratios: banking (BGFG/EGIN), insurance (ASSA), generic.

Parser design notes (fixes for known failure modes):
- Scale ("en miles de US$" vs "Cifras en balboas/dolares") is read from the
  SELECTED statement page itself, never guessed from magnitudes. A single
  filing can mix scales (BGFG: quarterly summary in thousands, audited annual
  statements in full balboas), so scale is resolved per selected page.
- Statement pages are identified by title lines that START with
  "estado ... de resultados/situacion" -- this excludes management-discussion
  pages, tables of contents, and character-spaced (unparseable) pages.
- Numbers are scanned RIGHT-TO-LEFT from each line; the label is whatever
  remains on the left. This survives parentheses and digits inside labels.
- A leading "Nota" reference column (small integers) is detected across rows
  and dropped before mapping values to periods.
- Operational-KPI and footnote rows (clients, branches, ATMs, ...) are dropped.
- Scanned-image PDFs (TRENCO) have no text layer and are reported as
  unparseable with an explicit error.
"""

import io
import os
import re
import unicodedata

import pandas as pd
import pdfplumber

import latinex_api as api

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

NUM_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?%?$")
QUARTER_DATE_RE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{2,4}")
YEAR_RE = re.compile(r"\b(20\d{2})\b")

JUNK_LABEL_PATTERNS = [
    "cliente", "sucursal", "atm", "colaborador", "canales digital",
    "bajo administracion", "pagina", "informacion operativa",
]

HEADER_PREFIXES = (
    "por los", "para los", "por el", "por tres", "al 31", "a el 31",
    "del 1", "nota ", "(en ", "(cifras",
)


def _norm(text):
    """Lowercase and strip accents / mojibake for keyword matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("�", "").lower()


def _parse_number(token):
    token = token.strip().rstrip("%")
    if not token or token in ("-", "—", "–"):
        return None
    neg = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        val = float(token)
        return -val if neg else val
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Page identification
# ---------------------------------------------------------------------------

def _is_garbled(page_text):
    """Detect character-spaced rendering ('In g re s o s p o r ...')."""
    tokens = page_text.split()
    if len(tokens) < 40:
        return False
    single = sum(1 for t in tokens if len(t) == 1)
    return single / len(tokens) > 0.35


def _classify_page(page_text):
    """Return 'income', 'balance', or None for a PDF page."""
    norm_text = _norm(page_text)
    if "indice del contenido" in norm_text[:400]:
        return None  # table of contents lists every statement title
    if _is_garbled(page_text):
        return None

    lines = [l.strip() for l in norm_text.split("\n") if l.strip()][:5]
    for line in lines:
        if not line.startswith("estado"):
            continue
        # Real statement titles are short; a long line is prose that happens
        # to start with "estado..." (ASSA's liquidity discussion).
        if len(line) > 70:
            continue
        if "flujos de efectivo" in line or "cambios en el patrimonio" in line:
            return None
        # Containment (not strict regex) tolerates in-PDF typos like
        # "estado consosolidado de situacion financiera" (EGIN Q1-2026).
        if "situacion financiera" in line or "posicion financiera" in line:
            return "balance"
        if "de resultados" in line:
            # "y otros resultados integrales" (ASSA) is the SAME statement;
            # only a standalone "utilidades integrales" page is different.
            if "utilidades integrales" in line:
                return None
            return "income"
    return None


def _is_annual_statement(page_text):
    """True when the statement header declares full-year figures.

    "ano terminado el 31 de diciembre" wins even when the page ALSO carries
    quarterly columns (BGFG's audited annuals show "IV Trimestre | Acumulado"
    side by side -- the accumulated columns are the annual figures).
    """
    head = _norm(page_text)[:500]
    if "ano terminado" in head and "diciembre" in head:
        return True
    if "trimestre" in head or "tres meses" in head:
        return False
    return "31 de diciembre" in head


def _extract_scale(page_text):
    """Read the scale declaration from the page. Returns (label, factor).

    Wording varies per issuer/year: "(en miles de US$)", "(Cifras en
    balboas)", "(Expresado en dolares de los Estados Unidos...)".
    """
    head = _norm(page_text)[:600]
    if "en millones" in head:
        return "en millones", 1_000_000
    if "en miles" in head:
        return "en miles de US$", 1_000
    if "en balboas" in head:
        return "Cifras en balboas (full units)", 1
    if "en dolares" in head:
        return "Cifras en dolares de EE.UU. (full units)", 1
    return None, None


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

def _split_line(line):
    """Split a line into (label, [number tokens]) scanning from the right."""
    parts = line.split()
    tokens = []
    while parts and NUM_RE.match(parts[-1]):
        tokens.insert(0, parts.pop())
        if len(tokens) > 8:
            break
    return " ".join(parts).strip(), tokens


def _is_junk_label(label):
    n = _norm(label)
    if len(n) < 4 or not n[0].isalpha():
        return True
    if n == "nota" or n.startswith(HEADER_PREFIXES):
        return True
    if ". ." in n or n.endswith(".."):  # table-of-contents dot leaders
        return True
    return any(p in n for p in JUNK_LABEL_PATTERNS)


def _parse_statement_lines(page_text):
    rows = []
    for line in page_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        label, tokens = _split_line(line)
        if not tokens or not label:
            continue
        if _is_junk_label(label):
            continue
        if "%" in " ".join(tokens):
            continue
        rows.append((label, tokens))
    return rows


def _strip_note_tokens(tokens):
    """Drop leading note-reference tokens before the real figures.

    Statements carry a "Nota" column that can hold SEVERAL references
    ("5, 6, 29, 30, 35") which tokenize as small integers ahead of the
    monetary values. Strip them only while a real figure (>= 1000)
    remains to the right, so tiny legitimate rows are left intact.
    """
    toks = list(tokens)

    def is_note(t):
        if "(" in t or "." in t:
            return False
        v = _parse_number(t)
        return v is not None and 0 < v < 200

    while toks and is_note(toks[0]) \
            and any((_parse_number(t) or 0) >= 1000 for t in toks[1:]):
        toks.pop(0)
    return toks


def _extract_periods(page_text, n_cols):
    header_zone = page_text.split("\n")[:10]

    for line in header_zone:
        dates = QUARTER_DATE_RE.findall(line)
        if len(dates) >= 2:
            return dates[:n_cols] if len(dates) >= n_cols else dates

    years = []
    for line in header_zone:
        for y in YEAR_RE.findall(line):
            if y not in years:
                years.append(y)
    years.sort(reverse=True)

    norm_text = _norm(page_text[:800])
    if years:
        if len(years) >= n_cols:
            return years[:n_cols]
        if len(years) == 2 and n_cols == 3 and "reexpresado" in norm_text:
            return [years[0], f"{years[1]} (reexpresado)", f"1-Ene-{years[1]} (reexpresado)"]
        return years + [f"Periodo {i + 1}" for i in range(len(years), n_cols)]
    return [f"Periodo {i + 1}" for i in range(n_cols)]


def _build_dataframe(rows, page_text):
    if not rows:
        return pd.DataFrame(), []

    cleaned = []
    for label, tokens in rows:
        toks = _strip_note_tokens(tokens)
        values = [_parse_number(t) for t in toks]
        if any(v is not None for v in values):
            cleaned.append((label, values))

    if not cleaned:
        return pd.DataFrame(), []

    counts = pd.Series([len(v) for _, v in cleaned])
    n_cols = int(counts.mode().iloc[0])

    # BGFG's audited annuals carry "IV Trimestre | Acumulado" with the year
    # pair repeated ("Nota 2025 2024 2025 2024"). The accumulated (rightmost)
    # pair holds the full-year figures -- keep only those.
    head = _norm(page_text)[:400]
    acumulado_layout = "acumulado" in head and "trimestre" in head
    years_in_head = []
    for y in YEAR_RE.findall(head):
        if y not in years_in_head:
            years_in_head.append(y)
    if acumulado_layout and len(years_in_head) >= 2 and n_cols >= 2 * len(years_in_head):
        keep = len(years_in_head)
        periods = sorted(years_in_head, reverse=True)[:keep]
        records = []
        for label, values in cleaned:
            if len(values) < n_cols:
                values = [None] * (n_cols - len(values)) + values
            records.append([label] + values[-keep:])
        df = pd.DataFrame(records, columns=["Line Item"] + periods)
        return df, periods

    periods = _extract_periods(page_text, n_cols)
    n_cols = min(n_cols, len(periods)) or len(periods)

    records = []
    for label, values in cleaned:
        if len(values) > n_cols:
            values = values[-n_cols:]
        elif len(values) < n_cols:
            values = [None] * (n_cols - len(values)) + values
        records.append([label] + values)

    df = pd.DataFrame(records, columns=["Line Item"] + periods[:n_cols])
    return df, periods[:n_cols]


# ---------------------------------------------------------------------------
# Report parsing core
# ---------------------------------------------------------------------------

def _parse_report(pdf_bytes, prefer_annual=False):
    """Parse one filing. Returns the same shape as get_financials()."""
    result = {"income": pd.DataFrame(), "balance": pd.DataFrame(),
              "periods": [], "scale_label": "", "scale_factor": None,
              "is_quarterly": False, "ocr_used": False, "error": None}

    # 1) Try the native text layer first (fast, exact).
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        scan_limit = min(45, len(pdf.pages))
        texts = {}
        total_chars = 0
        for i in range(scan_limit):
            text = pdf.pages[i].extract_text() or ""
            total_chars += len(text)
            texts[i] = text

    # 2) Scanned image PDFs (TRENCO, Melo/MHCH, CMBG, CMRealty, ...) have no
    #    text layer. Fall back to OCR so every issuer can be parsed.
    if total_chars < 500:
        try:
            import ocr
            texts = ocr.ocr_pdf_pages(pdf_bytes, max_pages=scan_limit)
            result["ocr_used"] = True
            total_chars = sum(len(t) for t in texts.values())
        except Exception as e:
            result["error"] = ("PDF has no text layer (scanned images) and the "
                               f"OCR fallback is unavailable: {e}")
            return result
        if total_chars < 500:
            result["error"] = ("PDF has no text layer and OCR recovered no text "
                               "(blank or non-textual scan).")
            return result

    # 3) Classify pages from whichever text source we ended up with.
    income_pages, balance_pages = [], []
    for i in range(scan_limit):
        kind = _classify_page(texts.get(i, ""))
        if kind == "income":
            income_pages.append(i)
        elif kind == "balance":
            balance_pages.append(i)

    # When annual figures are required, ONLY annual statement pages qualify --
    # silently falling back to a quarterly summary would mislabel 3-month
    # figures as full-year (accuracy over coverage).
    def _ordered(pages):
        if prefer_annual:
            return [i for i in pages if _is_annual_statement(texts[i])]
        return sorted(pages, key=lambda i: (_is_annual_statement(texts[i]), i))

    income_text = ""
    for pg in _ordered(income_pages):
        rows = _parse_statement_lines(texts[pg])
        if len(rows) < 5:
            continue
        df, periods = _build_dataframe(rows, texts[pg])
        if len(df) >= 5:
            result["income"] = df
            result["periods"] = periods
            income_text = texts[pg]
            break

    balance_text = ""
    for start in _ordered(balance_pages):
        combined_rows, combined_text = [], texts[start]
        pg = start
        while pg < scan_limit:
            text = texts.get(pg, "")
            if pg != start:
                kind = _classify_page(text)
                is_continuation = (
                    kind == "balance"
                    or ("total de pasivos" in _norm(text)
                        and "notas a los" not in _norm(text)[:200])
                )
                if not is_continuation:
                    break
            combined_rows.extend(_parse_statement_lines(text))
            if "total de pasivos y patrimonio" in _norm(text) \
                    or "total pasivos y patrimonio" in _norm(text):
                break
            pg += 1
            if pg - start >= 3:
                break
        if len(combined_rows) < 5:
            continue
        df, b_periods = _build_dataframe(combined_rows, combined_text)
        if len(df) >= 5:
            result["balance"] = df
            balance_text = combined_text
            if not result["periods"]:
                result["periods"] = b_periods
            break

    # Scale comes from the pages we actually selected (a filing can mix
    # thousands and full-unit statements).
    for text in (income_text, balance_text):
        if text:
            label, factor = _extract_scale(text)
            if label:
                result["scale_label"] = label
                result["scale_factor"] = factor
                break

    basis_text = _norm(income_text[:400]) if income_text else ""
    result["is_quarterly"] = bool(
        "trimestre" in basis_text or "tres meses" in basis_text
        or "tres primeros meses" in basis_text
        or (result["periods"]
            and QUARTER_DATE_RE.match(str(result["periods"][0])))
    )

    if result["income"].empty and result["balance"].empty:
        result["error"] = "Could not locate financial statements in the PDF"
    return result


def _get_pdf_cached(report_name, pdf_url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^\w\-.]", "_", report_name) + ".pdf"
    path = os.path.join(CACHE_DIR, safe)
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        with open(path, "rb") as f:
            return f.read()
    data = api.download_pdf_bytes(pdf_url)
    with open(path, "wb") as f:
        f.write(data)
    return data


def _empty_result(error=None):
    return {"income": pd.DataFrame(), "balance": pd.DataFrame(),
            "periods": [], "scale_label": "", "scale_factor": None,
            "is_quarterly": False, "ocr_used": False, "pdf_url": "",
            "report_name": "", "report_date": "", "error": error}


def get_financials(nemo, issuer_code=None):
    """Latest interim statements for a ticker (quarterly figures)."""
    result = _empty_result()
    try:
        if not issuer_code:
            issuer_code = api.get_quote(nemo)["issuer_code"]
        docs = api.get_documents(issuer_code)
        quarterly = docs[docs["type"] == "Informe Trimestral"]
        if quarterly.empty:
            result["error"] = f"No quarterly report found for {nemo}"
            return result

        latest = quarterly.iloc[0]
        result.update(pdf_url=latest["pdf_url"], report_name=latest["name"],
                      report_date=latest["date"])
        pdf_bytes = _get_pdf_cached(latest["name"], latest["pdf_url"])
        parsed = _parse_report(pdf_bytes, prefer_annual=False)
        result.update(parsed)
    except Exception as e:
        result["error"] = str(e)
    return result


def get_annual_financials(nemo, year, issuer_code=None):
    """Audited full-year statements from the Q4-{year} filing."""
    result = _empty_result()
    try:
        if not issuer_code:
            issuer_code = api.get_quote(nemo)["issuer_code"]
        docs = api.get_documents(issuer_code)
        quarterly = docs[docs["type"] == "Informe Trimestral"]
        match = quarterly[quarterly["name"].str.contains(f"{year}_Q4", case=False, na=False)]
        if match.empty:
            result["error"] = f"No Q4-{year} report found for {nemo}"
            return result

        doc = match.iloc[0]
        result.update(pdf_url=doc["pdf_url"], report_name=doc["name"],
                      report_date=doc["date"])
        pdf_bytes = _get_pdf_cached(doc["name"], doc["pdf_url"])
        parsed = _parse_report(pdf_bytes, prefer_annual=True)
        result.update(parsed)
        # Annual statements report twelve-month figures by definition.
        result["is_quarterly"] = False
    except Exception as e:
        result["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

def _find_value(df, periods, include, exclude=(), period=None):
    """Value of the first row whose label matches all rules, in the given
    period column (defaults to the latest). No stale-period fallback."""
    if df is None or df.empty or not periods:
        return None
    col = period if period is not None else periods[0]
    if col not in df.columns:
        return None
    for _, row in df.iterrows():
        n = _norm(str(row["Line Item"]))
        if any(k in n for k in exclude):
            continue
        if all(k in n for k in include):
            val = row[col]
            if val is not None and not pd.isna(val):
                return float(val)
    return None


def extract_metrics(fin, period=None):
    """Pull key line items from a parsed report, normalized to FULL USD.

    `period` selects an explicit column (e.g. the prior-year comparative);
    default is the latest period. Income metrics are period figures as filed
    (NOT annualized -- callers decide).
    """
    out = {}
    factor = fin.get("scale_factor")
    periods = fin.get("periods") or []
    if fin.get("error") or factor is None or not periods:
        return out

    inc, bal = fin["income"], fin["balance"]

    def find_i(include, exclude=()):
        v = _find_value(inc, periods, include, exclude, period=period)
        return v * factor if v is not None else None

    def find_b(include, exclude=()):
        v = _find_value(bal, periods, include, exclude, period=period)
        return v * factor if v is not None else None

    # --- shared / income ---
    ni_ctrl = (find_i(["utilidad neta", "controladora"], exclude=["no controladora"])
               or find_i(["accionistas", "controladora"], exclude=["no controladora"])
               or find_i(["utilidad neta", "propietarios"], exclude=["no controladora", "integrales"]))
    out["net_income"] = ni_ctrl if ni_ctrl is not None else \
        find_i(["utilidad neta"],
               exclude=["antes", "integrales", "operacional", "por accion"])
    out["net_income_is_controlling"] = ni_ctrl is not None
    out["pretax_income"] = find_i(["utilidad", "antes"], exclude=["integrales"])
    # Generic-company revenue (consumer/industrial) for net margin / asset yield.
    out["revenue"] = (find_i(["total de ingresos"], exclude=["intereses"])
                      or find_i(["ingresos de actividades ordinarias"])
                      or find_i(["ingresos por ventas"])
                      or find_i(["ventas netas"])
                      or find_i(["ingresos", "ordinarias"]))

    # --- banking lines ---
    out["interest_income"] = find_i(["total de ingresos por intereses"])
    out["interest_expense"] = find_i(["gastos por intereses"], exclude=["comisiones"])
    out["net_interest_income"] = find_i(["ingreso", "neto", "intereses"],
                                        exclude=["despues", "provision"]) \
        or find_i(["ingresos", "netos", "intereses"], exclude=["despues", "provision"])
    out["provision"] = find_i(["provision"], exclude=["reversa"])
    out["fees"] = find_i(["honorarios", "comisiones"])
    out["opex"] = find_i(["gastos generales"])
    out["loans"] = find_b(["prestamos"], exclude=["reserva", "neto", "intereses", "por pagar"])
    # Customer deposits (liability) -- never "depositos en bancos", which is
    # an interbank ASSET line that also matches a naive "depositos" search.
    out["deposits"] = (find_b(["depositos", "clientes"], exclude=["en banco"])
                       or find_b(["total", "depositos"], exclude=["en banco"]))

    # --- insurance lines (IFRS 17) ---
    out["insurance_revenue"] = find_i(["ingresos por servicios de seguro"])
    out["insurance_expense"] = find_i(["gastos por servicios de seguro"])
    out["reinsurance_result"] = find_i(["reaseguro"], exclude=["financiero", "financieros"])
    out["insurance_service_result"] = find_i(["resultado del servicio de seguro"])
    out["investment_return"] = find_i(["retorno de las inversiones"])
    out["investments"] = find_b(["instrumentos financieros"])
    out["insurance_provisions"] = find_b(["provisiones sobre contratos de seguros"])

    # --- balance ---
    # Generic (non-bank) balance sheets split into current / non-current, so the
    # naive "total + activos" search would hit "Total de activos corrientes"
    # first. Exclude the "corriente" subtotals so we get the grand totals.
    out["total_assets"] = (find_b(["total", "activos"], exclude=["corriente"])
                           or find_b(["total", "activos"]))
    out["total_equity"] = (find_b(["patrimonio", "controladora"], exclude=["no controladora"])
                           or find_b(["controladora"], exclude=["no controladora"])
                           or find_b(["patrimonio", "propietarios"], exclude=["no controladora"])
                           or find_b(["total", "patrimonio"], exclude=["pasivos"]))
    out["total_equity_incl_minority"] = find_b(["total", "patrimonio"], exclude=["pasivos"])
    out["total_liabilities"] = (find_b(["total", "pasivos"], exclude=["patrimonio", "corriente"])
                                or find_b(["total", "pasivos"], exclude=["patrimonio"]))

    return out


# ---------------------------------------------------------------------------
# Ratios
# ---------------------------------------------------------------------------

def compute_ratios(fin, price, shares_outstanding):
    """Core valuation/profitability ratios (full-USD basis via extract_metrics)."""
    out = {"net_income": None, "total_assets": None, "total_equity": None,
           "total_liabilities": None, "eps": None, "pe": None, "bvps": None,
           "pb": None, "roe_pct": None, "roa_pct": None,
           "equity_to_assets_pct": None, "note": ""}

    m = extract_metrics(fin)
    if not m:
        out["note"] = "Statements not parsed; ratios unavailable"
        return out

    annualize = 4 if fin.get("is_quarterly") else 1
    ni = m.get("net_income")
    eq = m.get("total_equity")
    ta = m.get("total_assets")

    out["net_income"] = ni
    out["total_assets"] = ta
    out["total_equity"] = eq
    out["total_liabilities"] = m.get("total_liabilities")
    if m.get("net_income_is_controlling"):
        out["note"] = "Net income: attributable to controlling interest"

    if ni and shares_outstanding:
        eps = ni * annualize / shares_outstanding
        out["eps"] = round(eps, 2)
        if price and eps:
            out["pe"] = round(price / eps, 2)
    if eq and shares_outstanding:
        bvps = eq / shares_outstanding
        out["bvps"] = round(bvps, 2)
        if price and bvps:
            out["pb"] = round(price / bvps, 2)
    if ni and eq:
        out["roe_pct"] = round(ni * annualize / eq * 100, 2)
    if ni and ta:
        out["roa_pct"] = round(ni * annualize / ta * 100, 2)
    if eq and ta:
        out["equity_to_assets_pct"] = round(eq / ta * 100, 2)
    return out


def dupont_decomposition(fin, kind=None):
    """Decompose ROE into its value levers for the deep-dive 'ROE tree'.

        ROE = ROA x Leverage
        ROA = Net margin x Asset yield
    and, for banks, the levers under those ratios (NIM, fee income, cost/income,
    cost of risk, effective tax). Every field is None when not computable, so
    the UI can grey out missing branches. All figures are annualized full-USD.

    Returns dict with *_pct / *_x fields plus the raw inputs and a note.
    """
    out = {"roe_pct": None, "roa_pct": None, "leverage_x": None,
           "net_margin_pct": None, "asset_yield_pct": None,
           "nim_pct": None, "fee_to_assets_pct": None, "cost_income_pct": None,
           "cost_of_risk_pct": None, "effective_tax_pct": None,
           "revenue": None, "net_income": None, "total_assets": None,
           "total_equity": None, "note": ""}

    m = extract_metrics(fin)
    if not m:
        out["note"] = "Statements not parsed; ROE tree unavailable"
        return out

    ann = 4 if fin.get("is_quarterly") else 1
    ni = m.get("net_income")
    eq = m.get("total_equity")
    ta = m.get("total_assets")
    if ni is not None:
        ni = ni * ann

    # Revenue proxy: banks earn net interest income + fees; fall back to
    # insurance/interest income for other sectors.
    nii = m.get("net_interest_income")
    fees = m.get("fees")
    if kind == "banking" or (nii is not None):
        revenue = ((nii or 0) + (fees or 0)) * ann or None
    else:
        rev_base = (m.get("insurance_revenue") or m.get("interest_income")
                    or m.get("revenue"))
        revenue = rev_base * ann if rev_base is not None else None
    out["revenue"] = revenue
    out["net_income"] = ni
    out["total_assets"] = ta
    out["total_equity"] = eq

    if ni is not None and eq:
        out["roe_pct"] = round(ni / eq * 100, 2)
    if ni is not None and ta:
        out["roa_pct"] = round(ni / ta * 100, 2)
    if ta and eq:
        out["leverage_x"] = round(ta / eq, 2)
    if ni is not None and revenue:
        out["net_margin_pct"] = round(ni / revenue * 100, 2)
    if revenue and ta:
        out["asset_yield_pct"] = round(revenue / ta * 100, 2)

    # Bank-specific levers
    if ta:
        if nii is not None:
            out["nim_pct"] = round(nii * ann / ta * 100, 2)
        if fees is not None:
            out["fee_to_assets_pct"] = round(fees * ann / ta * 100, 2)
    core_rev = ((nii or 0) + (fees or 0)) or None
    opex = m.get("opex")
    if opex is not None and core_rev:
        out["cost_income_pct"] = round(abs(opex) / core_rev * 100, 2)
    loans = m.get("loans")
    prov = m.get("provision")
    if prov is not None and loans:
        out["cost_of_risk_pct"] = round(abs(prov) * ann / loans * 100, 2)
    pretax = m.get("pretax_income")
    if pretax and m.get("net_income") is not None and pretax != 0:
        out["effective_tax_pct"] = round((1 - m["net_income"] / pretax) * 100, 2)

    if m.get("net_income_is_controlling"):
        out["note"] = "ROE uses net income attributable to controlling interest"
    return out


def sector_kind(sector, industry=""):
    """Map the Latinex sector/industry strings to a ratio family."""
    s = _norm(f"{sector} {industry}")
    if "seguro" in s:
        return "insurance"
    if "banco" in s or "financiero" in s:
        return "banking"
    return "generic"


def compute_sector_ratios(fin, kind):
    """Sector-specific ratio insights. Returns list of (label, value, help)."""
    m = extract_metrics(fin)
    if not m:
        return []
    ann = 4 if fin.get("is_quarterly") else 1

    def pct(num, den):
        if num is None or not den:
            return None
        return round(num / den * 100, 2)

    rows = []
    if kind == "banking":
        nii = m.get("net_interest_income")
        ta = m.get("total_assets")
        fees = m.get("fees")
        opex = m.get("opex")
        loans = m.get("loans")
        deposits = m.get("deposits")
        prov = m.get("provision")

        nim = pct(nii * ann if nii else None, ta)
        rows.append(("NIM (proxy)", f"{nim}%" if nim is not None else "-",
                     "Annualized net interest income / total assets"))
        core_rev = (nii or 0) + (fees or 0)
        eff = pct(opex, core_rev if core_rev else None)
        rows.append(("Efficiency", f"{eff}%" if eff is not None else "-",
                     "Operating expenses / (net interest income + fees); lower is better"))
        ltd = pct(loans, deposits)
        rows.append(("Loans/Deposits", f"{ltd}%" if ltd is not None else "-",
                     "Loan book / total deposits"))
        cor = pct(prov * ann if prov else None, loans)
        rows.append(("Cost of risk", f"{cor}%" if cor is not None else "-",
                     "Annualized provisions / loan book"))
        e2a = pct(m.get("total_equity"), ta)
        rows.append(("Capital/Assets", f"{e2a}%" if e2a is not None else "-",
                     "Equity (controlling) / total assets"))

    elif kind == "insurance":
        rev = m.get("insurance_revenue")
        svc = m.get("insurance_service_result")
        exp = m.get("insurance_expense")
        reins = m.get("reinsurance_result")
        inv = m.get("investments")
        inv_ret = m.get("investment_return")
        eq = m.get("total_equity")
        provisions = m.get("insurance_provisions")

        margin = pct(svc, rev)
        rows.append(("Insurance service margin", f"{margin}%" if margin is not None else "-",
                     "Insurance service result / insurance service revenue (IFRS 17)"))
        # Expense and reinsurance lines are negative in the statement.
        combined = None
        if rev and exp is not None and reins is not None:
            combined = round((abs(exp) + abs(reins)) / rev * 100, 2)
        rows.append(("Combined ratio (proxy)", f"{combined}%" if combined is not None else "-",
                     "(Insurance expense + net reinsurance cost) / revenue; <100% = profitable underwriting"))
        roi = pct(inv_ret * ann if inv_ret else None, inv)
        rows.append(("Investment return", f"{roi}%" if roi is not None else "-",
                     "Annualized investment return / financial instruments"))
        lev = None
        if inv and eq:
            lev = round(inv / eq, 2)
        rows.append(("Investments/Equity", f"{lev}x" if lev is not None else "-",
                     "Investment portfolio leverage over equity"))
        res = None
        if provisions and eq:
            res = round(provisions / eq, 2)
        rows.append(("Reserves/Equity", f"{res}x" if res is not None else "-",
                     "Insurance contract provisions / equity"))

    else:  # generic
        ni = m.get("net_income")
        rev = m.get("insurance_revenue") or m.get("interest_income") or m.get("revenue")
        margin = pct(ni, rev)
        if margin is not None:
            rows.append(("Net margin", f"{margin}%", "Net income / revenue"))
        de = None
        if m.get("total_liabilities") and m.get("total_equity"):
            de = round(m["total_liabilities"] / m["total_equity"], 2)
        rows.append(("Debt/Equity", f"{de}x" if de is not None else "-",
                     "Total liabilities / equity"))

    return rows


# ---------------------------------------------------------------------------
# Historical annual series
# ---------------------------------------------------------------------------

HIST_METRICS = [
    # (key, display label, is_income_metric)
    ("net_income", "Net income", True),
    ("net_interest_income", "Net interest income", True),
    ("insurance_revenue", "Insurance revenue", True),
    ("insurance_service_result", "Insurance service result", True),
    ("fees", "Fees & commissions", True),
    ("opex", "Operating expenses", True),
    ("loans", "Loans", False),
    ("deposits", "Deposits", False),
    ("investments", "Financial instruments", False),
    ("total_assets", "Total assets", False),
    ("total_equity", "Equity (controlling)", False),
    ("total_liabilities", "Total liabilities", False),
]


def get_historical(nemo, years=(2023, 2024, 2025), issuer_code=None):
    """Annual metric table: FY columns (full USD) + latest interim.

    Returns dict: table (DataFrame, metrics x periods), sources (list of
    (column, report_name, pdf_url)), errors (list of str).
    """
    if not issuer_code:
        issuer_code = api.get_quote(nemo)["issuer_code"]

    columns, sources, errors = {}, [], []
    parsed_annuals = {}

    for year in years:
        fin = get_annual_financials(nemo, year, issuer_code=issuer_code)
        if fin["error"]:
            errors.append(f"FY{year}: {fin['error']}")
            continue
        parsed_annuals[year] = fin
        m = extract_metrics(fin)
        if not m:
            errors.append(f"FY{year}: metrics not extracted")
            continue
        col = f"FY{year}"
        columns[col] = m
        sources.append((col, fin["report_name"], fin["pdf_url"]))

    # Fill gaps from comparative columns: each annual statement carries the
    # prior year alongside (e.g. ASSA's FY2023 filing is a scanned image,
    # but FY2023 figures appear -- possibly restated -- in the FY2024 report).
    for year in years:
        col = f"FY{year}"
        if col in columns:
            continue
        for src_year in sorted(parsed_annuals, reverse=True):
            fin = parsed_annuals[src_year]
            comp = next((p for p in (fin["periods"] or [])[1:]
                         if str(year) in str(p)), None)
            if not comp:
                continue
            m = extract_metrics(fin, period=comp)
            if m and any(v is not None for k, v in m.items()
                         if k != "net_income_is_controlling"):
                columns[col] = m
                sources.append((f"{col} (comparativo)", fin["report_name"], fin["pdf_url"]))
                errors = [e for e in errors if not e.startswith(f"FY{year}:")]
                break

    latest = get_financials(nemo, issuer_code=issuer_code)
    if not latest["error"]:
        m = extract_metrics(latest)
        if m:
            label = "Ultimo interino"
            qmatch = re.search(r"(20\d{2})_Q(\d)", latest["report_name"] or "")
            if qmatch:
                label = f"Q{qmatch.group(2)}-{qmatch.group(1)}"
                if latest["is_quarterly"]:
                    label += " (3M)"
            columns[label] = m
            sources.append((label, latest["report_name"], latest["pdf_url"]))
    else:
        errors.append(f"Latest: {latest['error']}")

    if not columns:
        return {"table": pd.DataFrame(), "sources": sources, "errors": errors}

    # FY columns in chronological order, interim column last.
    ordered_cols = sorted([c for c in columns if c.startswith("FY")]) \
        + [c for c in columns if not c.startswith("FY")]

    rows = []
    for key, label, _is_inc in HIST_METRICS:
        vals = {col: columns[col].get(key) for col in ordered_cols}
        if any(v is not None for v in vals.values()):
            rows.append({"Metric": label, **vals})
    table = pd.DataFrame(rows, columns=["Metric"] + ordered_cols)
    return {"table": table, "sources": sources, "errors": errors}


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    for nemo in ["BGFG", "EGIN", "ASSA", "TRENCO"]:
        print(f"\n===== {nemo} =====")
        q = api.get_quote(nemo)
        s = api.get_summary(nemo)
        kind = sector_kind(s["sector"], s["industry"])
        print(f"  Sector: {s['sector']}/{s['industry']} -> {kind}")

        fin = get_financials(nemo, issuer_code=q["issuer_code"])
        if fin["error"]:
            print(f"  Latest: ERROR {fin['error']}")
        else:
            r = compute_ratios(fin, q["price"], s["shares_outstanding"])
            print(f"  Latest {fin['report_name']} ({fin['scale_label']}): "
                  f"EPS={r['eps']} P/E={r['pe']} P/B={r['pb']} ROE={r['roe_pct']}%")
            for label, val, _ in compute_sector_ratios(fin, kind):
                print(f"    {label}: {val}")

        hist = get_historical(nemo, issuer_code=q["issuer_code"])
        if hist["errors"]:
            print(f"  Hist errors: {hist['errors']}")
        if not hist["table"].empty:
            with pd.option_context("display.float_format", "{:,.0f}".format,
                                   "display.width", 200):
                print(hist["table"].to_string(index=False))
