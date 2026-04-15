#!/usr/bin/env python3
"""test_latest_eps_on_revision_folder.py

revision_pdfs フォルダ内の全 PDF を走査し、
latest_full_year_eps の有無を CSV に出力する。
"""
import csv
import os
import sys
import traceback
from glob import glob
from pathlib import Path

# ---------------------------------------------------------------------------
# プロジェクト src を sys.path に追加
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
INPUT_FOLDER = r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\revision_pdfs"
OUTPUT_CSV   = r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\revision_eps_test_results.csv"

# ---------------------------------------------------------------------------
# PDF テキスト抽出（pdfplumber を優先、なければフォールバック）
# ---------------------------------------------------------------------------
def _pdf_to_text(path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n".join(pages)
    except Exception:
        pass

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        pages = [doc[i].get_text() for i in range(len(doc))]
        return "\n".join(pages)
    except Exception:
        raise RuntimeError("pdfplumber / PyMuPDF どちらも利用不可")


# ---------------------------------------------------------------------------
# extract_latest_full_year_eps を直接 import（最小パス）
# ---------------------------------------------------------------------------
def _get_eps(text: str):
    """extract_latest_full_year_eps を呼ぶ。import 失敗時は None を返す。"""
    try:
        from events.forecast_extractor import extract_latest_full_year_eps
        return extract_latest_full_year_eps(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main():
    pdf_files = sorted(glob(os.path.join(INPUT_FOLDER, "*.pdf")))

    rows = []
    total = 0
    eps_non_null = 0
    errors = 0

    for pdf_path in pdf_files:
        pdf_name = os.path.basename(pdf_path)
        total += 1
        row = {
            "pdf_name": pdf_name,
            "latest_full_year_eps": "",
            "raw_table_text": "",
            "error": "",
        }

        try:
            text = _pdf_to_text(pdf_path)

            # まず extract_forecast_revision 経由を試みる
            eps_val = None
            raw_table = ""
            try:
                from events.forecast_extractor import extract_forecast_revision
                result = extract_forecast_revision(
                    text=text,
                    pdf_path=pdf_path,
                )
                # ForecastRevisionEvent または dict 両対応
                if isinstance(result, dict):
                    eps_val  = result.get("latest_full_year_eps")
                    raw_table = result.get("raw_table_text", "")
                else:
                    eps_val  = getattr(result, "latest_full_year_eps", None)
                    raw_table = getattr(result, "raw_table_text", "")
            except Exception:
                # フォールバック: 直接 extract_latest_full_year_eps を呼ぶ
                eps_val = _get_eps(text)

            row["latest_full_year_eps"] = "" if eps_val is None else str(eps_val)
            row["raw_table_text"] = str(raw_table or "")

            if eps_val is not None:
                eps_non_null += 1

        except Exception as e:
            row["error"] = traceback.format_exc(limit=3).strip()
            errors += 1

        rows.append(row)
        print(f"[{total:3d}] {pdf_name}  eps={row['latest_full_year_eps'] or 'None'}")

    # CSV 出力
    fieldnames = ["pdf_name", "latest_full_year_eps", "raw_table_text", "error"]
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"total      : {total}")
    print(f"eps_non_null: {eps_non_null}")
    print(f"errors     : {errors}")
    print(f"output     : {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
