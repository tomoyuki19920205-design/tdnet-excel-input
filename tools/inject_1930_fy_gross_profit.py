"""
1930 / 2026-03-31 / FY の gross_profit を
extract_financials_result.json → decision.db quarterly_results → Supabase canonical_financials
に反映する。

手順:
1. キャッシュの extract_financials_result.json から値を読む
2. decision.db quarterly_results に INSERT OR REPLACE
3. sqlite_to_supabase.py で Supabase canonical_financials に push
4. filings_process.py --phase canonical --target-tickers 1930 で financials まで反映
5. sync_financials.py --apply --ticker 1930 で jquants 由来も含めて financials 更新

usage:
  python tools/inject_1930_fy_gross_profit.py            # dry-run
  python tools/inject_1930_fy_gross_profit.py --apply    # 本番反映
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

# キャッシュパス固定
_CACHE_ID   = "ac36bb0ce684602202526165"
_CACHE_ROOT = os.path.join(_PROJECT_ROOT, "data", "tdnet_cache")
_CACHE_DIR  = os.path.join(_CACHE_ROOT, _CACHE_ID)
_FINANCIALS_JSON = os.path.join(_CACHE_DIR, "extract_financials_result.json")
_METADATA_JSON   = os.path.join(_CACHE_DIR, "metadata.json")
_XBRL_ZIP        = os.path.join(_CACHE_DIR, "xbrl.zip")

_DECISION_DB = os.path.join(_PROJECT_ROOT, "data", "decision.db")

# 期待する確認値
_EXPECTED_TICKER = "1930"
_EXPECTED_PERIOD = "2026-03-31"
_EXPECTED_QUARTER = "FY"


def _to_millions(v: float | int | None) -> float | None:
    """円単位 → 百万円。"""
    if v is None:
        return None
    return round(v / 1_000_000, 4)


def _zip_hash(path: str) -> str:
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser(
        description="1930 FY extract_financials_result → quarterly_results UPSERT"
    )
    parser.add_argument("--apply", action="store_true", help="DB に書き込む（省略時は dry-run）")
    args = parser.parse_args()
    apply = args.apply

    # ── 1. キャッシュ読み込み ──────────────────────────────
    if not os.path.exists(_FINANCIALS_JSON):
        print(f"[ERROR] extract_financials_result.json が見つかりません: {_FINANCIALS_JSON}")
        sys.exit(1)

    with open(_FINANCIALS_JSON, encoding="utf-8") as f:
        fin = json.load(f)

    meta = {}
    if os.path.exists(_METADATA_JSON):
        with open(_METADATA_JSON, encoding="utf-8") as f:
            meta = json.load(f)

    print()
    print("=" * 60)
    print("  INJECT: extract_financials_result → quarterly_results")
    print("=" * 60)
    print(f"  mode           : {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  cache_dir      : {_CACHE_DIR}")
    print(f"  ticker (meta)  : {meta.get('ticker', '?')}")
    print(f"  disclosure_date: {meta.get('disclosure_date', '?')}")
    print(f"  title          : {meta.get('title', '?')[:60]}")
    print()
    print("  --- extract_financials_result.json ---")
    print(f"  period (raw)   : {fin.get('period')}")
    print(f"  quarter (raw)  : {fin.get('quarter')!r}")
    print(f"  sales (円)     : {fin.get('sales'):,}" if fin.get("sales") else "  sales: None")
    print(f"  gross_profit(円): {fin.get('gross_profit'):,}" if fin.get("gross_profit") else "  gross_profit: None")
    print(f"  cost_of_sales  : {fin.get('cost_of_sales'):,}" if fin.get("cost_of_sales") else "  cost_of_sales: None")
    print(f"  operating_profit:{fin.get('operating_profit'):,}" if fin.get("operating_profit") else "  operating_profit: None")
    print()

    # quarter が空文字 → FY 開示として FY に設定
    raw_quarter = fin.get("quarter", "")
    quarter = raw_quarter if raw_quarter else "FY"
    if not raw_quarter:
        print(f"  [INFO] quarter が空 → '{quarter}' に設定（年次決算短信）")

    # period 変換: "R8/3" → "2026-03-31"
    raw_period = fin.get("period", "")
    if raw_period == "R8/3":
        period = "2026-03-31"
        print(f"  [INFO] period '{raw_period}' → '{period}'")
    else:
        period = raw_period  # 解釈できない場合はそのまま（要確認）

    company_code = _EXPECTED_TICKER
    sales_m       = _to_millions(fin.get("sales"))
    gross_profit_m = _to_millions(fin.get("gross_profit"))
    operating_profit_m = _to_millions(fin.get("operating_profit"))
    source_url    = meta.get("source_url", "")
    zip_h         = _zip_hash(_XBRL_ZIP)
    now_iso       = datetime.now(JST).isoformat()

    print("  --- quarterly_results に UPSERT する値 ---")
    print(f"  company_code   : {company_code}")
    print(f"  fiscal_year_end: {period}")
    print(f"  quarter        : {quarter}")
    print(f"  sales (百万円) : {sales_m}")
    print(f"  gross_profit(百万円): {gross_profit_m}")
    print(f"  operating_profit    : {operating_profit_m}")
    print(f"  unit           : 百万円")
    print(f"  source_doc_id  : {_CACHE_ID}")
    print(f"  source_url     : {source_url}")
    print()

    if gross_profit_m is None:
        print("[WARN] gross_profit が None です。UPSERT しても NULL になります。")

    if not apply:
        print("  ※ DRY-RUN のため quarterly_results は変更されません。")
        print("  ※ 本番反映するには --apply を付けて再実行してください。")
        print("=" * 60)
        return

    # ── 2. quarterly_results に UPSERT ───────────────────────
    if not os.path.exists(_DECISION_DB):
        print(f"[ERROR] decision.db が見つかりません: {_DECISION_DB}")
        sys.exit(1)

    conn = sqlite3.connect(_DECISION_DB)
    conn.row_factory = sqlite3.Row

    # 既存行確認
    existing = conn.execute(
        "SELECT * FROM quarterly_results WHERE company_code=? AND fiscal_year_end=? AND quarter=?",
        (company_code, period, quarter)
    ).fetchone()

    if existing:
        print(f"  [INFO] 既存行あり: id={existing['id']} gp={existing['gross_profit']}")
        print("         既存行を UPDATE します（gross_profit が NULL でない既存値は保持）")
        # gross_profit が既存で NOT NULL かつ 新規が None → 保持
        new_gp = gross_profit_m if gross_profit_m is not None else existing["gross_profit"]
        new_sales = sales_m if sales_m is not None else existing["sales"]
        new_op = operating_profit_m if operating_profit_m is not None else existing["operating_profit"]
        conn.execute(
            """
            UPDATE quarterly_results
            SET sales=?, gross_profit=?, operating_profit=?,
                unit='百万円', source_doc_id=?, source_url=?, zip_hash=?,
                parser_version='cache_inject_v1', updated_at=?
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            """,
            (new_sales, new_gp, new_op,
             _CACHE_ID, source_url, zip_h, now_iso,
             company_code, period, quarter)
        )
    else:
        print("  [INFO] 新規行を INSERT します")
        conn.execute(
            """
            INSERT INTO quarterly_results
              (company_code, fiscal_year_end, quarter,
               sales, gross_profit, operating_profit,
               unit, source_doc_id, source_url, zip_hash,
               parser_version, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (company_code, period, quarter,
             sales_m, gross_profit_m, operating_profit_m,
             "百万円", _CACHE_ID, source_url, zip_h,
             "cache_inject_v1", now_iso, now_iso)
        )

    conn.commit()

    # 確認
    row = conn.execute(
        "SELECT * FROM quarterly_results WHERE company_code=? AND fiscal_year_end=? AND quarter=?",
        (company_code, period, quarter)
    ).fetchone()
    conn.close()

    print()
    print("  ✅ quarterly_results UPSERT 完了")
    print(f"  id={row['id']} company_code={row['company_code']} period={row['fiscal_year_end']} q={row['quarter']}")
    print(f"  sales={row['sales']} gross_profit={row['gross_profit']} op={row['operating_profit']}")
    print()
    print("  次のステップ:")
    print("    1. sqlite_to_supabase.py → Supabase canonical_financials に push:")
    print("       .venv\\Scripts\\python.exe tools\\sqlite_to_supabase.py")
    print("    2. canonical sync → financials に反映:")
    print("       .venv\\Scripts\\python.exe tools\\filings_process.py --phase canonical --target-tickers 1930")
    print("=" * 60)


if __name__ == "__main__":
    main()
