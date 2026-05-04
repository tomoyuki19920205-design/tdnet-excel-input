#!/usr/bin/env python3
"""
6861 キーエンス FY2026-03 gross_profit 補完スクリプト

目的:
  canonical_financials に 6861/2026-03-20/FY/gross_profit が
  存在しないため、tdnet_cache XBRL から補完してSupabaseに書き込む。

Usage:
  # 調査のみ (dry-run)
  .venv\Scripts\python tools\patch_keyence_gross_profit.py --dry-run

  # 実際に書き込む
  .venv\Scripts\python tools\patch_keyence_gross_profit.py --apply

禁止:
  - sales=1,169,289 と OP=595,759 は上書きしない
  - 既存1Q/2Q/3Q行は触らない
  - gross_profit が既存なら上書きしない (INSERT, on_conflict=source_row_key)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.db import supabase_upsert
from src.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("patch_keyence_gp")
JST = timezone(timedelta(hours=9))

# ============================================================
# ターゲット
# ============================================================
TARGET_TICKER   = "6861"
TARGET_PERIOD   = "2026-03-20"   # disclosure_date (FY期末)
TARGET_QUARTER  = "FY"
# キーエンスFY2026-03 実績 (百万円) — XBRL確認後に更新する
# 出典: キーエンス 2026年3月期 決算短信 XBRL
# GrossProfit = 売上高 - 売上原価
# 売上高: 1,169,289百万円 / 売上原価は短信に記載なし → XBRLから取得
GROSS_PROFIT_VALUE = None  # ← XBRL調査後に設定する

# ============================================================
# tdnet_cache からキーエンスFY XBRLを探す
# ============================================================
def find_keyence_xbrl_cache() -> list[Path]:
    """tdnet_cache で 6861 / 68610 を含む extract_result JSON を探す。"""
    cache_dir = Path(_PROJECT_ROOT) / "data" / "tdnet_cache"
    hits = []
    for d in cache_dir.iterdir():
        if not d.is_dir():
            continue
        # meta.json があれば ticker を確認
        meta_f = d / "meta.json"
        if meta_f.exists():
            try:
                meta = json.loads(meta_f.read_text(encoding="utf-8"))
                code = str(meta.get("company_code", "") or meta.get("ticker", ""))
                if code in ("6861", "68610"):
                    hits.append(d)
                    continue
            except Exception:
                pass
        # ファイル名に 6861 を含む場合
        for f in d.iterdir():
            if "6861" in f.name or "68610" in f.name:
                hits.append(d)
                break
    return hits


def read_xbrl_gross_profit(cache_dirs: list[Path]) -> dict | None:
    """キャッシュディレクトリから gross_profit を読む。"""
    for d in cache_dirs:
        # extract_financials_result.json を探す
        for fname in ("extract_financials_result.json", "financials.json",
                      "xbrl_result.json", "result.json"):
            f = d / fname
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    gp = data.get("gross_profit") or data.get("GrossProfit")
                    if gp is not None:
                        return {"gross_profit": gp, "source_file": str(f),
                                "cache_dir": str(d)}
                except Exception:
                    pass
        # サブディレクトリも確認
        for sub in d.rglob("*.json"):
            try:
                data = json.loads(sub.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    gp = data.get("gross_profit") or data.get("GrossProfit")
                    if gp is not None and "6861" in str(sub):
                        return {"gross_profit": gp, "source_file": str(sub),
                                "cache_dir": str(d)}
            except Exception:
                pass
    return None


# ============================================================
# jquants.db から gross_profit を探す
# ============================================================
def check_jquants_gross_profit() -> dict | None:
    jq_path = Path(_PROJECT_ROOT) / "data" / "jquants.db"
    if not jq_path.exists():
        logger.warning("jquants.db が見つかりません")
        return None

    conn = sqlite3.connect(jq_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        logger.info(f"jquants.db tables: {tables}")

        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            col_set = set(c.lower() for c in cols)

            # gross_profit / GrossProfit カラムがあるテーブル
            gp_col = next((c for c in cols
                           if c.lower() in ("gross_profit", "grossprofit")), None)
            if not gp_col:
                continue

            # ticker/code カラム
            code_col = next((c for c in cols
                             if c.lower() in ("ticker","code","localcode",
                                              "company_code")), None)
            if not code_col:
                continue

            # period カラム
            period_col = next((c for c in cols
                               if "period" in c.lower() or "date" in c.lower()
                               or "fiscal" in c.lower()), None)

            rows = conn.execute(
                f"SELECT * FROM {t} WHERE {code_col} IN ('6861','68610') "
                + (f"AND {period_col} LIKE '2026-03%'" if period_col else "")
                + f" LIMIT 5"
            ).fetchall()

            if rows:
                logger.info(f"[jquants] テーブル={t} ヒット {len(rows)}行")
                for r in rows:
                    rd = dict(r)
                    logger.info(f"  {rd}")
                    if rd.get(gp_col) is not None:
                        return {
                            "table": t, "col": gp_col,
                            "gross_profit": rd[gp_col],
                            "row": rd,
                        }

        logger.info("[jquants] 6861 FY gross_profit は jquants.db に存在しません")
        return None
    finally:
        conn.close()


# ============================================================
# decision_db.db の quarterly_results / financials を確認
# ============================================================
def check_main_db_gross_profit() -> dict | None:
    db_path = Path(_PROJECT_ROOT) / "decision_db.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    found = None
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        logger.info(f"decision_db.db tables: {tables}")

        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            col_lower = [c.lower() for c in cols]

            if "gross_profit" not in col_lower:
                continue

            # ticker/code カラム
            code_col = next((c for c in cols
                             if c.lower() in ("ticker","code","company_code")), None)
            if not code_col:
                continue

            rows = conn.execute(
                f"SELECT * FROM {t} WHERE {code_col} IN ('6861','68610') LIMIT 5"
            ).fetchall()
            for r in rows:
                rd = dict(r)
                gp = rd.get("gross_profit")
                logger.info(f"[main_db] {t}: {rd}")
                if gp is not None and found is None:
                    found = {"table": t, "gross_profit": gp, "row": rd}
    finally:
        conn.close()

    return found


# ============================================================
# Supabase canonical_financials への書き込み
# ============================================================
def write_to_canonical(gross_profit_value: float, dry_run: bool, config: dict) -> dict:
    """canonical_financials に gross_profit を書き込む。"""
    metrics = {
        "gross_profit": gross_profit_value,
    }

    rows, skipped = expand_financials_rows(
        ticker=TARGET_TICKER,
        period=TARGET_PERIOD,
        quarter=TARGET_QUARTER,
        metrics_dict=metrics,
        source="summary_xbrl",   # XBRL由来 → 最高優先度(1)
        filing_id=f"tdnet_6861_FY2026",
        disclosure_datetime=None,
        correction_flag=False,
        unit="JPY",
    )

    logger.info(f"[write] rows={len(rows)} skipped={skipped}")
    for r in rows:
        logger.info(f"  {r}")

    if dry_run:
        logger.info("[DRY-RUN] Supabase 書き込みはスキップ")
        return {"written": 0, "skipped": skipped, "dry_run": True, "rows": rows}

    result = supabase_upsert(
        "canonical_financials",
        rows,
        on_conflict="source_row_key",
        config=config,
    )

    if result.get("ok"):
        logger.info(f"[write] ✅ canonical_financials に gross_profit を書き込みました")
    else:
        logger.error(f"[write] ❌ 書き込み失敗: {result.get('error')}")

    return {
        "written": len(rows) if result.get("ok") else 0,
        "skipped": skipped,
        "dry_run": False,
        "ok": result.get("ok"),
        "result": result,
    }


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="6861 キーエンス FY gross_profit 補完"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Supabase 書き込みをスキップ (default)")
    parser.add_argument("--apply", action="store_true",
                        help="実際に Supabase に書き込む")
    parser.add_argument("--gross-profit", type=float, default=None,
                        help="gross_profit 値を直接指定 (百万円)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.getLogger().setLevel(level)

    logger.info("=" * 60)
    logger.info(f"  6861 キーエンス FY gross_profit 補完")
    logger.info(f"  mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")
    logger.info(f"  target: {TARGET_TICKER} / {TARGET_PERIOD} / {TARGET_QUARTER}")
    logger.info("=" * 60)

    # ---- 1. jquants.db 確認 ----
    logger.info("\n[1] jquants.db 確認...")
    jq_result = check_jquants_gross_profit()

    # ---- 2. decision_db.db 確認 ----
    logger.info("\n[2] decision_db.db 確認...")
    db_result = check_main_db_gross_profit()

    # ---- 3. tdnet_cache XBRL 確認 ----
    logger.info("\n[3] tdnet_cache 確認...")
    cache_dirs = find_keyence_xbrl_cache()
    logger.info(f"  6861 キャッシュ: {len(cache_dirs)} ディレクトリ")
    for d in cache_dirs[:5]:
        logger.info(f"  - {d}")

    xbrl_result = read_xbrl_gross_profit(cache_dirs)
    if xbrl_result:
        logger.info(f"  XBRL gross_profit: {xbrl_result}")

    # ---- 4. gross_profit 値の確定 ----
    gross_profit = args.gross_profit

    if gross_profit is None and jq_result:
        gross_profit = jq_result["gross_profit"]
        logger.info(f"  → jquants より gross_profit={gross_profit}")

    if gross_profit is None and xbrl_result:
        gross_profit = xbrl_result["gross_profit"]
        logger.info(f"  → XBRL より gross_profit={gross_profit}")

    if gross_profit is None and db_result:
        gross_profit = db_result["gross_profit"]
        logger.info(f"  → decision_db より gross_profit={gross_profit}")

    if gross_profit is None:
        logger.error(
            "\n❌ gross_profit が自動取得できませんでした。\n"
            "   --gross-profit <値> で直接指定してください。\n"
            "   (例) --gross-profit 735000\n"
            "   ※ 単位: 百万円"
        )
        logger.info("\n【調査サマリー】")
        logger.info(f"  jquants.db: {'あり ' + str(jq_result) if jq_result else 'なし'}")
        logger.info(f"  decision_db: {'あり ' + str(db_result) if db_result else 'なし'}")
        logger.info(f"  tdnet_cache: {len(cache_dirs)} ディレクトリ (XBRL未取得)")
        logger.info(
            "\n次のステップ:\n"
            "  1. キーエンス 2026年3月期 決算短信 XBRL/PDF で gross_profit を確認\n"
            "  2. 以下で書き込む:\n"
            "     .venv\\Scripts\\python tools\\patch_keyence_gross_profit.py "
            "--apply --gross-profit <値>"
        )
        sys.exit(1)

    logger.info(f"\n✅ gross_profit 確定: {gross_profit:,.0f} 百万円")

    # ---- 5. Supabase 書き込み ----
    logger.info("\n[5] canonical_financials 書き込み...")
    config = load_config()
    result = write_to_canonical(gross_profit, dry_run=args.dry_run, config=config)

    # ---- サマリー ----
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  ticker:       {TARGET_TICKER}")
    print(f"  period:       {TARGET_PERIOD}")
    print(f"  quarter:      {TARGET_QUARTER}")
    print(f"  metric:       gross_profit")
    print(f"  value:        {gross_profit:,.0f} 百万円")
    print(f"  source:       summary_xbrl (priority=1)")
    print(f"  mode:         {'DRY-RUN' if args.dry_run else 'APPLIED'}")
    print(f"  written:      {result.get('written', 0)}")
    print("=" * 60)

    if args.dry_run:
        print("\n  本番適用するには --apply を付けて再実行してください。")
    else:
        if result.get("ok"):
            print("\n  ✅ canonical_financials に書き込み完了")
            print("  → viewer の 6861 FY gross_profit が表示されるはずです")
        else:
            print("\n  ❌ 書き込み失敗")


if __name__ == "__main__":
    main()
