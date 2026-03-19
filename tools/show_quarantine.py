#!/usr/bin/env python3
"""データ取りそこないログ — decision_db.db の quarantine + order_metrics を表示"""
import sqlite3
import os
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
db_path = "decision_db.db"

if not os.path.exists(db_path):
    print(f"  DB not found: {os.path.abspath(db_path)}")
    print("  (tdnet_ingest.py を一度実行すると作成されます)")
    sys.exit(0)

conn = sqlite3.connect(db_path)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()]

# === quarantine ===
print("=" * 60)
print("  quarantine (取りそこなったデータ)")
print("=" * 60)
if "quarantine" in tables:
    cnt = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
    print(f"  件数: {cnt}")
    if cnt > 0:
        print()
        # 理由の集計
        reasons = conn.execute(
            "SELECT reason, COUNT(*) as c FROM quarantine "
            "GROUP BY reason ORDER BY c DESC LIMIT 10"
        ).fetchall()
        print("  理由の上位:")
        for reason, c in reasons:
            print(f"    [{c:>3}件] {reason[:70]}")
        print()
        # 最新10件
        print("  最新10件:")
        rows = conn.execute(
            "SELECT company_code, fiscal_year_end, quarter, metric_type, reason "
            "FROM quarantine ORDER BY id DESC LIMIT 10"
        ).fetchall()
        print(f"  {'ticker':<8} {'period':<12} {'Q':<4} {'type':<16} reason")
        print(f"  {'-'*70}")
        for r in rows:
            print(f"  {r[0]:<8} {r[1]:<12} {r[2]:<4} {r[3]:<16} {(r[4] or '')[:40]}")
    else:
        print("  (取りそこなしデータなし - Good!)")
else:
    print("  (テーブル未作成 - tdnet_ingest.py を一度実行してください)")

# === order_metrics ===
print()
print("=" * 60)
print("  order_metrics (受注データ)")
print("=" * 60)
if "order_metrics" in tables:
    cnt = conn.execute("SELECT COUNT(*) FROM order_metrics").fetchone()[0]
    print(f"  件数: {cnt}")
    if cnt > 0:
        # metric別
        metrics = conn.execute(
            "SELECT metric_name, COUNT(*) as c FROM order_metrics "
            "GROUP BY metric_name ORDER BY metric_name"
        ).fetchall()
        print("  metric別:")
        for name, c in metrics:
            print(f"    {name}: {c}件")
        print()
        # 最新10件
        print("  最新10件:")
        rows = conn.execute(
            "SELECT company_code, fiscal_year_end, quarter, metric_name, value, unit "
            "FROM order_metrics ORDER BY id DESC LIMIT 10"
        ).fetchall()
        print(f"  {'ticker':<8} {'period':<12} {'Q':<4} {'metric':<32} {'value':>10} unit")
        print(f"  {'-'*76}")
        for r in rows:
            val = f"{r[4]:,.0f}" if r[4] is not None else "N/A"
            print(f"  {r[0]:<8} {r[1]:<12} {r[2]:<4} {r[3]:<32} {val:>10} {r[5]}")
    else:
        print("  (まだデータなし - 決算短信が処理されると入ります)")
else:
    print("  (テーブル未作成)")

conn.close()
print()
print("Done.")
