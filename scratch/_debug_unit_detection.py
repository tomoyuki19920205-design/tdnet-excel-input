"""
6254 / 6258 / 6315 / 6466 の受注テーブルをダンプして単位検出の失敗原因を確認する
"""
import json, zipfile, sys, os, unicodedata, re
from pathlib import Path
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CACHE_DIR = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\edinet_cache")
SURVEY_JSON = Path(r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json')

TARGET_TICKERS = {"6254", "6258", "6315", "6466"}

_ORDER_KW = ["受注高", "受注額", "受注金額"]
_BACKLOG_KW = ["受注残高", "受注残"]
_SECTION_KW = _ORDER_KW + _BACKLOG_KW + ["繰越工事高", "完成工事高"]

def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("\n", "").replace("\u3000", "")

def _detect_unit_current(header_texts):
    """現在のロジック"""
    combined = " ".join(header_texts)
    combined_n = _norm(combined)
    if "百万円" in combined_n:
        return "百万円"
    if "千円" in combined_n:
        return "千円"
    if "億円" in combined_n:
        return "億円"
    if "円" in combined_n:
        return "円"
    return None

def dump_order_tables(ticker, doc_id):
    print(f"\n{'='*60}")
    print(f"[{ticker}] doc_id={doc_id}")
    print('='*60)
    
    zip_path = CACHE_DIR / doc_id / "xbrl.zip"
    if not zip_path.exists():
        print(f"  [ERROR] ZIP not found: {zip_path}")
        return
    
    with zipfile.ZipFile(zip_path) as z:
        htm_files = [n for n in z.namelist() if "PublicDoc" in n and n.endswith(".htm")]
        
        for fname in htm_files:
            raw = z.read(fname)
            try:
                text = raw.decode("utf-8")
            except Exception:
                text = raw.decode("cp932", errors="replace")
            
            soup = BeautifulSoup(text, "html.parser")
            tables = soup.find_all("table")
            
            for tidx, table in enumerate(tables):
                rows = table.find_all("tr")
                if not rows:
                    continue
                
                # 全行のセルテキスト（最初の5行）
                all_rows_text = []
                for row in rows[:5]:
                    cells = [_norm(c.get_text()) for c in row.find_all(["th", "td"])]
                    all_rows_text.append(cells)
                
                # 全テキスト結合して受注キーワード検索
                all_text = " ".join(
                    " ".join(cells) for cells in all_rows_text
                )
                has_order = any(kw in all_text for kw in _SECTION_KW)
                if not has_order:
                    continue
                
                # ヒットしたテーブルを詳細表示
                header_texts = all_rows_text[0] if all_rows_text else []
                unit_detected = _detect_unit_current(header_texts)
                
                print(f"\n  File: {fname}")
                print(f"  Table #{tidx}")
                print(f"  Unit detected (row0 only): {unit_detected}")
                print(f"  Row 0 (header): {header_texts[:6]}")
                
                # 全行を確認（最大5行）
                for ridx, cells in enumerate(all_rows_text):
                    print(f"  Row {ridx}: {cells[:6]}")
                
                # 全行のテキストに「千円」があるか確認
                full_table_text = " ".join(
                    " ".join(c for c in cells)
                    for cells in all_rows_text
                )
                print(f"  '千円' in full_table_text: {'千円' in full_table_text}")
                print(f"  '百万円' in full_table_text: {'百万円' in full_table_text}")
                
                # テーブル周辺のタイトルテキストも確認
                prev_sib = table.find_previous_sibling()
                if prev_sib:
                    prev_text = _norm(prev_sib.get_text())[:80]
                    print(f"  Prev sibling text: {prev_text}")
                
                parent_text = ""
                for parent in table.parents:
                    pt = _norm(parent.get_text()) if parent.name else ""
                    if "千円" in pt or "百万円" in pt:
                        # 最小のテキストを使う
                        if len(pt) < 200:
                            parent_text = pt[:100]
                            break
                if parent_text:
                    print(f"  Parent with unit: {parent_text}")
                
                print()

def main():
    survey = json.loads(SURVEY_JSON.read_text(encoding='utf-8'))
    
    for d in survey:
        ticker = d.get('ticker', '')
        if ticker not in TARGET_TICKERS:
            continue
        doc_id = d.get('doc_id')
        if not doc_id:
            print(f"[{ticker}] no doc_id")
            continue
        dump_order_tables(ticker, doc_id)

if __name__ == '__main__':
    main()
