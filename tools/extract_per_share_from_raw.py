#!/usr/bin/env python3
"""
extract_per_share_from_raw.py — jquants_financials_normalized.raw_json から
per_share_data テーブルへ EPS/BPS/配当/株式数をバックフィル。

新規APIコール不要。既存データからの抽出。

使い方:
  python tools/extract_per_share_from_raw.py --dry-run
  python tools/extract_per_share_from_raw.py --apply
  python tools/extract_per_share_from_raw.py --apply --limit 100
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker

logger = logging.getLogger("extract_per_share")

JST = timezone(timedelta(hours=9))
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# ============================================================
# マイグレーション（テーブル作成）
# ============================================================
_SCHEMA_SQL = Path(_PROJECT_ROOT) / "migrations" / "003_market_per_share.sql"

def _ensure_table(conn: sqlite3.Connection):
    """per_share_data テーブルが無ければ作成。"""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "per_share_data" not in tables:
        if _SCHEMA_SQL.exists():
            conn.executescript(_SCHEMA_SQL.read_text(encoding="utf-8"))
            conn.commit()
            logger.info("[SCHEMA] per_share_data テーブルを作成しました")
        else:
            logger.error(f"[SCHEMA] マイグレーションファイルが見つかりません: {_SCHEMA_SQL}")
            sys.exit(1)


# ============================================================
# raw_json からフィールド抽出
# ============================================================
def safe_float(val) -> float | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (ValueError, TypeError):
        return None


def safe_int(val) -> int | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _normalize_period(raw: str) -> str:
    raw = (raw or "").strip().upper()
    return raw if raw in ("1Q", "2Q", "3Q", "4Q", "FY") else (raw or "UNKNOWN")


def extract_per_share(raw: dict) -> dict | None:
    """raw_json (J-Quants /fins/statements レスポンス行) から per_share 指標を抽出。

    raw_json のキーは短縮名 (fetch_jquants_bulk.py が保存時に短縮):
      EPS, DEPS, BPS, DivAnn, Div1Q, Div2Q, Div3Q, DivFY,
      PayoutRatioAnn, FEPS, FDivAnn, FPayoutRatioAnn,
      NxFEPS, NxFDivAnn, NxFPayoutRatioAnn,
      ShOutFY, TrShFY, AvgSh, TotalAssets, Equity, EquityRatio

    forecast 採用ロジック (案A: 当期予想のみ):
      - FEPS (当期予想) のみ使用。NxFEPS (翌期予想) は入れない
      - FDivAnn のみ使用。NxFDivAnn は入れない
      - FPayoutRatioAnn のみ使用。NxFPayoutRatioAnn は入れない
      → 年度ズレ防止のため、翌期予想は初版では格納しない
    """
    # 銘柄コード: "Code" (短縮) or "LocalCode" (正式)
    code = (raw.get("Code") or raw.get("LocalCode") or "").strip()
    if not code:
        return None

    # 期末日: "CurFYEn" (短縮) or "CurrentFiscalYearEndDate" (正式)
    fy_end = (raw.get("CurFYEn") or raw.get("CurrentFiscalYearEndDate") or "").strip()
    # 四半期: "CurPerType" (短縮) or "TypeOfCurrentPeriod" (正式)
    period_type = (raw.get("CurPerType") or raw.get("TypeOfCurrentPeriod") or "").strip()
    # 開示日
    disclosed = (raw.get("DiscDate") or raw.get("DisclosedDate") or "").strip()

    if not fy_end or not period_type:
        return None

    ticker = normalize_ticker(code)
    quarter = _normalize_period(period_type)

    # forecast: 当期予想のみ (案A — NxF系は入れない)
    forecast_eps = safe_float(raw.get("FEPS"))
    forecast_div = safe_float(raw.get("FDivAnn"))
    forecast_payout = safe_float(raw.get("FPayoutRatioAnn"))

    return {
        "ticker": ticker,
        "period": fy_end,
        "quarter": quarter,
        "disclosed_date": disclosed or None,
        # 実績
        "eps": safe_float(raw.get("EPS")),
        "diluted_eps": safe_float(raw.get("DEPS")),
        "bps": safe_float(raw.get("BPS")),
        "dividend_q1": safe_float(raw.get("Div1Q")),
        "dividend_q2": safe_float(raw.get("Div2Q")),
        "dividend_q3": safe_float(raw.get("Div3Q")),
        "dividend_fy_end": safe_float(raw.get("DivFY")),
        "dividend_annual": safe_float(raw.get("DivAnn")),
        "payout_ratio": safe_float(raw.get("PayoutRatioAnn")),
        # 予想 (FEPS/FDivAnn 優先, Nx系フォールバック)
        "forecast_eps": forecast_eps,
        "forecast_dividend_annual": forecast_div,
        "forecast_payout_ratio": forecast_payout,
        # 株式数
        "shares_outstanding": safe_int(raw.get("ShOutFY")),
        "treasury_stock": safe_int(raw.get("TrShFY")),
        "avg_shares": safe_int(raw.get("AvgSh")),
        # BS
        "total_assets": safe_int(raw.get("TotalAssets")),
        "equity": safe_int(raw.get("Equity")),
        "equity_ratio": safe_float(raw.get("EquityRatio")),
        "source": "jquants",
    }


# ============================================================
# メイン処理
# ============================================================
def run(db_path: str, dry_run: bool = True, limit: int = 0) -> dict:
    stats = {"total_raw": 0, "extracted": 0, "upserted": 0, "skipped": 0, "errors": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)

    # raw_json を持つ全行を取得
    query = """
        SELECT local_code, current_fiscal_year_end_date, type_of_current_period,
               disclosed_date, raw_json
        FROM jquants_financials_normalized
        WHERE raw_json IS NOT NULL AND raw_json != ''
        ORDER BY local_code, current_fiscal_year_end_date, type_of_current_period
    """
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    stats["total_raw"] = len(rows)
    logger.info(f"[RAW] raw_json 付き: {len(rows):,} 行")

    # 重複排除: (ticker, period, quarter) → 最新の disclosed_date のデータを優先
    best: dict[tuple, dict] = {}

    for row in rows:
        raw_json_str = row["raw_json"]
        try:
            raw = json.loads(raw_json_str)
        except (json.JSONDecodeError, TypeError):
            stats["skipped"] += 1
            continue

        record = extract_per_share(raw)
        if not record:
            stats["skipped"] += 1
            continue

        stats["extracted"] += 1
        key = (record["ticker"], record["period"], record["quarter"])
        existing = best.get(key)

        if existing is None:
            best[key] = record
        else:
            # disclosed_date が新しい方を優先、ただしフィールドレベルCOALESCE
            if (record.get("disclosed_date") or "") >= (existing.get("disclosed_date") or ""):
                for field in record:
                    if record[field] is not None:
                        existing[field] = record[field]
            else:
                for field in record:
                    if existing[field] is None and record[field] is not None:
                        existing[field] = record[field]

    logger.info(f"[DEDUP] 重複排除後: {len(best):,} レコード")

    if dry_run:
        logger.info(
            f"\n{'='*60}\n"
            f"  DRY-RUN: UPSERT はスキップ\n"
            f"  対象: {len(best):,} レコード → per_share_data\n"
            f"{'='*60}\n"
            f"  本番反映するには --apply を付けて再実行してください\n"
            f"{'='*60}"
        )
        # サンプル表示
        for i, (key, rec) in enumerate(list(best.items())[:5]):
            logger.info(f"  サンプル {i+1}: {key} EPS={rec['eps']} BPS={rec['bps']} "
                        f"配当={rec['dividend_annual']} 株式数={rec['shares_outstanding']}")
        conn.close()
        return stats

    # UPSERT
    cols = [
        "ticker", "period", "quarter", "disclosed_date",
        "eps", "diluted_eps", "bps",
        "dividend_q1", "dividend_q2", "dividend_q3", "dividend_fy_end",
        "dividend_annual", "payout_ratio",
        "forecast_eps", "forecast_dividend_annual", "forecast_payout_ratio",
        "shares_outstanding", "treasury_stock", "avg_shares",
        "total_assets", "equity", "equity_ratio",
        "source", "updated_at",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols
        if c not in ("ticker", "period", "quarter")
    )
    sql = f"""
        INSERT INTO per_share_data ({', '.join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(ticker, period, quarter)
        DO UPDATE SET {updates}
    """

    now_iso = datetime.now(JST).isoformat()
    batch = []
    for rec in best.values():
        rec["updated_at"] = now_iso
        batch.append(tuple(rec.get(c) for c in cols))

    try:
        conn.executemany(sql, batch)
        conn.commit()
        stats["upserted"] = len(batch)
        logger.info(f"[UPSERT] {len(batch):,} レコードを per_share_data に書き込み")
    except Exception as e:
        logger.error(f"[UPSERT] 書き込みエラー: {e}")
        stats["errors"] += 1

    conn.close()
    return stats


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="raw_json → per_share_data バックフィル"
    )
    parser.add_argument("--db", default=_DEFAULT_DB, help="SQLite DB パス")
    parser.add_argument("--apply", action="store_true", help="本番反映")
    parser.add_argument("--dry-run", action="store_true", help="ドライラン (デフォルト)")
    parser.add_argument("--limit", type=int, default=0, help="処理行数上限")
    args = parser.parse_args()

    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "apply" if args.apply else "dryrun"
    log_file = os.path.join(_LOG_DIR, f"extract_per_share_{mode}_{ts}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    is_dry_run = not args.apply
    logger.info(f"=== extract_per_share {'DRY-RUN' if is_dry_run else 'APPLY'} ===")
    logger.info(f"  db: {args.db}")
    logger.info(f"  limit: {args.limit or '無制限'}")

    stats = run(args.db, dry_run=is_dry_run, limit=args.limit)

    logger.info(f"\n{'='*60}")
    logger.info(f"  完了")
    logger.info(f"  raw行数     : {stats['total_raw']:,}")
    logger.info(f"  抽出成功    : {stats['extracted']:,}")
    logger.info(f"  UPSERT      : {stats['upserted']:,}")
    logger.info(f"  スキップ    : {stats['skipped']:,}")
    logger.info(f"  エラー      : {stats['errors']}")
    logger.info(f"{'='*60}")

    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
