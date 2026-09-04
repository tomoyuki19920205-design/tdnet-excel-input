#!/usr/bin/env python3
"""
fetch_jquants_details.py — J-Quants Premium 詳細財務API 検証・保存

目的:
  /fins/summary に存在しない gross_profit を詳細財務API から取得する。
  候補: /v1/fins/statements / /v2/fins/statements
  まず 6861 単体で検証し、成功後に jquants.db 保存 → canonical_financials 書き込み。
  ※ 正しいエンドポイントは tools/probe_jquants_endpoints.py で確認してください。

Usage:
  # Step1: APIレスポンス項目名を確認 (dry-run)
  .venv\\Scripts\\python -X utf8 tools\\fetch_jquants_details.py --ticker 6861 --dry-run

  # Step2: jquants.db に保存
  .venv\\Scripts\\python -X utf8 tools\\fetch_jquants_details.py --ticker 6861 --apply

  # Step3: canonical_financials に書き込む
  .venv\\Scripts\\python -X utf8 tools\\fetch_jquants_details.py --ticker 6861 --apply --push-canonical

  # Step4: 過去30日分 (全銘柄)
  .venv\\Scripts\\python -X utf8 tools\\fetch_jquants_details.py --recent-days 30 --apply
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from lib.runtime_paths import runtime_path

# Windows cp932 対策
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "encoding") and _s.encoding and \
            _s.encoding.lower() not in ("utf-8", "utf8"):
        import io as _io
        if _s is sys.stdout:
            sys.stdout = _io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace")
        else:
            sys.stderr = _io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace")

_DEFAULT_DB  = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR     = os.path.join(_PROJECT_ROOT, "logs")
# エンドポイントバージョン: probe_jquants_endpoints.py で200になったURLに合わせる
# v2 → 403の場合は v1 に変更
_BASE_URL    = "https://api.jquants.com/v1"
_DETAIL_ENDPOINT = "/fins/statements"  # 403なら /fins/details / /fins/fs_details を試す
JST          = timezone(timedelta(hours=9))

# 6861 キーエンス FY 検証ターゲット
_KEYENCE_LOCAL_CODE = "68610"
_KEYENCE_PERIOD     = "2026-03-20"
_KEYENCE_QUARTER    = "FY"

logger = logging.getLogger("fetch_jquants_details")


# ============================================================
# 認証
# ============================================================
def _get_auth_headers() -> dict:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from jquants_auth import get_auth_headers
        return get_auth_headers()
    except Exception:
        pass
    env = Path(_PROJECT_ROOT) / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JQUANTS_API_KEY が未設定です。.env に追加してください。")
    return {"x-api-key": key}


# ============================================================
# API GET (429/5xx リトライ)
# ============================================================
def _api_get(session: requests.Session, endpoint: str,
             params: dict, headers: dict) -> requests.Response:
    url = f"{_BASE_URL}{endpoint}"
    for attempt in range(6):
        try:
            r = session.get(url, params=params, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            logger.warning(f"[API] {endpoint} attempt={attempt+1} error={e}")
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 429:
            wait = 60 * (attempt + 1)
            logger.warning(f"[API] 429 rate-limit, wait {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 401:
            raise RuntimeError(f"認証エラー(401): {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        return r
    raise RuntimeError(f"{endpoint}: リトライ上限超過")


def _to_local_code(ticker: str) -> str:
    t = ticker.strip()
    return (t + "0") if len(t) == 4 and t.isdigit() else t


# ============================================================
# /fins/fs_details 取得
# ============================================================
def fetch_fs_details(
    session: requests.Session,
    headers: dict,
    *,
    local_code: str | None = None,
    date: str | None = None,
    disclosed_date: str | None = None,
) -> list[dict]:
    """
    詳細財務API (_DETAIL_ENDPOINT) を呼ぶ。
    probe_jquants_endpoints.py で HTTP200 になったエンドポイントを _DETAIL_ENDPOINT に設定。

    パラメータ選択:
      - local_code 指定 → code=local_code
      - date 指定 → date=YYYY-MM-DD (当日開示分全銘柄)
      - disclosed_date 指定 → disclosed_date=YYYY-MM-DD
    pagination_key によるページングを追う。
    """
    params: dict = {}
    if local_code:
        params["code"] = local_code
    if date:
        params["date"] = date
    if disclosed_date:
        params["disclosed_date"] = disclosed_date

    all_items: list[dict] = []
    while True:
        resp = _api_get(session, _DETAIL_ENDPOINT, params, headers)

        if resp.status_code in (403, 404):
            logger.warning(
                f"[API] {_DETAIL_ENDPOINT} HTTP {resp.status_code}: {resp.text[:300]}\n"
                f"  → probe_jquants_endpoints.py を実行して正しいエンドポイントを確認してください"
            )
            return all_items
        if resp.status_code != 200:
            logger.warning(f"[API] {_DETAIL_ENDPOINT} HTTP {resp.status_code}: {resp.text[:300]}")
            return all_items

        data = resp.json()
        # レスポンスキー名: API版によって異なる
        items = (
            data.get("statements")
            or data.get("fs_details")
            or data.get("details")
            or data.get("data")
            or []
        )
        all_items.extend(items)

        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break
        params["pagination_key"] = pagination_key
        time.sleep(0.2)

    return all_items


# ============================================================
# レスポンス解析: 項目名一覧 + gross_profit 特定
# ============================================================
# J-Quants fs_details で gross_profit に相当する可能性があるキー
_GP_CANDIDATE_KEYS = [
    "GrossProfit",
    "gross_profit",
    "売上総利益",
    "GrossProfitLoss",
    "GrossProfitFromOperations",
    "GrossOperatingProfit",
    "RevenueFromOperationsLessOperatingExpenses",
]

# 除外する売上・費用系 (誤判定防止)
_EXCLUDE_KEYS = {"NetSales", "Sales", "Revenue", "OperatingIncome",
                 "OperatingProfit", "NetIncome", "NetProfit"}


def analyze_response(items: list[dict]) -> dict:
    """レスポンス項目を分析してgross_profit候補を返す。"""
    if not items:
        return {"all_keys": [], "gp_candidates": {}, "sample": None}

    # 全キーを集計
    key_counts: dict[str, int] = {}
    for item in items:
        for k in item.keys():
            key_counts[k] = key_counts.get(k, 0) + 1

    # gross_profit 候補
    gp_candidates: dict[str, any] = {}
    sample = items[0]
    for k, v in sample.items():
        k_lower = k.lower()
        if any(gp.lower() in k_lower for gp in _GP_CANDIDATE_KEYS):
            gp_candidates[k] = v

    return {
        "all_keys": sorted(key_counts.keys()),
        "key_counts": key_counts,
        "gp_candidates": gp_candidates,
        "sample": sample,
    }


def extract_gross_profit(item: dict) -> int | None:
    """1行から gross_profit 値を抽出する。"""
    for k in _GP_CANDIDATE_KEYS:
        v = item.get(k)
        if v is not None and v != "":
            try:
                return int(float(v))
            except (ValueError, TypeError):
                pass
    return None


# ============================================================
# DB セットアップ
# ============================================================
_CREATE_DETAILS_TABLE = """
CREATE TABLE IF NOT EXISTS jquants_financial_details_raw (
    local_code        TEXT    NOT NULL,
    disclosed_date    TEXT    NOT NULL,
    fiscal_year_end   TEXT,
    type_of_period    TEXT,
    gross_profit      INTEGER,
    raw_json          TEXT    NOT NULL,
    fetched_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(local_code, disclosed_date, fiscal_year_end, type_of_period)
);
"""

_UPSERT_DETAILS_SQL = """
INSERT INTO jquants_financial_details_raw
    (local_code, disclosed_date, fiscal_year_end, type_of_period,
     gross_profit, raw_json, fetched_at)
