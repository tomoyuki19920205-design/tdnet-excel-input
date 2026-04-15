import sqlite3
import json
import os

db_path = "C:/Users/takuy/OneDrive/tdnet-excel-input/data/backfill_state.db"

if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

filings = []
try:
    # タイトルに "決算短信" を含むものを全て抽出
    query = "SELECT * FROM filing_state WHERE title LIKE '%決算短信%'"
    rows = conn.execute(query).fetchall()
    print(f"Found {len(rows)} raw rows in DB")
    for r in rows:
        d = dict(r)
        f = {
            "filing_id": d.get("filing_id"),
            "ticker": d.get("ticker"),
            "title": d.get("title", ""),
            "disclosure_date": d.get("disclosure_date"),
            "doc_url": d.get("doc_url"),
            "xbrl_url": d.get("xbrl_url")
        }
        if f["filing_id"] and f["ticker"]:
            filings.append(f)
except Exception as e:
    print(f"Error: {e}")

conn.close()

with open("C:/Users/takuy/OneDrive/tdnet-excel-input/target_filings.json", "w", encoding="utf-8") as f:
    json.dump(filings, f, indent=2, ensure_ascii=False)

print(f"Exported {len(filings)} filings to target_filings.json")
