from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# repo import を通す
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.events.forecast_extractor import extract_latest_full_year_eps


PDF_DIR = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\revision_pdfs")
OUT_CSV = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\revision_eps_from_pdfs.csv")


def extract_text_from_pdf(path: Path) -> str:
    text_parts: list[str] = []

    # まず pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                text_parts.append(t)
    except Exception:
        pass

    text = "\n".join(text_parts).strip()
    if text:
        return text

    # 次に pdfplumber
    text_parts = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t:
                    text_parts.append(t)
    except Exception:
        pass

    return "\n".join(text_parts).strip()


def extract_ticker(text: str, path: Path) -> str:
    # 本文優先
    patterns = [
        r"証券コード[:：]?\s*([0-9]{4,5})",
        r"コード[:：]?\s*([0-9]{4,5})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)[:4]

    # ファイル名から4桁
    m = re.search(r"\b([0-9]{4})\b", path.stem)
    if m:
        return m.group(1)

    # 5桁コードっぽい場合の先頭4桁
    m = re.search(r"\b([0-9]{5})\b", path.stem)
    if m:
        return m.group(1)[:4]

    return ""


def main() -> None:
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    rows: list[dict] = []

    print(f"pdf_count={len(pdfs)}")
    if not pdfs:
        print("PDFが見つかりません。")
        return

    for pdf in pdfs:
        text = extract_text_from_pdf(pdf)
        if not text:
            rows.append({
                "ticker": "",
                "file": pdf.name,
                "latest_full_year_eps": "",
                "status": "no_text",
            })
            print(f"[NO_TEXT] {pdf.name}")
            continue

        eps = extract_latest_full_year_eps(text)
        ticker = extract_ticker(text, pdf)

        rows.append({
            "ticker": ticker,
            "file": pdf.name,
            "latest_full_year_eps": "" if eps is None else eps,
            "status": "ok" if eps is not None else "eps_none",
        })

        print(f"[{'OK' if eps is not None else 'NONE'}] {ticker:>4} | {eps} | {pdf.name}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["ticker", "file", "latest_full_year_eps", "status"]
        )
        w.writeheader()
        w.writerows(rows)

    ok = sum(1 for r in rows if r["status"] == "ok")
    none = sum(1 for r in rows if r["status"] == "eps_none")
    no_text = sum(1 for r in rows if r["status"] == "no_text")

    print("")
    print("==== SUMMARY ====")
    print(f"ok={ok} eps_none={none} no_text={no_text} total={len(rows)}")
    print(f"csv={OUT_CSV}")


if __name__ == "__main__":
    main()
