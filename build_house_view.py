# -*- coding: utf-8 -*-
"""
build_house_view.py -- rank all verified companies against each other (one
Claude call) and store the result in the snapshot for the Market page.

    python build_house_view.py
"""

import financials as fm
import analytics
import analyst
import snapshot as snap


def company_block(tk, e):
    q, s = e.get("quote") or {}, e.get("summary") or {}
    fin = e.get("financials") or {}
    ht = (e.get("historical") or {}).get("table")
    m = fm.extract_metrics(fin)
    r = fm.compute_ratios(fin, q.get("price"), s.get("shares_outstanding"))
    quarterly = fin.get("is_quarterly", True)
    tr = analytics.total_return(e.get("history_all"), e.get("dividends"))
    vb = analytics.valuation_bands(e.get("history_all"), ht,
                                   s.get("shares_outstanding"), quarterly)
    dp = analytics.dividend_profile(e.get("dividends"), q.get("price"), ht,
                                    s.get("shares_outstanding"), quarterly)
    lq = analytics.liquidity_score(e.get("history_all"), q, e.get("order_book_depth"))
    eq = analytics.earnings_quality(fin)
    verdict = ((e.get("deep_dive") or {}).get("data") or {}).get("verdict", "")

    def g(d, k, suf=""):
        v = d.get(k)
        return f"{v}{suf}" if v is not None else "n/a"

    def fy(metric, col):
        return analytics._fy_value(ht, metric, col)

    lines = [f"--- {tk} ({q.get('issuer_name', '')}) ---",
             f"Sector: {s.get('sector')} | Market cap: {q.get('market_cap'):,.0f}"
             if q.get("market_cap") else f"Sector: {s.get('sector')}",
             f"Deep-dive verdict: {verdict}",
             f"P/E: {g(r, 'pe', 'x')} | P/B: {g(r, 'pb', 'x')} | ROE: {g(r, 'roe_pct', '%')}",
             f"P/E percentile vs own 2023-todate history: "
             f"{g(vb['pe'] or {}, 'percentile')} (low = cheap vs itself)",
             f"Net income FY23/FY24/FY25: {fy('Net income', 'FY2023')} / "
             f"{fy('Net income', 'FY2024')} / {fy('Net income', 'FY2025')}",
             f"Total return: 1y {g(tr, 'tr_1y_pct', '%')} | 5y annualized {g(tr, 'tr_5y_pct', '%')}",
             f"Dividend: yield TTM {g(dp, 'ttm_yield_pct', '%')} | payout {g(dp, 'payout_pct', '%')} | "
             f"growth streak {g(dp, 'growth_streak_years', ' yrs')}",
             f"Liquidity grade: {lq.get('grade')} ({lq.get('grade_reason')})"]
    if eq.get("cash_conversion_pct") is not None:
        lines.append(f"Cash conversion (CFO/NI): {eq['cash_conversion_pct']}% | "
                     f"dividend coverage: {g(eq, 'div_coverage_x', 'x')}")
    return "\n".join(lines)


def main():
    data = snap.load() or {}
    ts = data.get("tickers", {})
    blocks = []
    for tk, e in sorted(ts.items()):
        fin = e.get("financials") or {}
        m = fm.extract_metrics(fin) if not fin.get("error") else {}
        if fin.get("vision_used") and not fin.get("error") and m.get("net_income") is not None:
            blocks.append(company_block(tk, e))
    if not blocks:
        print("no verified companies; aborting")
        return
    print(f"Ranking {len(blocks)} companies...")
    hv = analyst.generate_house_view("\n\n".join(blocks))
    if hv.get("error"):
        print("ERROR:", hv["error"])
        return
    from datetime import datetime
    data["house_view"] = {"overview": hv["overview"], "ranking": hv["ranking"],
                          "generated_at": datetime.now().isoformat(timespec="seconds")}
    snap.save(data)
    print("House view saved:")
    for it in hv["ranking"]:
        print(f"  #{it.get('rank')} {it.get('ticker')} [{it.get('stance')}] {it.get('one_liner')}")


if __name__ == "__main__":
    main()
