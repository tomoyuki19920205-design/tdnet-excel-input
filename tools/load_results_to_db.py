#!/usr/bin/env python3
# ============================================================
# load_results_to_db.py — XBRL抽出済みJSON → SQLite ETLローダー
# ============================================================
#
# 使い方:
#   python -m tools.load_results_to_db --db data/xbrl.db --input results/
#   python -m tools.load_results_to_db --db data/xbrl.db --input single.json
#
# 入力JSON形式 (1開示 = 1ファイル):
#   {
#     "ticker_code": "0812",
#     "company_name": "タムラ製作所",
#     "title": "2025年3月期 第3四半期決算短信",
#     "disclosed_at": "2025-02-14T15:00:00+09:00",
#     "url": "https://...",
#     "sha256": "abc123...",
#     "doc_type": "TANSHIN",
#     "source": "TDNET",
#     "fiscal_year_end": "2025-03-31",
#     "quarter": 3,
#     "source_unit": "百万円",
#     "values": {
#       "sales": 4915,
#       "gross_profit": 1200,
#       "op_income": 664,
#       "ordinary_income": null
#     },
#     "scope": "CONSOLIDATED",
#     "metric_type": "actual",
#     "quality": "IXBRL"
#   }
#
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.utils import parse_scale_unit

logger = logging.getLogger("etl")

# ============================================================
# メトリクスマッピング
# ============================================================
METRIC_MAP = {
    "sales": "NET_SALES",
    "gross_profit": "GROSS_PROFIT",
    "op_income": "OP_INCOME",
    "ordinary_income": "ORDINARY_INCOME",
}

# 有効なDDL定義値
VALID_SOURCES = ("TDNET", "EDINET", "MANUAL", "OTHER")
VALID_DOC_TYPES = ("TANSHIN", "REVISION", "PRESENTATION", "QA", "REPOST", "OTHER")
VALID_SCOPES = ("CONSOLIDATED", "NON_CONSOLIDATED")
VALID_QUALITIES = ("XBRL", "IXBRL", "PDF", "MANUAL")


# ============================================================
# DB操作ヘルパー
# ============================================================

def _upsert_company(
    conn: sqlite3.Connection,
    ticker_code: str,
    name_ja: str | None = None,
) -> int:
    """
    companiesテーブルにUPSERT。
    ticker_codeで検索し、存在すればname_ja等を更新、なければINSERT。

    Returns: company_id
    """
    cur = conn.execute(
        "SELECT company_id, name_ja FROM companies WHERE ticker_code = ?",
        (ticker_code,),
    )
    row = cur.fetchone()

    if row is not None:
        company_id = row[0]
        # name_jaが新たに提供され、かつ変更がある場合のみ更新
        if name_ja and name_ja != row[1]:
            conn.execute(
                "UPDATE companies SET name_ja = ?, updated_at = datetime('now') "
                "WHERE company_id = ?",
                (name_ja, company_id),
            )
        return company_id

    cur = conn.execute(
        "INSERT INTO companies (ticker_code, name_ja) VALUES (?, ?)",
        (ticker_code, name_ja),
    )
    return cur.lastrowid  # type: ignore