VALUES
    (:local_code, :disclosed_date, :fiscal_year_end, :type_of_period,
     :gross_profit, :raw_json, :fetched_at)
ON CONFLICT(local_code, disclosed_date, fiscal_year_end, type_of_period)
DO UPDATE SET
    gross_profit = COALESCE(excluded.gross_profit, gross_profit),
    raw_json     = excluded.raw_json,
    fetched_at   = excluded.fetched_at
"""


def _item_to_db_row(item: dict) -> dict | None:
    """API 1アイテム → DB行。キー名は実際のレスポンスに合わせて調整。"""
    local_code = (
        item.get("Code") or item.get("LocalCode") or item.get("code") or ""
    ).strip()
    if not local_code:
        return None

    disclosed_date = (
        item.get("DisclosedDate") or item.get("DiscDate") or
        item.get("disclosed_date") or ""
    ).strip()
    fiscal_year_end = (
        item.get("CurrentFiscalYearEndDate") or item.get("CurFYEn") or
        item.get("fiscal_year_end") or ""
    ).strip()
    type_of_period = (
        item.get("TypeOfCurrentPeriod") or item.get("CurPerType") or
        item.get("type_of_period") or ""
    ).strip()

    gross_profit = extract_gross_profit(item)

    return {
        "local_code":      local_code,
        "disclosed_date":  disclosed_date,
        "fiscal_year_end": fiscal_year_end,
        "type_of_period":  type_of_period,
        "gross_profit":    gross_profit,
        "raw_json":        json.dumps(item, ensure_ascii=False),
        "fetched_at":      datetime.now(JST).isoformat(),
    }


def save_to_db(db_path: str, rows: list[dict]) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_DETAILS_TABLE)
    count = 0
    for row in rows:
        if not row:
            continue
        try:
            conn.execute(_UPSERT_DETAILS_SQL, row)
            count += 1
        except Exception as e:
            logger.warning(f"[DB] upsert error: {row.get('local_code')} {e}")
    conn.commit()
    conn.close()
    return count


# ============================================================
# canonical_financials への書き込み
# ============================================================
def push_to_canonical(
    ticker: str, period: str, quarter: str, gross_profit: int, config: dict
) -> dict:
    """gross_profit を canonical_financials に書き込む。"""
    from lib.pipeline.canonical_writer import expand_financials_rows
    from lib.pipeline.db import supabase_upsert

    rows, skipped = expand_financials_rows(
        ticker=ticker,
        period=period,
        quarter=quarter,
        metrics_dict={"gross_profit": gross_profit},
        source="jquants_detail",   # summary_xbrl(1) より低い priority=2 扱い
        filing_id=f"jqdetail_{ticker}_{quarter}",
        unit="JPY",
    )
    if not rows:
        return {"ok": False, "written": 0, "skipped": skipped}

    result = supabase_upsert(
        "canonical_financials", rows, on_conflict="source_row_key", config=config
    )
    return {
        "ok": result.get("ok"),
        "written": len(rows) if result.get("ok") else 0,
        "skipped": skipped,
        "result": result,
    }


# ============================================================
# メイン
# ============================================================
def run(opts) -> None:
    auth = _get_auth_headers()
    session = requests.Session()

    target_local_code = _to_local_code(opts.ticker) if opts.ticker else None
    logger.info(f"[RUN] ticker={opts.ticker} local_code={target_local_code} "
                f"mode={'DRY-RUN' if opts.dry_run else 'APPLY'}")

    # --- 取得 ---
    all_items: list[dict] = []

    if target_local_code:
        # 単一銘柄: code で直接取得
        logger.info(f"[FETCH] /fins/fs_details code={target_local_code}")
        items = fetch_fs_details(session, auth, local_code=target_local_code)
        logger.info(f"[FETCH] {len(items)} rows")
        all_items.extend(items)
    else:
        # 日付範囲で取得
        today = datetime.now(JST)
        days = opts.recent_days or 30
        dt_from = today - timedelta(days=days)
        current = dt_from
        while current <= today:
            d = current.strftime("%Y-%m-%d")
            logger.info(f"[FETCH] /fins/fs_details date={d}")
            items = fetch_fs_details(session, auth, date=d)
            logger.info(f"  → {len(items)} rows")
            all_items.extend(items)
            current += timedelta(days=1)
            time.sleep(0.3)

    session.close()

    if not all_items:
        logger.warning("[RUN] 取得件数 0。APIレスポンスを確認してください。")
        print("\n❌ データが取得できませんでした。")
        print("   - JQUANTS_API_KEY が Premium プランか確認してください")
        print("   - エンドポイント /fins/fs_details が利用可能か確認してください")
        return

    # --- 項目分析 ---
    analysis = analyze_response(all_items)
    print("\n" + "=" * 60)
    print("  /fins/fs_details レスポンス項目分析")
    print("=" * 60)
    print(f"  取得件数: {len(all_items)}")
    print(f"\n  全キー ({len(analysis['all_keys'])}個):")
    for k in analysis['all_keys']:
        print(f"    {k}")
    print(f"\n  gross_profit 候補:")
    if analysis['gp_candidates']:
        for k, v in analysis['gp_candidates'].items():
            print(f"    ✅ {k} = {v}")
    else:
        print("    ❌ 該当なし")

    # 6861 FY 行を特定
    keyence_fy = [
        item for item in all_items
        if (item.get("Code") or item.get("LocalCode") or "").strip() == _KEYENCE_LOCAL_CODE
        and (item.get("TypeOfCurrentPeriod") or item.get("CurPerType") or "").strip() == "FY"
    ]
    print(f"\n  6861 FY 行: {len(keyence_fy)} 件")
    for item in keyence_fy[:3]:
        gp = extract_gross_profit(item)
        print(f"    gross_profit={gp}  "
              f"period={item.get('CurrentFiscalYearEndDate', item.get('CurFYEn'))}")
        if opts.verbose:
            print(f"    raw={json.dumps(item, ensure_ascii=False)[:400]}")

    if opts.dry_run:
        print("\n  ℹ️  DRY-RUN: DB・Supabase への書き込みはスキップ")
        print("     本番反映するには --apply を付けてください")
        return

    # --- DB 保存 ---
    db_rows = [_item_to_db_row(item) for item in all_items]
    db_rows = [r for r in db_rows if r]
    saved = save_to_db(opts.db, db_rows)
    logger.info(f"[DB] jquants_financial_details_raw: {saved} rows saved → {opts.db}")
    print(f"\n  ✅ DB保存: {saved} rows → {opts.db}")

    # --- canonical push ---
    if opts.push_canonical:
        if not keyence_fy:
            print("\n  ⚠️  6861 FY が取得できなかったため canonical 書き込みをスキップ")
            return
        item = keyence_fy[0]
        gp = extract_gross_profit(item)
        if gp is None:
            print("\n  ⚠️  gross_profit が取得できなかったため canonical 書き込みをスキップ")
            return

        from src.config import load_config
        config = load_config()
        result = push_to_canonical(
            ticker="6861",
            period=_KEYENCE_PERIOD,
            quarter=_KEYENCE_QUARTER,
            gross_profit=gp,
            config=config,
        )
        if result.get("ok"):
            print(f"\n  ✅ canonical_financials に書き込み完了")
            print(f"     ticker=6861 period={_KEYENCE_PERIOD} quarter=FY "
                  f"gross_profit={gp:,} (JPY)")
            print("     → viewer の 6861 FY gross_profit が表示されるはずです")
        else:
            print(f"\n  ❌ canonical 書き込み失敗: {result.get('result', {})}")
    else:
        print("\n  ℹ️  canonical 書き込みは --push-canonical を指定してください")

    print()


def main() -> None:
    os.makedirs(str(runtime_path(_LOG_DIR)), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(str(runtime_path(_LOG_DIR)), f"fetch_jquants_details_{ts}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    parser = argparse.ArgumentParser(
        description="J-Quants /fins/fs_details → gross_profit 取得・保存"
    )
    parser.add_argument("--ticker", type=str, default="6861",
                        help="対象銘柄コード (4桁, default: 6861)")
    parser.add_argument("--recent-days", type=int, default=None,
                        help="日付範囲取得: 直近N日 (ticker未指定時に使用)")
    parser.add_argument("--db", type=str, default=str(runtime_path(_DEFAULT_DB)),
                        help=f"保存先 SQLite (default: data/jquants.db)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="DB・Supabase に書き込まない (default)")
    parser.add_argument("--apply", action="store_true",
                        help="DB・Supabase に書き込む")
    parser.add_argument("--push-canonical", action="store_true",
                        help="canonical_financials にも書き込む (--apply と併用)")
    parser.add_argument("--verbose", action="store_true")
    opts = parser.parse_args()

    if opts.apply:
        opts.dry_run = False

    logger.info("=" * 60)
    logger.info("  J-Quants /fins/fs_details 取得")
    logger.info("=" * 60)
    logger.info(f"  ticker        : {opts.ticker}")
    logger.info(f"  mode          : {'DRY-RUN' if opts.dry_run else 'APPLY'}")
    logger.info(f"  push_canonical: {opts.push_canonical}")
    logger.info(f"  db            : {opts.db}")
    logger.info(f"  log           : {log_file}")
    logger.info("=" * 60)

    try:
        run(opts)
    except RuntimeError as e:
        logger.error(f"FATAL: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"FATAL: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
