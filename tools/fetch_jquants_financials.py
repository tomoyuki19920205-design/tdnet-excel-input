#!/usr/bin/env python3
# ============================================================
# fetch_jquants_financials.py — J-Quants V2 /fins/summary
#                               → data/jquants.db 保存
# ============================================================
"""
J-Quants V2 API から財務データ（決算短信サマリー）を取得し、
data/jquants.db の jquants_financials_normalized テーブルに保存する。

使い方:
  # ドライラン（デフォルト: 直近30日）
  python -X utf8 tools/fetch_jquants_financials.py

  # 本番反映（直近30日）
  python -X utf8 tools/fetch_jquants_financials.py --apply

  # 期間指定
  python -X utf8 tools/fetch_jquants_financials.py --from-date 2026-04-01 --to-date 2026-05-02 --apply

  # 単一銘柄テスト（ドライラン）
  python -X utf8 tools/fetch_jquants_financials.py --ticker 1930 --from-date 2026-04-01 --to-date 2026-05-02

  # 単一銘柄テスト（本番反映）
  python -X utf8 tools/fetch_jquants_financials.py --ticker 1930 --from-date 2026-04-01 --to-date 2026-05-02 --apply

保存先:
  data/jquants.db の jquants_financials_normalized

単位:
  API から受け取った円単位のまま保存する（百万円への変換は sync_financials.py が行う）

認証:
  .env の JQUANTS_API_KEY を x-api-key ヘッダに設定（V2 方式）

エンドポイント:
  V2 /fins/summary  — 決算短信サマリー（日付指定で全銘柄）
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

# ============================================================
# パス設定
# ============================================================
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

sys.path.insert(0, str(Path(__file__).parent))

from src.jquants.financial_details import normalize_actual_consolidated_pbt

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
if sys.stderr and hasattr(sys.stderr, "encoding"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "jquants.db")
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
_DEFAULT_RECENT_DAYS = 30

JST = timezone(timedelta(hours=9))

# J-Quants V2 API
_BASE_URL = "https://api.jquants.com/v2"
_SLEEP_BETWEEN_DATES = 0.3
_SLEEP_ON_429 = 60
_MAX_RETRIES = 5

# gross_profit 補完用: /fins/details エンドポイント
# probe_jquants_endpoints.py で HTTP200 になったパスに合わせて変更する
_DETAILS_ENDPOINT = "/fins/details"  # 403なら /fins/statements / /v1/fins/statements を試す

# /fins/details レスポンス内で gross_profit に相当するキー候補
# 実際のレスポンス: "Gross profit (loss)" などスペース・括弧ありキーに対応
_GP_DETAIL_KEYS = [
    # 正式名 (J-Quants /v2/fins/details 実迷)
    "Gross profit (loss)",
    "Gross profit",
    "Gross profit-loss",
    # CamelCase 系
    "GrossProfit",
    "GrossProfitLoss",
    "GrossProfitFromOperations",
    "gross_profit",
    # 日本語
    "売上総利益",
]

# 正規化比較用: lowercase + スペース・括弧・ハイフン除去
_GP_NORMALIZED = {
    # 正規化後の文字列 : 元のキー名 (ログ用)
    "grossprofit":              "GrossProfit",
    "grossprofitloss":          "GrossProfitLoss",
    "grossprofitfromoperations":"GrossProfitFromOperations",
    "gross_profit":             "gross_profit",
    "売上総利益":              "売上総利益",
}

logger = logging.getLogger("fetch_jquants_financials")


# ============================================================
# 認証（V2 x-api-key 方式 — 既存 jquants_auth.py と同じ）
# ============================================================
def _load_env() -> None:
    """  .env から環境変数を読み込む。"""
    env_path = Path(_PROJECT_ROOT) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _get_auth_headers() -> dict:
    """
    JQUANTS_API_KEY を .env から読み込み、x-api-key ヘッダを返す。
    既存の jquants_auth.py を優先して使用する。
    """
    try:
        from tools.jquants_auth import get_auth_headers
        return get_auth_headers()
    except (ImportError, RuntimeError):
        pass

    # フォールバック: 直接 .env を読む
    _load_env()
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "JQUANTS_API_KEY が未設定です。\n"
            ".env に JQUANTS_API_KEY=xxx を追加してください。"
        )
    logger.info(f"[AUTH] JQUANTS_API_KEY loaded ({api_key[:8]}...)")
    return {"x-api-key": api_key}


# ============================================================
# API クライアント（429/5xx リトライ付き）
# ============================================================
def _api_get(
    session: requests.Session,
    endpoint: str,
    params: dict,
    auth_headers: dict,
) -> requests.Response:
    """GET リクエスト（429/5xx リトライ付き）。"""
    url = f"{_BASE_URL}{endpoint}"
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, headers=auth_headers, timeout=30)
        except requests.exceptions.Timeout:
            logger.warning(f"[API] Timeout: {endpoint} attempt={attempt+1}")
            time.sleep(5 * (attempt + 1))
            continue
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[API] ConnectionError: {e} attempt={attempt+1}")
            time.sleep(10 * (attempt + 1))
            continue

        if resp.status_code == 429:
            wait = _SLEEP_ON_429 * (attempt + 1)
            logger.warning(f"[API] Rate limit 429! Waiting {wait}s (attempt {attempt+1})")
            time.sleep(wait)
            continue

        if resp.status_code == 401:
            raise RuntimeError(
                f"認証エラー (401): JQUANTS_API_KEY を確認してください。\n"
                f"response: {resp.text[:300]}"
            )

        if resp.status_code >= 500:
            wait = 5 * (attempt + 1)
            logger.warning(f"[API] HTTP {resp.status_code} attempt={attempt+1} wait={wait}s")
            time.sleep(wait)
            continue

        return resp

    raise RuntimeError(f"[API] {endpoint}: {_MAX_RETRIES}回リトライ後も失敗")


# ============================================================
# /v2/fins/summary 取得
# ============================================================
def fetch_statements_for_date(
    session: requests.Session,
    date_str: str,
    auth_headers: dict,
) -> list[dict]:
    """
    /v2/fins/summary?date={date_str} で1日分の財務データを取得。

    pagination_key によるページングを最後まで追う。
    Returns: API の summary リスト（生 dict）
    """
    all_items: list[dict] = []
    pagination_key: str | None = None

    while True:
        params: dict = {"date": date_str}
        if pagination_key:
            params["pagination_key"] = pagination_key

        resp = _api_get(session, "/fins/summary", params, auth_headers)

        if resp.status_code == 404:
            # 休日・祝日等でデータなし
            return all_items

        if resp.status_code != 200:
            logger.warning(
                f"[API] /fins/summary date={date_str} "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return all_items

        data = resp.json()

        # V2 /fins/summary のレスポンスキー名: "data"
        items = data.get("data", data.get("summary", []))
        all_items.extend(items)

        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break

        time.sleep(0.2)

    return all_items


# ============================================================
# ticker 正規化（4桁→5桁 local_code）
# ============================================================
def _to_local_code(ticker: str) -> str:
    """4桁 ticker → 5桁 local_code（末尾0補完）。"""
    t = ticker.strip()
    if len(t) == 4 and t.isdigit():
        return t + "0"
    if len(t) == 5 and t.isdigit():
        return t
    return t


# ============================================================
# DB セットアップ
# ============================================================
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jquants_financials_normalized (
    local_code                    TEXT NOT NULL,
    disclosed_date                TEXT NOT NULL,
    current_fiscal_year_end_date  TEXT NOT NULL,
    type_of_current_period        TEXT NOT NULL,
    type_of_document              TEXT NOT NULL,
    net_sales                     INTEGER,
    gross_profit                  INTEGER,
    operating_profit              INTEGER,
    profit_before_tax             INTEGER,
    raw_json                      TEXT,
    fetched_at                    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def _ensure_table(conn: sqlite3.Connection) -> None:
    """テーブルが存在しない場合のみ作成する。UNIQUE INDEX は既存テーブルにすでに存在する可能性があるためスキップ。"""
    conn.execute(_CREATE_TABLE_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jquants_financials_normalized)")}
    if "profit_before_tax" not in columns:
        conn.execute(
            "ALTER TABLE jquants_financials_normalized ADD COLUMN profit_before_tax INTEGER"
        )
    conn.commit()


# ============================================================
# 行変換: API dict → DB dict
# V2 /fins/summary のフィールド名を使用
# ============================================================
def _safe_int(val) -> int | None:
    """数値を int に変換。None/空/非数値は None。"""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _row_to_db(item: dict) -> dict | None:
    """
    API の summary 1行 → jquants_financials_normalized の1行。

    V2 /fins/summary の実際のフィールド名:
      Code           : 銘柄コード (5桁)
      DiscDate       : 開示日 (YYYY-MM-DD)
      CurFYEn        : 当期会計年度末 (YYYY-MM-DD)
      CurPerType     : 当期会計期間種別 (1Q/2Q/3Q/FY)
      DocType        : 書類種別
      Sales          : 連結売上高 (円単位)
      OP             : 連結営業利益 (円単位)
      NCSales        : 非連結売上高 (円単位)
      NCOP           : 非連結営業利益 (円単位)

    金額は円単位のまま保存（sync_financials.py が百万円に変換する）。
    """
    local_code = (item.get("Code") or "").strip()
    disclosed_date = (item.get("DiscDate") or "").strip()
    fiscal_year_end = (item.get("CurFYEn") or "").strip()
    period = (item.get("CurPerType") or "").strip()
    doc_type = (item.get("DocType") or "").strip()

    # 必須フィールドチェック
    if not local_code or not disclosed_date or not fiscal_year_end or not period:
        return None

    # 連結優先、なければ非連結を使用
    net_sales = _safe_int(item.get("Sales") or item.get("NCSales"))
    # OP and NCOP have different consolidation scopes. Never promote NCOP
    # into the consolidated operating_profit field.
    operating_profit = _safe_int(item.get("OP"))
    # V2 /fins/summary に GrossProfit フィールドは存在しない。
    # ただし旧スクリプトが raw_json に "_gross_profit" を追記していた場合はそれを採用する。
    gross_profit = _safe_int(item.get("_gross_profit"))
    profit_before_tax = _safe_int(item.get("_profit_before_tax"))

    return {
        "local_code": local_code,
        "disclosed_date": disclosed_date,
        "current_fiscal_year_end_date": fiscal_year_end,
        "type_of_current_period": period,
        "type_of_document": doc_type,
        # 金額: 円単位のまま保存
        "net_sales": net_sales,
        "gross_profit": gross_profit,
        "operating_profit": operating_profit,
        "profit_before_tax": profit_before_tax,
        "raw_json": json.dumps(item, ensure_ascii=False),
        "fetched_at": datetime.now(JST).isoformat(),
    }


# ============================================================
# DB への UPSERT
# ============================================================
# INSERT + 条件付き UPDATE 方式:
#   - 新規行は INSERT
#   - 既存行は UPDATE。gross_profit / net_sales は新規値が NULL の場合、
#     既存値を保持する。
#   - operating_profit は latest-effective correction の明示的な NULL を
#     保持するため、OP の値（NULL を含む）で置換する。
#   - raw_json と fetched_at は常に最新値で上書きする。
_INSERT_SQL = """
INSERT OR IGNORE INTO jquants_financials_normalized
    (local_code, disclosed_date, current_fiscal_year_end_date,
     type_of_current_period, type_of_document,
     net_sales, gross_profit, operating_profit, profit_before_tax,
     raw_json, fetched_at)
