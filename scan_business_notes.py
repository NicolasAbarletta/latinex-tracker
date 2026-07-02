# -*- coding: utf-8 -*-
"""
scan_business_notes.py -- find, in each company's latest ANNUAL filing, the
pages carrying business-description and segment-information notes:
  - "Organizacion y operaciones" / "Informacion general" (note 1: what they do)
  - "Informacion por segmentos" / "Informacion de segmentos" (revenue by line)
Text layer first (fast); OCR fallback for scanned filings. Prints a page map
per company so the report extractor knows what exists. No API calls.
"""

import io
import os
import sys

os.environ.setdefault("LATINEX_OCR_SUBPROCESS", "1")

import pdfplumber

import latinex_api as api
import financials as fm
import ocr

BUSINESS_KEYS = ["organizacion y operaciones", "informacion general",
                 "constitucion y operaciones", "operaciones y actividades"]
SEGMENT_KEYS = ["informacion por segmentos", "informacion de segmentos",
                "segmentos de operacion", "informacion financiera por segmentos"]


def page_texts(pdf_bytes, limit=60):
    texts = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = min(limit, len(pdf.pages))
        for i in range(n):
            texts[i] = pdf.pages[i].extract_text() or ""
    if sum(len(t) for t in texts.values()) < 500:  # scanned -> OCR
        texts = ocr.ocr_pdf_pages(pdf_bytes, max_pages=limit)
    return texts


def scan(nemo):
    q = api.get_quote(nemo)
    docs = api.get_documents(q["issuer_code"])
    quarterly = docs[docs["type"] == "Informe Trimestral"]
    q4 = quarterly[quarterly["name"].str.contains("Q4", case=False, na=False)]
    doc = (q4 if not q4.empty else quarterly).iloc[0]
    pdf = fm._get_pdf_cached(doc["name"], doc["pdf_url"])
    texts = page_texts(pdf)

    biz, seg = [], []
    for i in sorted(texts):
        n = fm._norm(texts[i])
        if any(k in n for k in BUSINESS_KEYS):
            biz.append(i)
        if any(k in n for k in SEGMENT_KEYS):
            seg.append(i)
    print(f"{nemo:7} report={doc['name']:24} pages={len(texts)} "
          f"business_note_pages={biz[:4]} segment_pages={seg[:4]}", flush=True)
    return {"report": doc["name"], "business_pages": biz, "segment_pages": seg}


if __name__ == "__main__":
    for tk in (sys.argv[1:] or ["BGFG", "EGIN", "ASSA", "MELO", "CMBG", "PPHO", "TRENCO"]):
        try:
            scan(tk)
        except Exception as e:
            print(f"{tk}: ERROR {e}", flush=True)