def _find_disclosure(
    conn: sqlite3.Connection,
    company_id: int,
    disclosed_at: str,
    title: str,
    sha256: str | None,
    url: str | None,
) -> int | None:
    """
    既存の開示を検索。sha256があれば優先、なければ (company_id, disclosed_at, title) で照合。

    Returns: disclosure_id or None
    """
    if sha256:
        cur = conn.execute(
            "SELECT disclosure_id FROM disclosures "
            "WHERE company_id = ? AND sha256 = ?",
            (company_id, sha256),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    cur = conn.execute(
        "SELECT disclosure_id FROM disclosures "
        "WHERE company_id = ? AND disclosed_at = ? AND title = ?",
        (company_id, disclosed_at, title),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _insert_disclosure(
    conn: sqlite3.Connection,
    company_id: int,
    source: str,
    disclosed_at: str,
    title: str,
    doc_type: str,
    url: str | None,
    sha256: str | None,
) -> int:
    """disclosuresテーブルにINSERT。Returns: disclosure_id"""
    cur = conn.execute(
        "INSERT INTO disclosures "
        "(company_id, source, disclosed_at, title, doc_type, url, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company_id, source, disclosed_at, title, doc_type, url, sha256),
    )
    return cur.lastrowid  # type: ignore


def _upsert_period(
    conn: sqlite3.Connection,
    company_id: int,
    fiscal_year_end: str,
    quarter: int,
) -> int:
    """
    periodsテーブルにUPSERT。
    (company_id, fiscal_year_end, quarter) で一意。

    Returns: period_id
    """
    cur = conn.execute(
        "SELECT period_id FROM periods "
        "WHERE company_id = ? AND fiscal_year_end = ? AND quarter = ?",
        (company_id, fiscal_year_end, quarter),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]

    # fiscal_yearをfiscal_year_endから算出
    fiscal_year = int(fiscal_year_end[:4])
    is_full_year = 1 if quarter == 4 else 0

    cur = conn.execute(
        "INSERT INTO periods "
        "(company_id, fiscal_year_end, fiscal_year, quarter, is_full_year) "
        "VALUES (?, ?, ?, ?, ?)",
        (company_id, fiscal_year_end, fiscal_year, quarter, is_full_year),
    )
    return cur.lastrowid  # type: ignore


def _insert_fact_if_not_exists(
    conn: sqlite3.Connection,
    company_id: int,
    period_id: int,
    disclosure_id: int,
    scope: str,
    metric: str,
    value: int,
    unit: str,
    quality: str,
    scale: int | None = None,
    confidence: int | None = None,
) -> str:
    """
    factsテーブルにINSERT。
    UNIQUE(disclosure_id, period_id, metric, scope)で重複チェック。
    既存なら更新せずスキップ。

    Returns: "inserted" or "skipped"
    """
    try:
        conn.execute(
            "INSERT INTO facts "
            "(company_id, period_id, disclosure_id, scope, metric, "
            " value, unit, scale, quality, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, period_id, disclosure_id, scope, metric,
             value, unit, scale, quality, confidence),
        )
        return "inserted"
    except sqlite3.IntegrityError:
        # UNIQUE制約違反 → 既存データあり、スキップ
        return "skipped"


def _insert_guidance_if_not_exists(
    conn: sqlite3.Connection,
    company_id: int,
    period_id: int,
    disclosure_id: int,
    metric: str,
    value: int | None,
    unit: str,
    quality: str,
    confidence: int | None = None,
) -> str:
    """
    guidanceテーブルにINSERT（重複は別disclosureからの追加なので許可）。

    Returns: "inserted"
    """
    conn.execute(
        "INSERT INTO guidance "
        "(company_id, period_id, disclosure_id, metric, value, unit, quality, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (company_id, period_id, disclosure_id, metric, value, unit, quality, confidence),
    )
    return "inserted"


# ============================================================
# 単位変換
# ============================================================

def _convert_to_jpy(raw_value: int | float, source_unit: str) -> int:
    """
    source_unit の値を円整数(JPY)に変換する。

    例: raw_value=4915, source_unit="百万円" → 4_915_000_000
    """
    multiplier = parse_scale_unit(source_unit)
    return int(round(raw_value * multiplier))


# ============================================================
# 1ファイル（1開示）の処理
# ============================================================

def load_single(conn: sqlite3.Connection, data: dict) -> dict:
    """
    1つのJSON dictをDBに投入する。

    Returns:
        {"status": "ok"|"skipped"|"error", "inserted": int, "skipped": int,
         "detail": str, "skip_reason": str|None}
    """
    result = {
        "status": "ok",
        "inserted": 0,
        "skipped": 0,
        "detail": "",
        "skip_reason": None,
    }

    # --- バリデーション ---
    ticker = data.get("ticker_code", "").strip()
    if not ticker:
        result["status"] = "error"
        result["detail"] = "ticker_code が空です"
        return result

    title = data.get("title", "").strip()
    if not title:
        result["status"] = "error"
        result["detail"] = "title が空です"
        return result

    disclosed_at = data.get("disclosed_at", "").strip()
    if not disclosed_at:
        result["status"] = "error"
        result["detail"] = "disclosed_at が空です"
        return result

    fiscal_year_end = data.get("fiscal_year_end", "").strip()
    quarter = data.get("quarter")
    if not fiscal_year_end or quarter is None:
        result["status"] = "error"
        result["detail"] = f"fiscal_year_end={fiscal_year_end}, quarter={quarter} が不足"
        return result

    quarter = int(quarter)
    if quarter not in (1, 2, 3, 4):
        result["status"] = "error"
        result["detail"] = f"quarter={quarter} は無効です (1-4)"
        return result

    values = data.get("values", {})
    if not values or all(v is None for v in values.values()):
        result["status"] = "error"
        result["detail"] = "values が空です"
        return result

    # デフォルト値
    source = data.get("source", "TDNET")
    if source not in VALID_SOURCES:
        source = "OTHER"

    doc_type = data.get("doc_type", "TANSHIN")
    if doc_type not in VALID_DOC_TYPES:
        doc_type = "OTHER"

    scope = data.get("scope", "CONSOLIDATED")
    if scope not in VALID_SCOPES:
        scope = "CONSOLIDATED"

    quality = data.get("quality", "IXBRL")
    if quality not in VALID_QUALITIES:
        quality = "MANUAL"

    metric_type = data.get("metric_type", "actual")
    source_unit = data.get("source_unit", "円")
    url = data.get("url")
    sha256 = data.get("sha256")
    company_name = data.get("company_name")

    # --- DB操作 ---
    # 1. company UPSERT
    company_id = _upsert_company(conn, ticker, company_name)

    # 2. disclosure 重複チェック → INSERT
    existing_disc = _find_disclosure(conn, company_id, disclosed_at, title, sha256, url)
    if existing_disc is not None:
        # 同一開示が既存 → factsの重複もスキップ
        disclosure_id = existing_disc
        logger.debug(f"  既存disclosure使用: id={disclosure_id}")
    else:
        disclosure_id = _insert_disclosure(
            conn, company_id, source, disclosed_at, title, doc_type, url, sha256,
        )

    # 3. period UPSERT
    period_id = _upsert_period(conn, company_id, fiscal_year_end, quarter)

    # 4. facts / guidance INSERT
    for raw_key, raw_value in values.items():
        if raw_value is None:
            continue

        metric = METRIC_MAP.get(raw_key)
        if metric is None:
            logger.warning(f"  未知のmetric key: {raw_key} (スキップ)")
            continue

        # 円整数に変換
        jpy_value = _convert_to_jpy(raw_value, source_unit)

        if metric_type == "guidance":
            status = _insert_guidance_if_not_exists(
                conn, company_id, period_id, disclosure_id,
                metric, jpy_value, "JPY", quality,
            )
        else:
            status = _insert_fact_if_not_exists(
                conn, company_id, period_id, disclosure_id,
                scope, metric, jpy_value, "JPY", quality,
            )

        if status == "inserted":
            result["inserted"] += 1
        else:
            result["skipped"] += 1

    if result["inserted"] == 0 and result["skipped"] > 0:
        result["status"] = "skipped"
        result["skip_reason"] = "DUPLICATE_FACT"
    elif result["inserted"] == 0:
        result["status"] = "error"
        result["detail"] = "有効な値がありませんでした"

    detail_parts = [
        f"{ticker}",
        f"{fiscal_year_end} Q{quarter}",
        f"ins={result['inserted']} skip={result['skipped']}",
    ]
    result["detail"] = " ".join(detail_parts)

    return result


# ============================================================
# バッチ処理
# ============================================================

def load_from_path(
    db_path: str,
    input_path: str,
    schema_path: str | None = None,
) -> dict:
    """
    ファイルまたはディレクトリからJSONを読み込みDBに投入する。

    Returns:
        {"processed": int, "inserted": int, "skipped": int, "errors": int,
         "skip_reasons": dict, "error_details": list}
    """
    # スキーマ適用（必要なら）
    if schema_path:
        from tools.migrate_db import migrate
        migrate(db_path, schema_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    summary = {
        "processed": 0,
        "inserted": 0,
        "skipped": 0,
        "errors": 0,
        "skip_reasons": {},
        "error_details": [],
    }

    try:
        json_files = _collect_json_files(input_path)
        if not json_files:
            logger.warning(f"[ETL] JSONファイルが見つかりません: {input_path}")
            return summary

        for json_path in json_files:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"[ETL] JSON読み込み失敗: {json_path} - {e}")
                summary["errors"] += 1
                summary["error_details"].append(f"{json_path}: {e}")
                summary["processed"] += 1
                continue

            # リスト形式（複数開示を1ファイルに含む）にも対応
            items = data if isinstance(data, list) else [data]

            for item in items:
                summary["processed"] += 1
                result = load_single(conn, item)

                if result["status"] == "error":
                    summary["errors"] += 1
                    summary["error_details"].append(result["detail"])
                    logger.error(f"[ETL] エラー: {result['detail']}")
                elif result["status"] == "skipped":
                    summary["skipped"] += 1
                    reason = result.get("skip_reason", "UNKNOWN")
                    summary["skip_reasons"][reason] = (
                        summary["skip_reasons"].get(reason, 0) + 1
                    )
                    logger.info(f"[ETL] スキップ: {result['detail']} ({reason})")
                else:
                    summary["inserted"] += result["inserted"]
                    logger.info(f"[ETL] 投入: {result['detail']}")

        conn.commit()
    finally:
        conn.close()

    # サマリログ
    skip_str = " ".join(f"{k}={v}" for k, v in summary["skip_reasons"].items())
    logger.info(
        f"[ETL] 完了: processed={summary['processed']} "
        f"inserted={summary['inserted']} skipped={summary['skipped']} "
        f"errors={summary['errors']}"
        + (f" ({skip_str})" if skip_str else "")
    )

    return summary


def _collect_json_files(path: str) -> list[str]:
    """ファイルまたはディレクトリからJSONファイルパスを収集する。"""
    p = Path(path)
    if p.is_file() and p.suffix.lower() == ".json":
        return [str(p)]
    if p.is_dir():
        return sorted(str(f) for f in p.glob("*.json"))
    return []


# ============================================================
# CLI エントリポイント
# ============================================================

def main():
    import io as _io

    # Windows cp932 対策
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = _io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    parser = argparse.ArgumentParser(
        description="XBRL抽出済みJSON → SQLite ETLローダー"
    )
    parser.add_argument(
        "--db", type=str, required=True,
        help="SQLiteデータベースファイルパス",
    )
    parser.add_argument(
        "--input", type=str, required=True,
        help="入力JSONファイルまたはディレクトリ",
    )
    parser.add_argument(
        "--auto-migrate", action="store_true",
        help="DB未初期化の場合にschema.sqlを自動適用",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="詳細ログ出力",
    )
    args = parser.parse_args()

    # ロガーセットアップ
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    schema = None
    if args.auto_migrate:
        schema = os.path.join(_PROJECT_ROOT, "schema.sql")

    print("=" * 60)
    print("  XBRL ETL ローダー")
    print("=" * 60)
    print(f"  DB    : {args.db}")
    print(f"  入力  : {args.input}")
    print()

    summary = load_from_path(
        db_path=args.db,
        input_path=args.input,
        schema_path=schema,
    )

    print("=" * 60)
    print("  結果サマリ")
    print("=" * 60)
    print(f"  処理件数    : {summary['processed']}")
    print(f"  INSERT      : {summary['inserted']}")
    print(f"  スキップ    : {summary['skipped']}")
    print(f"  エラー      : {summary['errors']}")

    if summary["skip_reasons"]:
        print("  [skip内訳]")
        for reason, count in summary["skip_reasons"].items():
            print(f"    {reason}: {count}")

    if summary["error_details"]:
        print("  [エラー詳細]")
        for detail in summary["error_details"][:10]:
            print(f"    {detail}")
    print()

    sys.exit(1 if summary["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