VALUES
    (:local_code, :disclosed_date, :current_fiscal_year_end_date,
     :type_of_current_period, :type_of_document,
     :net_sales, :gross_profit, :operating_profit, :profit_before_tax,
     :raw_json, :fetched_at)
"""

_UPDATE_SQL = """
UPDATE jquants_financials_normalized
SET
    type_of_document  = :type_of_document,
    net_sales         = COALESCE(:net_sales,        net_sales),
    gross_profit      = COALESCE(:gross_profit,     gross_profit),
    operating_profit  = :operating_profit,
    profit_before_tax = COALESCE(:profit_before_tax, profit_before_tax),
    raw_json          = :raw_json,
    fetched_at        = :fetched_at
WHERE
    local_code                   = :local_code
    AND disclosed_date           = :disclosed_date
    AND current_fiscal_year_end_date = :current_fiscal_year_end_date
    AND type_of_current_period   = :type_of_current_period
    AND type_of_document         = :type_of_document
"""


def upsert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """rows を jquants_financials_normalized に upsert。戻り値は成功件数。

    既存行の gross_profit / net_sales は新規値が NULL の場合に既存値を保持する。
    operating_profit は訂正開示の NULL を有効な状態として上書きする。
    """
    _ensure_table(conn)
    count = 0
    for row in rows:
        row = {**row, "profit_before_tax": row.get("profit_before_tax")}
        try:
            # 1) 新規行なら INSERT（既存行は無視）
            conn.execute(_INSERT_SQL, row)
            # 2) 既存行（INSERT が無視された場合も含む）を UPDATE
            #    sales / gross_profit は COALESCE、OP は NULL を含めて置換
            conn.execute(_UPDATE_SQL, row)
            count += 1
        except Exception as e:
            logger.error(
                f"[UPSERT] error: local_code={row.get('local_code')} "
                f"date={row.get('disclosed_date')} "
                f"period={row.get('type_of_current_period')}: {e}"
            )
    conn.commit()
    return count


# ============================================================
# /fins/details から gross_profit を補完 (--enable-details-gp)
# ============================================================
def _normalize_key(k: str) -> str:
    """J-Quants キー名を正規化: lowercase + 空白・括弧・ハイフンを除去。"""
    import re
    return re.sub(r"[\s\(\)\-_]", "", k).lower()


def _extract_gp_from_detail_item(item: dict) -> int | None:
    """詳細財務API 1行から gross_profit を抽出する。

    Step1: _GP_DETAIL_KEYS で直接マッチ
    Step2: 正規化比較（スペース・括弧・ハイフン差異を吸収）
    両ステップともトップレベル・FS/PLサブオブジェクトの両方を探索。
    """

    def _try_extract(d: dict) -> int | None:
        # Step1: 直接マッチ
        for k in _GP_DETAIL_KEYS:
            v = d.get(k)
            if v is not None and v != "":
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    pass
        # Step2: 正規化比較（キー名にスペース・括弧ありのバリエーション対応）
        _GP_NORM_TARGETS = {
            "grossprofit", "grossprofitloss", "grossprofitfromoperations",
            "gross_profit",
        }
        for k, v in d.items():
            if _normalize_key(k) in _GP_NORM_TARGETS:
                if v is not None and v != "":
                    try:
                        val = int(float(v))
                        logger.info(
                            f"[DETAILS] 正規化マッチ: '{k}' (normalized='{_normalize_key(k)}') = {val}"
                        )
                        return val
                    except (ValueError, TypeError):
                        pass
        return None

    # トップレベル
    result = _try_extract(item)
    if result is not None:
        return result

    # FS / PL サブオブジェクト
    for sub_key in ("FS", "PL", "fins", "financial_statements"):
        sub = item.get(sub_key)
        if isinstance(sub, dict):
            result = _try_extract(sub)
            if result is not None:
                return result
    return None


def _fetch_details_gross_profit(
    session: requests.Session,
    auth_headers: dict,
    local_code: str,
    disclosed_date: str,
) -> int | None:
    """
    /fins/details から 1銘柄・1開示日の gross_profit を取得する。

    失敗・未取得は None を返す（best-effort）。
    """
    params = {"code": local_code, "date": disclosed_date}
    try:
        resp = _api_get(session, _DETAILS_ENDPOINT, params, auth_headers)
    except Exception as e:
        logger.warning(f"[DETAILS] API error: local_code={local_code} date={disclosed_date}: {e}")
        return None

    if resp.status_code in (403, 404):
        logger.warning(
            f"[DETAILS] HTTP {resp.status_code} — エンドポイント '{_DETAILS_ENDPOINT}' が存在しません。\n"
            f"  probe_jquants_endpoints.py を実行して正しいエンドポイントを確認し、"
            f"_DETAILS_ENDPOINT を修正してください。"
        )
        return None
    if resp.status_code != 200:
        logger.warning(f"[DETAILS] HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json()
    items = (
        data.get("details")
        or data.get("statements")
        or data.get("fs_details")
        or data.get("data")
        or []
    )

    # ---- FS キーダンプ (gross_profit 候補特定用) ----
    # 売上・粗利・営利に直接関係するキーのみ表示。資本項目・債券費用等は除外。
    _DUMP_SHOW_KEYS = {
        "gross profit", "gross profit (loss)", "gross profit (ifrs)",
        "operating gross profit", "operating gross profit (loss)",
        "net sales", "revenue", "operating revenue",
        "operating profit", "operating profit (loss)",
        "売上総利益", "売上高", "営業利益",
    }

    def _should_dump(k: str) -> bool:
        return k.lower().strip() in _DUMP_SHOW_KEYS

    if items:
        sample = items[0]
        # FS サブオブジェクト内を絞り込み表示
        for sub_key in ("FS", "PL", "fins", "financial_statements", "details"):
            sub = sample.get(sub_key)
            if isinstance(sub, dict):
                matched = [(k, sub[k]) for k in sorted(sub.keys()) if _should_dump(k)]
                if matched:
                    logger.info(
                        f"[DETAILS][DUMP] local_code={local_code} date={disclosed_date} "
                        f"{sub_key}({len(sub)}キー中 関連{len(matched)}件):"
                    )
                    for k, v in matched:
                        logger.info(f"  ★ {k} = {v}")
                else:
                    logger.info(
                        f"[DETAILS][DUMP] local_code={local_code} date={disclosed_date} "
                        f"{sub_key}({len(sub)}キー) 売上・粗利・営利キーなし"
                    )
                break
        else:
            # FS サブキーがない場合はトップレベルから直接探す
            matched = [(k, sample[k]) for k in sorted(sample.keys()) if _should_dump(k)]
            if matched:
                logger.info(
                    f"[DETAILS][DUMP] local_code={local_code} date={disclosed_date} "
                    f"top-level 関連{len(matched)}件:"
                )
                for k, v in matched:
                    logger.info(f"  ★ {k} = {v}")
            else:
                logger.info(
                    f"[DETAILS][DUMP] local_code={local_code} date={disclosed_date} "
                    f"FS/PLサブキーなし・トップレベルにも売上・粗利・営利キーなし"
                )
    else:
        logger.info(
            f"[DETAILS][DUMP] データ0件: local_code={local_code} date={disclosed_date} "
            f"resp_keys={sorted(data.keys())}"
        )
    # ---- ダンプ終わり ----

    for item in items:
        gp = _extract_gp_from_detail_item(item)
        if gp is not None:
            return gp
    return None


def _fetch_details_actual_metrics(
    session: requests.Session,
    auth_headers: dict,
    summary_item: dict,
) -> dict[str, int | None]:
    """Fetch the matching details disclosure and extract strict actual metrics."""
    local_code = str(summary_item.get("Code") or "")
    disclosed_date = str(summary_item.get("DiscDate") or "")
    response = _api_get(
        session,
        _DETAILS_ENDPOINT,
        {"code": local_code, "date": disclosed_date},
        auth_headers,
    )
    if response.status_code != 200:
        logger.warning(
            f"[DETAILS] HTTP {response.status_code}: "
            f"local_code={local_code} date={disclosed_date}"
        )
        return {"gross_profit": None, "profit_before_tax": None}
    items = response.json().get("data") or []
    disclosure_number = str(summary_item.get("DiscNo") or "")
    matching = [
        item for item in items
        if item.get("Code") == local_code
        and str(item.get("DiscNo") or "") == disclosure_number
    ]
    if len(matching) != 1:
        logger.warning(
            f"[DETAILS] exact disclosure match count={len(matching)} "
            f"code={local_code} disc_no={disclosure_number}"
        )
        return {"gross_profit": None, "profit_before_tax": None}
    item = matching[0]
    pbt_record = normalize_actual_consolidated_pbt(
        item, summary_item, expected_code=local_code
    )
    return {
        "gross_profit": _extract_gp_from_detail_item(item),
        "profit_before_tax": (
            pbt_record.raw_value_jpy if pbt_record is not None else None
        ),
    }


def _supplement_gross_profit_from_details(
    conn: sqlite3.Connection,
    session: requests.Session,
    auth_headers: dict,
    target_local_code: str | None,
    force: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """
    jquants_financials_normalized で gross_profit IS NULL の行に対して
    /fins/details から補完する。

    Args:
        target_local_code: None なら全行が対象（推奨しない）。通常は 1銘柄を指定。
        force: True なら既存 gross_profit も上書きする。
        date_from: 対象 disclosed_date の下限 (YYYY-MM-DD)。指定で過去履歴を除外。
        date_to:   対象 disclosed_date の上限 (YYYY-MM-DD)。

    Returns:
        {"checked": int, "supplemented": int, "skipped": int, "errors": int,
         "details_api_calls": int, "details_cache_hits": int}
    """
    stats = {
        "checked": 0,
        "supplemented": 0,
        "skipped": 0,
        "errors": 0,
        "details_api_calls": 0,
        "details_cache_hits": 0,
    }

    # Each disclosure number is matched exactly; no cross-document fallback.
    details_cache: dict[str, dict[str, int | None]] = {}

    where_clause = "(gross_profit IS NULL OR profit_before_tax IS NULL)" if not force else "1=1"
    if target_local_code:
        where_clause += f" AND local_code = '{target_local_code}'"
    if date_from:
        where_clause += f" AND disclosed_date >= '{date_from}'"
    if date_to:
        where_clause += f" AND disclosed_date <= '{date_to}'"

    rows = conn.execute(
        f"""
        SELECT local_code, disclosed_date,
               current_fiscal_year_end_date, type_of_current_period,
               gross_profit, profit_before_tax, raw_json
        FROM jquants_financials_normalized
        WHERE {where_clause}
        ORDER BY disclosed_date DESC
        """
    ).fetchall()

    logger.info(
        f"[DETAILS] gross_profit 補完対象: {len(rows)} 行 "
        f"(local_code={target_local_code or 'all'} "
        f"date={date_from or '*'}~{date_to or '*'} "
        f"force={force})"
    )

    for row in rows:
        stats["checked"] += 1
        local_code   = row[0]
        disc_date    = row[1]
        fy_end       = row[2]
        period_type  = row[3]
        existing_gp  = row[4]
        existing_pbt = row[5]
        summary_item = json.loads(row[6] or "{}")

        # force=False かつ既存値あり → スキップ
        if not force and existing_gp is not None and existing_pbt is not None:
            stats["skipped"] += 1
            continue

        cache_key = str(summary_item.get("DiscNo") or "")
        if cache_key in details_cache:
            metrics = details_cache[cache_key]
            stats["details_cache_hits"] += 1
        else:
            metrics = _fetch_details_actual_metrics(
                session, auth_headers, summary_item
            )
            details_cache[cache_key] = metrics
            stats["details_api_calls"] += 1
            time.sleep(0.3)

        gp = metrics["gross_profit"]
        pbt = metrics["profit_before_tax"]

        if gp is None and pbt is None:
            logger.debug(
                f"[DETAILS] 取得不可: local_code={local_code} date={disc_date} period={period_type}"
            )
            stats["skipped"] += 1
            continue

        try:
            conn.execute(
                """
                UPDATE jquants_financials_normalized
                SET gross_profit = COALESCE(?, gross_profit),
                    profit_before_tax = COALESCE(?, profit_before_tax)
                WHERE local_code = ?
                  AND disclosed_date = ?
                  AND current_fiscal_year_end_date = ?
                  AND type_of_current_period = ?
                """,
                (gp, pbt, local_code, disc_date, fy_end, period_type),
            )
            conn.commit()
            logger.info(
                f"[DETAILS] ✅ gross_profit 補完: local_code={local_code} "
                f"date={disc_date} period={fy_end}/{period_type} "
                f"gp={gp} pbt={pbt} (force={force})"
            )
            stats["supplemented"] += 1
        except Exception as e:
            logger.warning(f"[DETAILS] UPDATE error: {e}")
            stats["errors"] += 1

    logger.info(
        f"[DETAILS] 補完完了: checked={stats['checked']} "
        f"supplemented={stats['supplemented']} "
        f"skipped={stats['skipped']} "
        f"errors={stats['errors']} "
        f"api_calls={stats['details_api_calls']} "
        f"cache_hits={stats['details_cache_hits']}"
    )
    return stats


# ============================================================
# メイン処理
# ============================================================
def fetch_and_save(
    *,
    from_date: str,
    to_date: str,
    ticker: str | None = None,
    dry_run: bool = True,
    db_path: str = _DEFAULT_DB,
    enable_details_gp: bool = False,
    force_details_gp: bool = False,
) -> dict:
    """
    J-Quants V2 /fins/summary から財務データを取得し jquants.db に保存する。

    Args:
        from_date: 取得開始日 (YYYY-MM-DD)
        to_date:   取得終了日 (YYYY-MM-DD, 含む)
        ticker:    4桁銘柄コード。None なら全銘柄
        dry_run:   True の場合は DB 書き込みをスキップ
        db_path:   保存先 SQLite パス
    """
    stats = {
        "from_date": from_date,
        "to_date": to_date,
        "target_ticker": ticker,
        "total_fetched": 0,
        "target_rows": 0,
        "upserted": 0,
        "fy_rows": 0,
        "errors": 0,
        "dry_run": dry_run,
        "details_gp_supplemented": 0,
    }

    auth_headers = _get_auth_headers()

    target_local_code: str | None = None
    if ticker:
        target_local_code = _to_local_code(ticker)
        logger.info(f"[FETCH] ticker={ticker} → local_code={target_local_code}")

    try:
        dt_from = datetime.strptime(from_date, "%Y-%m-%d")
        dt_to = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"日付フォーマットエラー: {e}") from e

    date_list: list[str] = []
    current = dt_from
    while current <= dt_to:
        date_list.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    logger.info(
        f"[FETCH] range={from_date} ~ {to_date} "
        f"({len(date_list)} days) "
        f"ticker={ticker or 'all'} dry_run={dry_run}"
    )

    conn = sqlite3.connect(db_path) if not dry_run else None
    if conn:
        _ensure_table(conn)

    all_db_rows: list[dict] = []

    session = requests.Session()
    try:
        for i, date_str in enumerate(date_list):
            try:
                items = fetch_statements_for_date(session, date_str, auth_headers)
            except Exception as e:
                logger.error(f"[FETCH] {date_str}: error={e}")
                stats["errors"] += 1
                continue

            stats["total_fetched"] += len(items)

            if target_local_code:
                items = [
                    item for item in items
                    if (item.get("Code") or "").strip() == target_local_code
                ]

            stats["target_rows"] += len(items)

            fy_items = [
                item for item in items
                if (item.get("CurPerType") or "").strip() == "FY"
            ]
            stats["fy_rows"] += len(fy_items)

            if items:
                logger.info(
                    f"[FETCH] {date_str}: fetched={len(items)} "
                    f"FY={len(fy_items)}"
                    + (f" (for {target_local_code})" if target_local_code else "")
                )

            for item in items:
                row = _row_to_db(item)
                if row:
                    all_db_rows.append(row)
                else:
                    stats["errors"] += 1

            if (i + 1) % 10 == 0 or i == len(date_list) - 1:
                logger.info(
                    f"[FETCH] progress {i+1}/{len(date_list)} days "
                    f"total_fetched={stats['total_fetched']} "
                    f"target_rows={stats['target_rows']} "
                    f"fy_rows={stats['fy_rows']}"
                )

            time.sleep(_SLEEP_BETWEEN_DATES)

    finally:
        session.close()

    if dry_run:
        logger.info(
            f"\n{'='*55}\n"
            f"  DRY-RUN: DB 書き込みをスキップ\n"
            f"  total_fetched : {stats['total_fetched']}\n"
            f"  target_rows   : {stats['target_rows']}\n"
            f"  FY rows       : {stats['fy_rows']}\n"
            f"  本番反映するには --apply を付けて再実行してください\n"
            f"{'='*55}"
        )
        fy_sample = [r for r in all_db_rows if r.get("type_of_current_period") == "FY"]
        for r in fy_sample[:5]:
            logger.info(
                f"  [DRY] FY: local_code={r['local_code']} "
                f"period={r['current_fiscal_year_end_date']} "
                f"net_sales={r['net_sales']} "
                f"operating_profit={r['operating_profit']}"
            )
    else:
        if conn and all_db_rows:
            upserted = upsert_rows(conn, all_db_rows)
            stats["upserted"] = upserted
            logger.info(
                f"[SAVE] upserted={upserted} / target_rows={stats['target_rows']}"
            )
            # FY 行のログ
            fy_db_rows = [r for r in all_db_rows if r.get("type_of_current_period") == "FY"]
            for r in fy_db_rows[:10]:
                logger.info(
                    f"  [SAVED FY] local_code={r['local_code']} "
                    f"period={r['current_fiscal_year_end_date']} "
                    f"net_sales={r['net_sales']} "
                    f"operating_profit={r['operating_profit']}"
                )
        elif not all_db_rows:
            logger.info("[SAVE] 0 rows to save")

    # ---- /fins/details から gross_profit 補完 ----
    if not dry_run and enable_details_gp and conn:
        target_lc = _to_local_code(ticker) if ticker else None
        if not target_lc:
            logger.warning(
                "[DETAILS] --enable-details-gp は --ticker 指定時のみ推奨です。"
                "全銘柄補完は時間がかかります。"
            )
        logger.info(
            f"[DETAILS] gross_profit 補完開始: local_code={target_lc} "
            f"force={force_details_gp}"
        )
        conn2 = sqlite3.connect(db_path)
        try:
            d_stats = _supplement_gross_profit_from_details(
                conn2, session, auth_headers=_get_auth_headers(),
                target_local_code=target_lc,
                force=force_details_gp,
                date_from=from_date,
                date_to=to_date,
            )
            stats["details_gp_supplemented"] = d_stats["supplemented"]
            logger.info(
                f"[DETAILS] 補完完了: checked={d_stats['checked']} "
                f"supplemented={d_stats['supplemented']} "
                f"skipped={d_stats['skipped']} errors={d_stats['errors']}"
            )
        finally:
            conn2.close()
    elif dry_run and enable_details_gp:
        logger.info(
            "[DETAILS] DRY-RUN: gross_profit 補完はスキップ (--apply と組み合わせてください)"
        )

    if conn:
        conn.close()

    return stats


# ============================================================
# CLI
# ============================================================
def main() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(_LOG_DIR, f"fetch_jquants_fin_{ts}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    parser = argparse.ArgumentParser(
        description="J-Quants V2 /fins/summary → jquants.db 保存",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
━━━ 使用例 ━━━

  # ドライラン（直近30日、全銘柄）
  python -X utf8 tools/fetch_jquants_financials.py

  # 本番反映（直近30日）
  python -X utf8 tools/fetch_jquants_financials.py --apply

  # 期間 + 単一銘柄 ドライラン
  python -X utf8 tools/fetch_jquants_financials.py \\
    --from-date 2026-04-01 --to-date 2026-05-02 --ticker 1930

  # 期間 + 単一銘柄 本番反映
  python -X utf8 tools/fetch_jquants_financials.py \\
    --from-date 2026-04-01 --to-date 2026-05-02 --ticker 1930 --apply

  # 直近N日 本番反映
  python -X utf8 tools/fetch_jquants_financials.py --recent-days 30 --apply

  # raw_json["_gross_profit"] から gross_profit を復元（ドライラン）
  python -X utf8 tools/fetch_jquants_financials.py --repair-gross-profit-from-raw-json

  # raw_json["_gross_profit"] から gross_profit を復元（本番反映）
  python -X utf8 tools/fetch_jquants_financials.py --repair-gross-profit-from-raw-json --apply
""",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="DB に書き込む（省略時はドライラン）",
    )
    parser.add_argument("--from-date", type=str, default=None, help="取得開始日 YYYY-MM-DD")
    parser.add_argument("--to-date", type=str, default=None, help="取得終了日 YYYY-MM-DD（含む）")
    parser.add_argument("--recent-days", type=int, default=None,
                        help=f"直近N日（default: {_DEFAULT_RECENT_DAYS}）")
    parser.add_argument("--ticker", type=str, default=None, help="対象銘柄コード 4桁（例: 1930）")
    parser.add_argument("--db", type=str, default=_DEFAULT_DB,
                        help=f"保存先 SQLite パス（default: {_DEFAULT_DB}）")
    parser.add_argument(
        "--repair-gross-profit-from-raw-json", action="store_true",
        help="raw_json['_gross_profit'] が存在するが gross_profit IS NULL の行を修復する",
    )
    parser.add_argument(
        "--enable-details-gp", action="store_true",
        help="gross_profit が NULL の行を /fins/details から補完する (--apply と組み合わせる)",
    )
    parser.add_argument(
        "--force-details-gp", action="store_true",
        help="--enable-details-gp 時に既存 gross_profit も上書きする",
    )
    args = parser.parse_args()

    # ── repair モード ──────────────────────────────────────────
    if args.repair_gross_profit_from_raw_json:
        _repair_gross_profit(db_path=args.db, apply=args.apply)
        return

    is_dry_run = not args.apply

    today = datetime.now(JST).strftime("%Y-%m-%d")
    recent_days = args.recent_days if args.recent_days is not None else _DEFAULT_RECENT_DAYS

    if args.from_date and args.to_date:
        from_date = args.from_date
        to_date = args.to_date
    elif args.from_date:
        from_date = args.from_date
        to_date = today
    else:
        since = datetime.now(JST) - timedelta(days=recent_days)
        from_date = since.strftime("%Y-%m-%d")
        to_date = today

    logger.info("=" * 55)
    logger.info("  J-Quants Financials Fetch (V2 /fins/summary)")
    logger.info("=" * 55)
    logger.info(f"  mode      : {'DRY-RUN' if is_dry_run else 'APPLY (本番反映)'}")
    logger.info(f"  from_date : {from_date}")
    logger.info(f"  to_date   : {to_date}")
    logger.info(f"  ticker    : {args.ticker or '全銘柄'}")
    logger.info(f"  db        : {args.db}")
    logger.info(f"  log       : {log_file}")
    logger.info("=" * 55)

    logger.info(f"  enable_details_gp: {args.enable_details_gp}")
    logger.info(f"  force_details_gp : {args.force_details_gp}")

    try:
        stats = fetch_and_save(
            from_date=from_date,
            to_date=to_date,
            ticker=args.ticker,
            dry_run=is_dry_run,
            db_path=args.db,
            enable_details_gp=args.enable_details_gp,
            force_details_gp=args.force_details_gp,
        )
    except RuntimeError as e:
        logger.error(f"FATAL: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"FATAL: {e}", exc_info=True)
        sys.exit(1)

    print()
    print("=" * 55)
    print("  FETCH SUMMARY")
    print("=" * 55)
    print(f"  mode          : {'DRY-RUN' if is_dry_run else 'APPLY'}")
    print(f"  from_date     : {stats['from_date']}")
    print(f"  to_date       : {stats['to_date']}")
    print(f"  target_ticker : {stats['target_ticker'] or '全銘柄'}")
    print(f"  total_fetched : {stats['total_fetched']:,}  (API全銘柄件数)")
    print(f"  target_rows   : {stats['target_rows']:,}  (ticker絞り込み後)")
    print(f"  FY rows           : {stats['fy_rows']:,}  (TypeOfCurrentPeriod=FY)")
    print(f"  upserted          : {stats['upserted']:,}  (DB保存件数)")
    print(f"  details_gp_patched: {stats.get('details_gp_supplemented', 0):,}  (/fins/details 補完)")
    print(f"  errors            : {stats['errors']}")
    print("-" * 55)
    if is_dry_run:
        print("  ※ DRY-RUN のため DB は変更されていません")
        print("  ※ 本番反映するには --apply を付けて再実行してください")
    else:
        print("  ✅ DB への保存が完了しました")
        if stats["fy_rows"] > 0:
            print(f"  ✅ FY 行 {stats['fy_rows']} 件を保存しました")
        if stats.get("details_gp_supplemented", 0) > 0:
            print(f"  ✅ gross_profit 補完: {stats['details_gp_supplemented']} 行")
        print("  次のステップ:")
        print("    python -X utf8 tools/sync_financials.py --apply")
    print("=" * 55)
    print()


# ============================================================
# repair: raw_json["_gross_profit"] → gross_profit 復元
# ============================================================
def _repair_gross_profit(*, db_path: str = _DEFAULT_DB, apply: bool = False) -> None:
    """raw_json に '_gross_profit' があるが gross_profit IS NULL の行を修復する。

    - --apply なしはドライラン（件数のみ表示、DB変更なし）
    - --apply あり で実際に UPDATE する
    - 単位: raw_json['_gross_profit'] は円単位。DB も円単位保存なのでそのまま使う。
    """
    if not os.path.exists(db_path):
        print(f"[REPAIR] DB が見つかりません: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # gross_profit IS NULL かつ raw_json に _gross_profit がある行を探す
    candidates = conn.execute(
        """
        SELECT rowid, local_code, disclosed_date,
               current_fiscal_year_end_date, type_of_current_period,
               raw_json
        FROM jquants_financials_normalized
        WHERE gross_profit IS NULL
          AND raw_json IS NOT NULL
          AND raw_json LIKE '%_gross_profit%'
        """
    ).fetchall()

    print()
    print("=" * 55)
    print("  REPAIR: raw_json['_gross_profit'] → gross_profit")
    print("=" * 55)
    print(f"  mode    : {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  db      : {db_path}")
    print(f"  対象候補: {len(candidates)} 行")
    print()

    repaired = 0
    skipped = 0
    for row in candidates:
        try:
            rj = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue

        gp_raw = rj.get("_gross_profit")
        if gp_raw is None:
            skipped += 1
            continue

        gp_val = _safe_int(gp_raw)
        if gp_val is None:
            skipped += 1
            continue

        logger.info(
            f"  [REPAIR] local_code={row['local_code']} "
            f"disc={row['disclosed_date']} "
            f"period={row['current_fiscal_year_end_date']} "
            f"quarter={row['type_of_current_period']} "
            f"→ gross_profit={gp_val}  "
            f"({'UPDATE' if apply else 'DRY'})"
        )

        if apply:
            conn.execute(
                """
                UPDATE jquants_financials_normalized
                SET gross_profit = ?
                WHERE rowid = ?
                """,
                (gp_val, row["rowid"]),
            )
        repaired += 1

    if apply:
        conn.commit()

    conn.close()

    print(f"  repaired: {repaired} 行")
    print(f"  skipped : {skipped} 行（raw_json なし / _gross_profit なし / 変換不可）")
    print("-" * 55)
    if apply:
        print("  ✅ gross_profit を復元しました")
        print("  次のステップ:")
        print("    python -X utf8 tools/sync_financials.py --apply --full")
    else:
        print("  ※ DRY-RUN です。本番反映するには --apply を追加してください")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
