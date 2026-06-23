# Latinex Equity Tracker

Dashboard for the Panama stock market (Latinex / Latin American Stock Exchange):
prices, the live order book (bids & offers), dividends, financial statements parsed
from the quarterly filing PDFs (with OCR fallback for scanned reports), sector ratios,
annual history, international comparables (Yahoo Finance), and a McKinsey-style company
deep dive — verdict, scorecard, strengths/weaknesses and an ROE/DuPont value-driver
tree — generated with Claude.

## Pages

- **Market** — every listed common stock, the BVPSI index, YTD movers, and a per-instrument
  order book (bid/offer depth, spread, demand/supply imbalance).
- **Company Deep Dive** — McKinsey-style read on a company: scorecard, what's working /
  what needs watching, the ROE/DuPont tree, plus price & volume, valuation ratios, the
  3-year annual history, the long-form narrative, dividends, filings and the statements
  as reported.
- **Comparables** — watchlist names vs. international peers, in three views: a refined
  sheet, a valuation map (ROE vs P/E), and relative-position bars per metric.
- **Export** — one-click Excel workbook (market, order book, financials, ratios, peers,
  dividends, analyses).

## Run locally

```
pip install -r requirements.txt
python run.py        # http://localhost:8502
```

Create a `.env` file with:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### OCR for scanned filings (optional but recommended)

Some issuers (TRENCO, Grupo Melo/MHCH, CMBG, CMRealty, ...) publish image-only PDFs.
With OCR installed, those statements are parsed automatically; without it, the app still
runs and clearly flags them. Requirements:

1. `pip install -r requirements.txt` (installs `pymupdf`, `pytesseract`, `pillow`).
2. Install the **Tesseract** binary with Spanish data (`spa`). On Windows, if it is not
   on PATH, set `TESSERACT_CMD` to its full path, e.g.
   `C:/Program Files/Tesseract-OCR/tesseract.exe`.

Optional env: `LATINEX_OCR_DPI` (default 300), `LATINEX_OCR_LANG` (default `spa+eng`),
`LATINEX_MODEL` (analysis model, default `claude-opus-4-6`).

## Deploy on Streamlit Community Cloud

App entrypoint: `dashboard.py`. Required secrets (Settings > Secrets):

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
PUBLIC_MODE = "true"
ADMIN_KEY = "a-secret-key"
```

In public mode visitors see everything (cached data and analyses) but cannot regenerate
analyses, edit the watchlist or clear caches; the administrator enters the key in the
sidebar to enable those actions. (Note: Streamlit Cloud has no Tesseract binary by
default, so OCR of scanned filings runs only where Tesseract is installed.)

## Notes

- Data comes from latinexbolsa.com's undocumented JSON endpoints (no auth) and the PDFs
  on `files.latinexbolsa.com`. Prices are delayed up to 5 minutes.
- This is not investment advice; verify figures against the source PDFs.
- A static design prototype of the UI lives in `design/index.html`.
