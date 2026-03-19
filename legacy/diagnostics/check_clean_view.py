#!/usr/bin/env python3
"""segment_financials_clean VIEW の確認"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.migration.migration_db import MigrationDB

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decision_db.db")
print(f"DB: {db_path}")
db = MigrationDB(db_path)  # これでVIEW自動作成 + raw_profit_label マイグレーション

conn = db._conn

# clean件数
clean = conn.execute("SELECT COUNT(*) FROM segment_financials_clean").fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0]
excluded = total - clean
print(f"\nTotal: {total}, Clean: {clean}, Excluded: {excluded}")
print(f"  Exclusion rate: {excluded/total*100:.1f}%\n")

# 除外の内訳
print("=" * 60)
print("Excluded breakdown")
print("=" * 60)
for label, cond in [
    ("'売上'", "segment_name = '売上'"),
    ("'利益'", "segment_name = '利益'"),
    ("'#VALUE!'", "segment_name = '#VALUE!'"),
    ("'0'", "segment_name = '0'"),
    ("'月次売上'", "segment_name = '月次売上'"),
    ("'累計'", "segment_name = '累計'"),
    ("'ＧＰ'", "segment_name = 'ＧＰ'"),
    ("UNKNOWN_%", "segment_name LIKE 'UNKNOWN_%'"),
    ("NULL/empty", "segment_name IS NULL OR segment_name = ''"),
    ("sales+profit both NULL", "segment_sales IS NULL AND segment_profit IS NULL"),
]:
    cnt = conn.execute(f"SELECT COUNT(*) FROM segment_financials WHERE {cond}").fetchone()[0]
    if cnt > 0:
        print(f"  {label:<20}: {cnt:>6}")

# clean ticker 上位10
print(f"\n{'=' * 60}")
print("Clean: ticker top 10")
print("=" * 60)
rows = conn.execute(
    "SELECT company_code, COUNT(*) as c FROM segment_financials_clean "
    "GROUP BY company_code ORDER BY c DESC LIMIT 10"
).fetchall()
for code, c in rows:
    print(f"  {code}: {c}")

# clean segment_name 上位20
print(f"\n{'=' * 60}")
print("Clean: segment_name top 20")
print("=" * 60)
rows = conn.execute(
    "SELECT segment_name, COUNT(*) as c FROM segment_financials_clean "
    "GROUP BY segment_name ORDER BY c DESC LIMIT 20"
).fetchall()
for name, c in rows:
    print(f"  [{c:>4}] {name}")

# raw_profit_label カラム確認
print(f"\n{'=' * 60}")
print("raw_profit_label column check")
print("=" * 60)
cols = [r[1] for r in conn.execute("PRAGMA table_info(segment_financials)").fetchall()]
print(f"  Columns: {cols}")
has_rpl = "raw_profit_label" in cols
print(f"  raw_profit_label exists: {has_rpl}")

db.close()
print("\nDone.")
