#!/usr/bin/env python3
"""
6861 キーエンス FY gross_profit 欠損調査スクリプト

Usage:
    cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
    .venv\\Scripts\\python tools\\investigate_keyence_gp.py
"""
import os, sqlite3, json, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print("="*60)

# ============================================================
# 1. jquants.db の 6861 FY gross_profit
# ============================================================
sep("1. jquants.db — 6861 FY gross_profit")
jq_path = ROOT / "data" / "jquants.db"
try:
    conn = sqlite3.connect(jq_path); conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"テーブル一覧: {tables}")

    # 財務系テーブルを全探索
    for t in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if any(k in ' '.join(cols).lower() for k in ['gross', 'gp', 'financial']):
            print(f"\n[{t}] columns: {cols}")
            # ticker/code カラムを特定
            code_col = next((c for c in cols if c in ('ticker','code','Code','LocalCode','company_code')), None)
            period_col = next((c for c in cols if 'period' in c.lower() or 'date' in c.lower() or 'fiscal' in c.lower()), None)
            gp_col = next((c for c in cols if 'gross' in c.lower() or c == 'GrossProfit'), None)
            print(f"  code_col={code_col} period_col={period_col} gp_col={gp_col}")
            if code_col and gp_col:
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE {code_col} IN ('6861','68610') "
                    f"ORDER BY {period_col} DESC LIMIT 10" if period_col else
                    f"SELECT * FROM {t} WHERE {code_col} IN ('6861','68610') LIMIT 10"
                ).fetchall()
                for r in rows:
                    print(f"  {dict(r)}")

    conn.close()
except Exception as e:
    print(f"jquants.db エラー: {e}")

# ============================================================
# 2. decision_db.db — quarterly_results の 6861 FY
# ============================================================
sep("2. decision_db.db — quarterly_results 6861 FY 2026-03")
db_path = ROOT / "decision_db.db"
try:
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    # テーブル確認
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"テーブル: {tables}")

    # quarterly_results
    if "quarterly_results" in tables:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(quarterly_results)").fetchall()]
        print(f"quarterly_results cols: {cols}")
        rows = conn.execute(
            "SELECT * FROM quarterly_results "
            "WHERE ticker IN ('6861','68610') AND period_end LIKE '2026-03%' "
            "ORDER BY quarter"
        ).fetchall()
        print(f"6861 2026-03 件数: {len(rows)}")
        for r in rows:
            print(f"  {dict(r)}")

        # 全quarterのgross_profit確認
        if "gross_profit" in cols:
            gp_rows = conn.execute(
                "SELECT quarter, period_end, gross_profit, sales, operating_profit "
                "FROM quarterly_results "
                "WHERE ticker IN ('6861','68610') ORDER BY period_end DESC LIMIT 8"
            ).fetchall()
            print("\n6861 直近gross_profit:")
            for r in gp_rows:
                print(f"  {dict(r)}")
    else:
        print("quarterly_results テーブルなし")

    # financials テーブルも確認
    if "financials" in tables:
        cols2 = [c[1] for c in conn.execute("PRAGMA table_info(financials)").fetchall()]
        print(f"\nfinancials cols: {cols2}")
        if "gross_profit" in cols2:
            rows2 = conn.execute(
                "SELECT ticker, period_end, quarter, gross_profit, sales, operating_profit "
                "FROM financials WHERE ticker IN ('6861','68610') "
                "ORDER BY period_end DESC LIMIT 8"
            ).fetchall()
            for r in rows2: print(f"  {dict(r)}")
    conn.close()
except Exception as e:
    print(f"decision_db.db エラー: {e}")

# ============================================================
# 3. TDNET XBRLキャッシュ — 6861 FY
# ============================================================
sep("3. TDNET XBRLキャッシュ — 6861 FY")
xbrl_archive = ROOT / "data" / "xbrl_archive"
tdnet_cache  = ROOT / "data" / "tdnet_cache"

for d, label in [(xbrl_archive, "xbrl_archive"), (tdnet_cache, "tdnet_cache")]:
    if not d.exists():
        print(f"[{label}] ディレクトリなし")
        continue
    # 6861 を含むファイル/ディレクトリ
    hits = sorted(d.rglob("*6861*"))[:10]
    print(f"[{label}] 6861 ヒット: {len(hits)}件")
    for h in hits:
        print(f"  {h.relative_to(ROOT)} ({h.stat().st_size if h.is_file() else 'dir'})")

# ============================================================
# 4. extract_financials_result.json — gross_profit
# ============================================================
sep("4. extract_financials_result.json 検索")
# よくある出力先を確認
candidates = list(ROOT.glob("**/extract_financials_result*.json"))[:5]
candidates += list(ROOT.glob("**/*6861*result*.json"))[:5]
print(f"候補JSON: {candidates}")
for p in candidates[:3]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            gp = data.get("gross_profit") or data.get("GrossProfit")
            print(f"  {p.name}: gross_profit={gp}")
    except Exception as e:
        print(f"  {p.name}: {e}")

# ============================================================
# 5. xbrl.db 確認
# ============================================================
sep("5. data/xbrl.db — 6861 FY")
xbrl_db = ROOT / "data" / "xbrl.db"
if xbrl_db.exists():
    try:
        conn = sqlite3.connect(xbrl_db); conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"テーブル: {tables}")
        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            print(f"  {t}: {cols}")
            code_col = next((c for c in cols if 'ticker' in c.lower() or 'code' in c.lower()), None)
            if code_col:
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE {code_col} IN ('6861','68610') LIMIT 5"
                ).fetchall()
                for r in rows: print(f"    {dict(r)}")
        conn.close()
    except Exception as e:
        print(f"xbrl.db エラー: {e}")
else:
    print("xbrl.db なし")

print("\n=== 調査完了 ===")
