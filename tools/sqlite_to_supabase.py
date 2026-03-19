#!/usr/bin/env python3
# ============================================================
# sqlite_to_supabase.py — SQLite (decision_db.db) → Supabase push
# ============================================================
# 4フェーズ・バッチ処理 + SANITIZE監視 + quarantine隔離
#
# CLI:
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --limit 1000
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --resume
#   .\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --dry-run
# ============================================================
from __future__ import annotations

import argparse
import collections
import io
import json
import logging
import math
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker

logger = logging.getLogger("sqlite2supa")

JST = timezone(timedelta(hours=9))

# ============================================================
# 定数
# ============================================================
_METRIC_MAP = {
    "sales":            "NET_SALES",
    "gross_profit":     "GROSS_PROFIT",
    "operating_profit": "OP_INCOME",
}

_UNIT_MULTIPLIER = {
    "百万円":  1_000_000,
    "千円":    1_000,
    "円":      1,
}

# quarter 正規化マップ (TDnet → public.financials 互換)
_QUARTER_MAP = {
    "1Q": "1Q", "2Q": "2Q", "3Q": "3Q", "4Q": "FY",
    "1": "1Q", "2": "2Q", "3": "3Q", "4": "FY",
    "FY": "FY",
}

_MAX_SAFE_VALUE = 9_000_000_000_000_000  # 9e+15 (9千兆円)

_BATCH_SIZE = 500
_MASTER_BATCH = 1000
_RETRY_MAX = 5
_RETRY_BASE_SEC = 1.0
_CHECKPOINT_FILE = "data/push_checkpoint.json"
_QUARANTINE_DB = "data/quarantine.db"


# ============================================================
# .env 読み込み
# ============================================================
def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


# ============================================================
# Quarantine DB (ローカル SQLite)
# ============================================================
class _QuarantineDB:
    """SANITIZE された行を自動隔離する軽量ローカル DB"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS quarantine (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sqlite_row_id INTEGER,
                ticker        TEXT NOT NULL,
                fye           TEXT NOT NULL,
                quarter       TEXT NOT NULL,
                column_name   TEXT NOT NULL,
                original_value TEXT,
                sanitize_reason TEXT,
                source_doc_id TEXT,
                source_url    TEXT,
                zip_hash      TEXT,
                estimated_cause TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        self.conn.commit()

    def insert(self, *, sqlite_row_id: int, ticker: str, fye: str,
               quarter: str, column_name: str, original_value: str,
               sanitize_reason: str, source_doc_id: str | None,
               source_url: str | None, zip_hash: str | None,
               estimated_cause: str) -> None:
        self.conn.execute("""
            INSERT INTO quarantine
                (sqlite_row_id, ticker, fye, quarter, column_name,
                 original_value, sanitize_reason, source_doc_id,
                 source_url, zip_hash, estimated_cause)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (sqlite_row_id, ticker, fye, quarter, column_name,
              str(original_value), sanitize_reason, source_doc_id,
              source_url, zip_hash, estimated_cause))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# SANITIZE トラッカー
# ============================================================
class _SanitizeTracker:
    """SANITIZE 発生を記録し、サマリ出力する"""

    def __init__(self):
        self.records: list[dict] = []
        self.by_col: dict[str, int] = collections.Counter()
        self.by_ticker: dict[str, int] = collections.Counter()

    def add(self, *, ticker: str, fye: str, quarter: str,
            col: str, raw_val, reason: str,
            sqlite_row_id: int = 0,
            source_doc_id: str | None = None,
            source_url: str | None = None,
            zip_hash: str | None = None):
        self.records.append({
            "sqlite_row_id": sqlite_row_id,
            "ticker": ticker, "fye": fye, "quarter": quarter,
            "col": col, "raw_val": raw_val, "reason": reason,
            "source_doc_id": source_doc_id,
            "source_url": source_url,
            "zip_hash": zip_hash,
        })
        self.by_col[col] += 1
        self.by_ticker[ticker] += 1

    @property
    def count(self) -> int:
        return len(self.records)

    def print_summary(self):
        """Phase4 終了時に呼ぶ SANITIZE サマリ"""
        if not self.records:
            logger.info("[SANITIZE] 異常値なし (0件)")
            return

        logger.warning(
            f"[SANITIZE] === 異常値サマリ: {len(self.records)} 件 ==="
        )

        # 列別件数
        logger.warning("[SANITIZE] 列別件数:")
        for col, cnt in self.by_col.most_common():
            logger.warning(f"  {col}: {cnt} 件")

        # ticker 上位5
        logger.warning("[SANITIZE] ticker別 上位5:")
        for ticker, cnt in self.by_ticker.most_common(5):
            logger.warning(f"  {ticker}: {cnt} 件")

        # 全件リスト (最大50件)
        logger.warning("[SANITIZE] 該当行 詳細:")
        for i, r in enumerate(self.records[:50]):
            cause = _estimate_cause(r["raw_val"], r["reason"])
            logger.warning(
                f"  [{i+1}] ticker={r['ticker']} "
                f"fye={r['fye']} Q={r['quarter']} "
                f"col={r['col']} val={r['raw_val']} "
                f"reason={r['reason']} "
                f"cause={cause}"
            )
            if r.get("source_doc_id"):
                logger.warning(
                    f"       source_doc_id={r['source_doc_id']}"
                )
            if r.get("source_url"):
                logger.warning(
                    f"       source_url={r['source_url']}"
                )
        if len(self.records) > 50:
            logger.warning(
                f"  ... 他 {len(self.records)-50} 件 "
                f"(quarantine.db 参照)"
            )
        logger.warning(
            f"[SANITIZE] === サマリ終了 ({len(self.records)} 件) ==="
        )


# ============================================================
# 異常値 推定原因
# ============================================================
def _estimate_cause(raw_val, reason: str) -> str:
    """SANITIZE された値の推定原因を返す"""
    try:
        fv = float(raw_val)
    except (ValueError, TypeError):
        if raw_val is None or raw_val == "":
            return "空値/NULL"
        return "文字列混入"

    if math.isinf(fv):
        return "指数表記オーバーフロー (inf)"
    if math.isnan(fv):
        return "NaN (0除算またはパーサ異常)"
    if abs(fv) > 1e+15:
        return f"巨大値 (指数={math.log10(abs(fv)):.0f}, 単位誤りの疑い)"
    if "overflow" in reason.lower():
        return "int変換オーバーフロー"
    return f"不明 ({reason})"


# ============================================================
# 値サニタイズ (強化版)
# ============================================================
def _sanitize_value(
    raw_val, multiplier: int,
    *, ticker: str = "", fye: str = "",
    quarter: str = "", col: str = "",
    tracker: _SanitizeTracker | None = None,
    sqlite_row_id: int = 0,
    source_doc_id: str | None = None,
    source_url: str | None = None,
    zip_hash: str | None = None,
) -> int | None:
    """
    DB値を安全に int(JPY) へ変換。
    異常値は None (NULL) にして tracker に記録。
    """
    if raw_val is None:
        return None

    # ガード: 空文字
    if isinstance(raw_val, str):
        raw_val = raw_val.strip()
        if raw_val == "":
            _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
                   col=col, raw_val=raw_val, reason="空文字",
                   sqlite_row_id=sqlite_row_id,
                   source_doc_id=source_doc_id,
                   source_url=source_url, zip_hash=zip_hash)
            return None

    # ガード: float 変換
    try:
        fv = float(raw_val)
    except (ValueError, TypeError) as e:
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val,
               reason=f"float変換不可: {e}",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None

    # ガード: inf / -inf / nan
    if math.isinf(fv):
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val, reason="inf",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None

    if math.isnan(fv):
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val, reason="NaN",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None

    # ガード: 巨大値 (乗算前に先にチェック)
    if abs(fv) > 1e+16:
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val,
               reason=f"元値が10^16超 ({fv:.2e})",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None

    scaled = fv * multiplier

    # ガード: 乗算後の範囲チェック
    if abs(scaled) > _MAX_SAFE_VALUE:
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val,
               reason=f"scaled超過 ({scaled:.2e})",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None

    try:
        return int(scaled)
    except (OverflowError, ValueError) as e:
        _track(tracker, ticker=ticker, fye=fye, quarter=quarter,
               col=col, raw_val=raw_val,
               reason=f"int変換失敗: {e}",
               sqlite_row_id=sqlite_row_id,
               source_doc_id=source_doc_id,
               source_url=source_url, zip_hash=zip_hash)
        return None


def _track(tracker: _SanitizeTracker | None, **kwargs):
    """tracker への記録 + ログ出力"""
    if tracker:
        tracker.add(**kwargs)
    logger.warning(
        f"[SANITIZE] {kwargs.get('reason','?')}: "
        f"col={kwargs.get('col')} val={kwargs.get('raw_val')!r} "
        f"ticker={kwargs.get('ticker')} "
        f"fye={kwargs.get('fye')} Q={kwargs.get('quarter')}"
    )


# ============================================================
# チェックポイント
# ============================================================
def _load_checkpoint(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save_checkpoint(path: str, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data["saved_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _clear_checkpoint(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ============================================================
# Supabase REST API
# ============================================================
class _SupabaseAPI:
    def __init__(self, url: str, key: str) -> None:
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        }

    def _request(self, method: str, url: str,
                 **kwargs) -> requests.Response:
        last_exc = None
        for attempt in range(_RETRY_MAX):
            try:
                r = requests.request(
                    method, url, timeout=60, **kwargs
                )
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                wait = _RETRY_BASE_SEC * (2 ** attempt)
                logger.warning(
                    f"[API] 接続エラー ({attempt+1}/{_RETRY_MAX})"
                    f" — {wait:.0f}秒待機"
                )
                time.sleep(wait)
            except requests.HTTPError as e:
                status = (
                    e.response.status_code if e.response else 0
                )
                if status == 429 or status >= 500:
                    last_exc = e
                    wait = _RETRY_BASE_SEC * (2 ** attempt)
                    if status == 429:
                        ra = e.response.headers.get("Retry-After")
                        if ra:
                            wait = max(wait, float(ra))
                    logger.warning(
                        f"[API] HTTP {status} "
                        f"({attempt+1}/{_RETRY_MAX})"
                        f" — {wait:.0f}秒待機"
                    )
                    time.sleep(wait)
                else:
                    raise
        raise last_exc  # type: ignore

    def select_all(self, table: str, select: str) -> list[dict]:
        all_rows: list[dict] = []
        offset = 0
        page = 1000
        while True:
            r = self._request(
                "GET", f"{self.rest_url}/{table}",
                headers={
                    **self.headers, "Prefer": "",
                    "Range": f"{offset}-{offset+page-1}",
                },
                params={"select": select},
            )
            rows = r.json()
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page:
                break
            offset += page
        return all_rows

    def upsert_batch(self, table: str, data: list[dict],
                     on_conflict: str = "") -> list[dict]:
        if not data:
            return []
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        r = self._request(
            "POST", f"{self.rest_url}/{table}",
            headers=self.headers, params=params, json=data,
        )
        return r.json()

    def upsert_batch_fast(self, table: str, data: list[dict],
                          on_conflict: str = "") -> int:
        if not data:
            return 0
        headers = {
            **self.headers,
            "Prefer": (
                "return=headers-only,"
                "resolution=merge-duplicates"
            ),
        }
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        self._request(
            "POST", f"{self.rest_url}/{table}",
            headers=headers, params=params, json=data,
        )
        return len(data)


# ============================================================
# ユーティリティ
# ============================================================
def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _parse_quarter(q_str: str) -> int | None:
    try:
        return int(q_str.replace("Q", "").strip())
    except (ValueError, AttributeError):
        return None


# ============================================================
# TDnet → public.financials 変換
# ============================================================
def _coerce_numeric(value) -> float | None:
    """数値変換。None/空文字/NaN/inf は None を返す。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    try:
        fv = float(value)
    except (ValueError, TypeError):
        return None
    if math.isinf(fv) or math.isnan(fv):
        return None
    return fv


def _safe_int(value) -> int | None:
    """数値を int に変換。None/空文字/NaN は None を返す。
    quarterly_results の円建て金額をそのまま int にする用途。
    """
    fv = _coerce_numeric(value)
    if fv is None:
        return None
    try:
        return int(fv)
    except (OverflowError, ValueError):
        return None


def _normalize_financials_quarter(raw_quarter: str) -> str | None:
    """TDnet quarter → public.financials 互換形式に変換。
    1Q/2Q/3Q → そのまま, 4Q → FY, 不正値 → None
    """
    q = str(raw_quarter).strip().upper()
    return _QUARTER_MAP.get(q)


def _convert_tdnet_amount(
    value, unit: str,
) -> int | None:
    """TDnet の金額値を public.financials のスケール (円) に変換。"""
    fv = _coerce_numeric(value)
    if fv is None:
        return None
    multiplier = _UNIT_MULTIPLIER.get(unit, 1)
    scaled = fv * multiplier
    if abs(scaled) > _MAX_SAFE_VALUE:
        return None
    try:
        return int(scaled)
    except (OverflowError, ValueError):
        return None


# ============================================================
# financials 単位正規化 (百万円統一)
# ============================================================
_HEURISTIC_YEN_THRESHOLD = 1_000_000_000  # 10億


def _detect_source_origin(row) -> str:
    """quarterly_results 行の由来を判定する。

    Returns:
        'tdnet'   — source_url あり → TDnet 由来 (値は円)
        'jquants' — メタ情報なし (source_url/source_doc_id/zip_hash 全欠)
                    → J-Quants 由来 (値は百万円)
        'unknown' — 一部メタあり (source_doc_id だけなど)
                    → 判定不能。補助ヒューリスティック適用。
    """
    # sqlite3.Row は .get() を持たないため、安全にアクセス
    def _get(key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    source_url = (_get("source_url") or "").strip()
    if source_url:
        return "tdnet"

    source_doc_id = (_get("source_doc_id") or "").strip()
    zip_hash = (_get("zip_hash") or "").strip()

    if not source_doc_id and not zip_hash:
        return "jquants"

    return "unknown"


def _normalize_to_millions(
    value, origin: str, *, col: str = "", ticker: str = "",
) -> int | None:
    """値を百万円に正規化する。

    - None は None のまま維持 (0 埋めしない)
    - TDnet 由来: 円 → 百万円 (÷ 1,000,000)
    - J-Quants 由来: 百万円 → そのまま
    - unknown: 補助ヒューリスティック + warning

    除算で端数が出る場合は round() + warning。
    """
    fv = _coerce_numeric(value)
    if fv is None:
        return None

    if origin == "tdnet":
        # TDnet: 値は円 → 百万円に変換
        result = fv / 1_000_000
        remainder = fv % 1_000_000
        if remainder != 0:
            logger.warning(
                f"[NORMALIZE] 円→百万円で端数発生: "
                f"ticker={ticker} col={col} "
                f"raw={fv} → {result:.6f}"
            )
            return round(result)
        return int(result)

    elif origin == "jquants":
        # J-Quants: 値は百万円 → そのまま
        return int(fv)

    else:
        # unknown: 補助ヒューリスティック
        if abs(fv) > _HEURISTIC_YEN_THRESHOLD:
            # > 10億は円建て疑い → 百万円変換
            logger.warning(
                f"[NORMALIZE] 不明由来+大きい値 → 円→百万円変換: "
                f"ticker={ticker} col={col} val={fv}"
            )
            result = fv / 1_000_000
            if fv % 1_000_000 != 0:
                return round(result)
            return int(result)
        else:
            # 小さい値 → 百万円とみなす
            return int(fv)


def _build_financials_rows_from_tdnet(
    sqlite_rows: list,
) -> list[dict]:
    """
    SQLite の quarterly_results 行リストから
    public.financials 用の dict リストを構築する。

    ・ticker: common_ticker.normalize_ticker で正規化
    ・period: fiscal_year_end をそのまま使用
    ・quarter: 1Q/2Q/3Q/FY に正規化
    ・金額: field 単位で NormalizedField を生成し、confidence ベースでマージ
    ・source: 'tdnet'

    同一 (ticker, period, quarter) に複数行が存在する場合:
      field 単位 confidence 比較マージ (merge_row_fields)
      tie-break: confidence > source優先順 > anomaly少 > 既存維持
    """
    from src.normalization.field_metadata import (
        NormalizedField, SourceType, RawUnit,
        map_field_source_to_source_type, map_source_unit_to_raw_unit,
    )
    from src.normalization.normalize_field import normalize_financial_field
    from src.normalization.merge_fields import merge_row_fields

    now_iso = datetime.now(JST).isoformat()
    previewed = 0
    origin_counts: dict[str, int] = {}
    collision_log: list[str] = []

    _AMOUNT_COLS = ("sales", "gross_profit", "operating_profit")
    _ALL_NORM_COLS = ("sales", "gross_profit", "operating_profit", "cost_of_sales")

    # Normalization report counters
    norm_report = {
        "auto_unit_converted": 0,
        "confidence_high": 0,    # > 0.8
        "confidence_medium": 0,  # 0.6-0.8
        "confidence_low": 0,     # < 0.6
        "anomaly_flags": collections.Counter(),
        "field_merge_wins": collections.Counter(),
    }

    def _get_row_field(row, key):
        """sqlite3.Row safe accessor"""
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    def _parse_field_sources(row) -> dict:
        """field_sources JSON を解析"""
        raw = _get_row_field(row, "field_sources")
        if not raw:
            return {}
        try:
            return json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _determine_field_source_type(
        field_name: str, field_sources: dict, origin: str,
    ) -> str:
        """field 単位の source_type を決定"""
        fs_val = field_sources.get(field_name, "")
        if fs_val:
            return map_field_source_to_source_type(fs_val)
        # field_sources に記録がない場合、origin から推定
        if origin == "jquants":
            return SourceType.JQUANTS
        elif origin == "tdnet":
            return SourceType.TDNET_SUMMARY_XBRL  # デフォルト
        return SourceType.UNKNOWN

    def _determine_raw_unit(
        source_type: str, origin: str, row,
    ) -> str:
        """field の raw_unit を決定"""
        # source_unit が DB に保存されている場合
        source_unit = _get_row_field(row, "source_unit")
        if source_unit:
            mapped = map_source_unit_to_raw_unit(source_unit)
            if mapped != RawUnit.UNKNOWN:
                return mapped

        # source_type ベースの推定
        if source_type in (SourceType.TDNET_SUMMARY_XBRL, SourceType.TDNET_ATTACHMENT_XBRL):
            return RawUnit.YEN
        elif source_type == SourceType.JQUANTS:
            return RawUnit.MILLION_YEN
        elif origin == "tdnet":
            return RawUnit.YEN
        elif origin == "jquants":
            return RawUnit.MILLION_YEN
        return RawUnit.UNKNOWN

    # ============================================================
    # Phase 1: 全行を field 単位 NormalizedField に変換
    # ============================================================
    candidates: list[dict] = []

    for r in sqlite_rows:
        raw_ticker = r["company_code"]
        ticker = normalize_ticker(raw_ticker)
        period = r["fiscal_year_end"]
        raw_quarter = r["quarter"]
        quarter = _normalize_financials_quarter(raw_quarter)

        if not ticker or not period or quarter is None:
            continue

        origin = _detect_source_origin(r)
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

        field_sources = _parse_field_sources(r)

        # field 単位で NormalizedField を生成
        norm_fields: dict[str, NormalizedField | None] = {}
        for col in _ALL_NORM_COLS:
            raw_val = _coerce_numeric(_get_row_field(r, col))
            source_type = _determine_field_source_type(col, field_sources, origin)
            raw_unit = _determine_raw_unit(source_type, origin, r)

            nf = normalize_financial_field(
                raw_val,
                field_name=col,
                source_type=source_type,
                raw_unit=raw_unit,
                origin=origin,
            )
            norm_fields[col] = nf

            # レポート集計
            if nf.normalized_value is not None:
                if nf.meta.confidence > 0.8:
                    norm_report["confidence_high"] += 1
                elif nf.meta.confidence >= 0.6:
                    norm_report["confidence_medium"] += 1
                else:
                    norm_report["confidence_low"] += 1

                for flag in nf.meta.anomaly_flags:
                    norm_report["anomaly_flags"][flag] += 1
                    if flag == "unit_converted":
                        norm_report["auto_unit_converted"] += 1

        # serving 用カラムの値を取り出す
        amounts = {
            col: (norm_fields[col].normalized_value if norm_fields[col] else None)
            for col in _AMOUNT_COLS
        }

        # 全金額が Null なら投入しない
        if all(v is None for v in amounts.values()):
            continue

        if previewed < 5:
            logger.info(
                f"[FINANCIALS] ticker={raw_ticker}→{ticker} "
                f"origin={origin}"
            )
            previewed += 1

        candidates.append({
            "ticker": ticker,
            "period": period,
            "quarter": quarter,
            "sales": amounts["sales"],
            "gross_profit": amounts["gross_profit"],
            "operating_profit": amounts["operating_profit"],
            "source": "tdnet",
            "updated_at": now_iso,
            "_origin": origin,
            "_norm_fields": norm_fields,  # マージ判定用
        })

    # ============================================================
    # Phase 2: field 単位 confidence マージ
    # ============================================================
    merged: dict[tuple, dict] = {}
    collision_count = 0

    for row in candidates:
        key = (row["ticker"], row["period"], row["quarter"])
        if key not in merged:
            merged[key] = row
            continue

        # 衝突発生 — field 単位 confidence マージ
        collision_count += 1
        existing = merged[key]
        new_row = row

        merged_fields = merge_row_fields(
            existing["_norm_fields"], new_row["_norm_fields"],
        )

        # マージ結果から serving 値を再構築
        for col in _AMOUNT_COLS:
            nf = merged_fields.get(col)
            existing[col] = nf.normalized_value if nf else None

        existing["_norm_fields"] = merged_fields

        # どの source が勝ったかをログ
        winner_sources = set()
        for col in _AMOUNT_COLS:
            nf = merged_fields.get(col)
            if nf and nf.normalized_value is not None:
                winner_sources.add(nf.meta.source_type)
                norm_report["field_merge_wins"][nf.meta.source_type] += 1

        collision_log.append(
            f"  {key[0]} {key[1]} {key[2]}: "
            f"field merge — winning sources: {sorted(winner_sources)}"
        )

    # ============================================================
    # Phase 3: Quarantine (serving 除外) — 責務分離
    # ============================================================
    quarantine: list[dict] = []

    # Rule 1: FY 行で sales AND gross_profit が None
    keys_to_remove: list[tuple] = []
    for key, row in merged.items():
        ticker, period, quarter = key
        if quarter != "FY":
            continue
        if row.get("sales") is None and row.get("gross_profit") is None:
            quarantine.append({
                "ticker": ticker, "period": period, "quarter": quarter,
                "sales": row.get("sales"),
                "gross_profit": row.get("gross_profit"),
                "operating_profit": row.get("operating_profit"),
                "source": row.get("_origin", "unknown"),
                "reason": "FY行で sales+gp が共に None（不完全行）",
            })
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del merged[key]

    # Rule 2: FY/3Q 桁ずれ
    keys_to_remove = []
    for key, row in merged.items():
        ticker, period, quarter = key
        if quarter != "FY":
            continue
        fy_sales = row.get("sales")
        if fy_sales is None or fy_sales <= 0:
            continue
        q3_key = (ticker, period, "3Q")
        q3_row = merged.get(q3_key)
        if not q3_row:
            continue
        q3_sales = q3_row.get("sales")
        if q3_sales is None or q3_sales <= 0:
            continue
        ratio = fy_sales / q3_sales
        if ratio > 10.0:
            quarantine.append({
                "ticker": ticker, "period": period, "quarter": "FY",
                "sales": fy_sales,
                "gross_profit": row.get("gross_profit"),
                "operating_profit": row.get("operating_profit"),
                "source": row.get("_origin", "unknown"),
                "reason": f"FY/3Q sales 桁ずれ: FY={fy_sales:.0f} / 3Q={q3_sales:.0f} = {ratio:.1f}x (>10.0)",
            })
            keys_to_remove.append(key)

    for key in keys_to_remove:
        if key in merged:
            del merged[key]

    if quarantine:
        logger.warning(
            f"[FINANCIALS] quarantine: {len(quarantine)}件を serving から除外"
        )
        for q in quarantine:
            logger.warning(
                f"  QUARANTINE: {q['ticker']} {q['period']} {q['quarter']} "
                f"sales={q['sales']} gp={q['gross_profit']} op={q['operating_profit']} "
                f"source={q['source']} reason={q['reason']}"
            )

    # ============================================================
    # Phase 4: Serving payload 生成 + レポート
    # ============================================================
    result: list[dict] = []
    for row in merged.values():
        clean = {
            k: v for k, v in row.items()
            if k not in ("_origin", "_norm_fields")
        }
        result.append(clean)

    # 由来別集計ログ
    logger.info(f"[FINANCIALS] origin counts: {origin_counts}")

    # 衝突マージログ
    if collision_log:
        logger.info(f"[FINANCIALS] 衝突マージ: {collision_count}件")
        for line in collision_log[:20]:
            logger.info(line)
        if len(collision_log) > 20:
            logger.info(f"  ... 他 {len(collision_log)-20} 件")

    # Normalization レポート
    logger.info(
        f"[NORMALIZE] === Normalization Report ===\n"
        f"  auto_unit_converted: {norm_report['auto_unit_converted']}\n"
        f"  confidence: high(>0.8)={norm_report['confidence_high']} "
        f"medium(0.6-0.8)={norm_report['confidence_medium']} "
        f"low(<0.6)={norm_report['confidence_low']}\n"
        f"  anomaly_flags: {dict(norm_report['anomaly_flags'])}\n"
        f"  field_merge_wins: {dict(norm_report['field_merge_wins'])}"
    )

    logger.info(
        f"[FINANCIALS] 候補={len(candidates)} → "
        f"マージ後={len(result)} (衝突解消={collision_count}件)"
    )

    return result


# ============================================================
# メイン：5フェーズ一括処理
# ============================================================
def push_sqlite_to_supabase(
    db_path: str,
    supabase_url: str = "",
    supabase_key: str = "",
    dry_run: bool = False,
    limit: int = 0,
    resume: bool = False,
    batch_size: int = _BATCH_SIZE,
    checkpoint_path: str = "",
) -> dict:
    # --- 接続情報 ---
    if not supabase_url or not supabase_key:
        _load_dotenv()
        supabase_url = (
            supabase_url or os.environ.get("SUPABASE_URL", "")
        )
        supabase_key = (
            supabase_key or os.environ.get("SUPABASE_ANON_KEY", "")
        )

    if not supabase_url or not supabase_key:
        raise ValueError(
            ".env ファイルが見つからないか、接続情報が未設定です。\n"
            "  SUPABASE_URL と SUPABASE_ANON_KEY を .env に"
            "設定してください。"
        )

    if not checkpoint_path:
        checkpoint_path = os.path.join(
            _PROJECT_ROOT, _CHECKPOINT_FILE
        )

    quarantine_path = os.path.join(_PROJECT_ROOT, _QUARANTINE_DB)
    api = _SupabaseAPI(supabase_url, supabase_key)

    # --- SQLite 読み取り ---
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"DBファイルが見つかりません: {db_path}"
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    total_count = conn.execute(
        "SELECT COUNT(*) FROM quarterly_results"
    ).fetchone()[0]

    ckpt = _load_checkpoint(checkpoint_path) if resume else {}
    resume_phase = ckpt.get("phase", 0)
    resume_offset = ckpt.get("offset", 0)

    query = "SELECT * FROM quarterly_results ORDER BY id"
    params: list = []
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    target = len(rows)
    logger.info(
        f"[PUSH] SQLite総行数: {total_count}, "
        f"今回対象: {target} 件"
    )

    stats = {
        "sqlite_rows": total_count,
        "target_rows": target,
        "companies_upserted": 0,
        "periods_upserted": 0,
        "disclosures_upserted": 0,
        "facts_pushed": 0,
        "facts_sanitized": 0,
        "financials_scanned": 0,
        "financials_built": 0,
        "financials_inserted": 0,
        "financials_skipped_existing": 0,
        "financials_skipped_invalid": 0,
        "errors": 0,
        "complete": False,
    }

    if dry_run:
        logger.info(
            "[PUSH] dry-run モード: Supabase への書き込みは"
            "スキップ"
        )
        stats["complete"] = True
        return stats

    if not rows:
        stats["complete"] = True
        return stats

    t0 = time.time()
    tracker = _SanitizeTracker()
    qdb = _QuarantineDB(quarantine_path)

    try:
        # =============================================
        # Phase 1: companies 一括 upsert
        # =============================================
        if resume_phase <= 1:
            logger.info("[Phase 1/4] companies 一括 upsert...")
            unique_tickers = sorted(
                set(r["company_code"] for r in rows)
            )
            payloads = [
                {"ticker_code": t, "is_active": True}
                for t in unique_tickers
            ]
            ticker_to_cid: dict[str, int] = {}
            for chunk in _chunks(payloads, _MASTER_BATCH):
                result = api.upsert_batch(
                    "companies", chunk,
                    on_conflict="ticker_code",
                )
                for c in result:
                    ticker_to_cid[c["ticker_code"]] = (
                        c["company_id"]
                    )
                stats["companies_upserted"] += len(chunk)
            logger.info(
                f"  → {len(ticker_to_cid)} 社 完了 "
                f"({time.time()-t0:.1f}秒)"
            )
            _save_checkpoint(checkpoint_path, {
                "phase": 1, "offset": 0, **stats,
            })
        else:
            logger.info(
                "[Phase 1/4] companies — "
                "チェックポイントからスキップ"
            )
            existing = api.select_all(
                "companies", "company_id,ticker_code"
            )
            ticker_to_cid = {
                c["ticker_code"]: c["company_id"]
                for c in existing
            }
            logger.info(
                f"  → {len(ticker_to_cid)} 社 読込済"
            )

        # =============================================
        # Phase 2: periods 一括 upsert
        # =============================================
        if resume_phase <= 2:
            logger.info("[Phase 2/4] periods 一括 upsert...")
            seen: set[tuple] = set()
            payloads = []
            parse_errors = 0
            for r in rows:
                ticker = r["company_code"]
                fye = r["fiscal_year_end"]
                q = _parse_quarter(r["quarter"])
                if q is None:
                    parse_errors += 1
                    continue
                pkey = (ticker, fye, q)
                if pkey in seen:
                    continue
                seen.add(pkey)
                cid = ticker_to_cid.get(ticker)
                if cid is None:
                    continue
                payloads.append({
                    "company_id": cid,
                    "fiscal_year_end": fye,
                    "fiscal_year": int(fye.split("-")[0]),
                    "quarter": q,
                    "is_full_year": q == 4,
                })
            if parse_errors:
                logger.warning(
                    f"  quarter解析エラー: {parse_errors} 件"
                )
                stats["errors"] += parse_errors

            period_key_to_pid: dict[tuple, int] = {}
            for chunk in _chunks(payloads, _MASTER_BATCH):
                result = api.upsert_batch(
                    "periods", chunk,
                    on_conflict=(
                        "company_id,fiscal_year_end,quarter"
                    ),
                )
                for p in result:
                    key = (
                        p["company_id"],
                        str(p["fiscal_year_end"]),
                        p["quarter"],
                    )
                    period_key_to_pid[key] = p["period_id"]
                stats["periods_upserted"] += len(chunk)
            logger.info(
                f"  → {len(period_key_to_pid)} 期間 完了 "
                f"({time.time()-t0:.1f}秒)"
            )
            _save_checkpoint(checkpoint_path, {
                "phase": 2, "offset": 0, **stats,
            })
        else:
            logger.info(
                "[Phase 2/4] periods — "
                "チェックポイントからスキップ"
            )
            existing = api.select_all(
                "periods",
                "period_id,company_id,"
                "fiscal_year_end,quarter",
            )
            period_key_to_pid = {}
            for p in existing:
                key = (
                    p["company_id"],
                    str(p["fiscal_year_end"]),
                    p["quarter"],
                )
                period_key_to_pid[key] = p["period_id"]
            logger.info(
                f"  → {len(period_key_to_pid)} 期間 読込済"
            )

        # =============================================
        # Phase 3: disclosures 一括 upsert
        # =============================================
        if resume_phase <= 3:
            logger.info("[Phase 3/4] disclosures 一括 upsert...")
            now_iso = datetime.now(JST).isoformat()
            seen_sha: set[str] = set()
            disc_payloads: list[dict] = []
            for r in rows:
                ticker = r["company_code"]
                fye = r["fiscal_year_end"]
                q = _parse_quarter(r["quarter"])
                if q is None:
                    continue
                sha = f"sqlite-sync-{ticker}-{fye}-Q{q}"
                if sha in seen_sha:
                    continue
                seen_sha.add(sha)
                cid = ticker_to_cid.get(ticker)
                if cid is None:
                    continue
                disc_payloads.append({
                    "company_id": cid,
                    "source": "MANUAL",
                    "disclosed_at": now_iso,
                    "title": (
                        f"SQLite同期: {ticker} {fye} Q{q}"
                    ),
                    "doc_type": "TANSHIN",
                    "is_target": True,
                    "sha256": sha,
                })

            logger.info(
                f"  対象 disclosure: {len(disc_payloads)} 件"
            )
            existing_disc = api.select_all(
                "disclosures", "disclosure_id,sha256"
            )
            existing_sha: dict[str, int] = {
                d["sha256"]: d["disclosure_id"]
                for d in existing_disc if d.get("sha256")
            }
            logger.info(
                f"  既存 disclosure: {len(existing_sha)} 件"
            )

            sha_to_disc_id: dict[str, int] = dict(existing_sha)
            new_disc = [
                d for d in disc_payloads
                if d["sha256"] not in existing_sha
            ]

            if new_disc:
                logger.info(
                    f"  新規 insert: {len(new_disc)} 件"
                )
                for chunk in _chunks(new_disc, _MASTER_BATCH):
                    result = api.upsert_batch(
                        "disclosures", chunk, on_conflict=""
                    )
                    for d in result:
                        sha_to_disc_id[d["sha256"]] = (
                            d["disclosure_id"]
                        )
                    stats["disclosures_upserted"] += len(result)
            else:
                logger.info("  新規なし（全件既存）")

            logger.info(
                f"  → {len(sha_to_disc_id)} disclosure 完了 "
                f"({time.time()-t0:.1f}秒)"
            )
            _save_checkpoint(checkpoint_path, {
                "phase": 3, "offset": 0, **stats,
            })
        else:
            logger.info(
                "[Phase 3/4] disclosures — "
                "チェックポイントからスキップ"
            )
            existing_disc = api.select_all(
                "disclosures", "disclosure_id,sha256"
            )
            sha_to_disc_id = {
                d["sha256"]: d["disclosure_id"]
                for d in existing_disc if d.get("sha256")
            }
            logger.info(
                f"  → {len(sha_to_disc_id)} disclosure 読込済"
            )

        # =============================================
        # Phase 4: facts 一括 upsert
        # =============================================
        phase4_offset = (
            resume_offset if resume_phase == 4 else 0
        )
        logger.info(
            f"[Phase 4/4] facts 一括 upsert "
            f"(batch={batch_size}, offset={phase4_offset})..."
        )

        fact_buf: list[dict] = []
        processed = phase4_offset
        batch_num = 0

        for idx, r in enumerate(rows):
            if idx < phase4_offset:
                continue

            ticker = r["company_code"]
            fye = r["fiscal_year_end"]
            q_str = r["quarter"]
            q = _parse_quarter(q_str)
            if q is None:
                processed = idx + 1
                continue

            cid = ticker_to_cid.get(ticker)
            if cid is None:
                processed = idx + 1
                continue
            pid = period_key_to_pid.get((cid, str(fye), q))
            if pid is None:
                processed = idx + 1
                continue
            sha = f"sqlite-sync-{ticker}-{fye}-Q{q}"
            disc_id = sha_to_disc_id.get(sha)
            if disc_id is None:
                processed = idx + 1
                continue

            unit = r["unit"] or "百万円"
            multiplier = _UNIT_MULTIPLIER.get(unit, 1)
            row_id = r["id"]

            for sqlite_col, metric in _METRIC_MAP.items():
                raw_val = r[sqlite_col]
                value_jpy = _sanitize_value(
                    raw_val, multiplier,
                    ticker=ticker, fye=fye,
                    quarter=q_str, col=sqlite_col,
                    tracker=tracker,
                    sqlite_row_id=row_id,
                    source_doc_id=r["source_doc_id"],
                    source_url=r["source_url"],
                    zip_hash=r["zip_hash"],
                )
                if value_jpy is None:
                    if raw_val is not None:
                        stats["facts_sanitized"] += 1
                        # quarantine 書き込み
                        try:
                            cause = _estimate_cause(
                                raw_val,
                                tracker.records[-1]["reason"]
                                if tracker.records
                                else "unknown",
                            )
                            qdb.insert(
                                sqlite_row_id=row_id,
                                ticker=ticker, fye=fye,
                                quarter=q_str,
                                column_name=sqlite_col,
                                original_value=str(raw_val),
                                sanitize_reason=(
                                    tracker.records[-1]["reason"]
                                    if tracker.records
                                    else "unknown"
                                ),
                                source_doc_id=(
                                    r["source_doc_id"]
                                ),
                                source_url=r["source_url"],
                                zip_hash=r["zip_hash"],
                                estimated_cause=cause,
                            )
                        except Exception:
                            logger.error(
                                "[QUARANTINE] 書き込み失敗:\n"
                                + traceback.format_exc()
                            )
                    continue

                fact_buf.append({
                    "company_id": cid,
                    "period_id": pid,
                    "disclosure_id": disc_id,
                    "scope": "CONSOLIDATED",
                    "metric": metric,
                    "value": value_jpy,
                    "unit": "JPY",
                    "quality": "IXBRL",
                })

            processed = idx + 1

            # バッチ flush
            if len(fact_buf) >= batch_size:
                batch_num += 1
                try:
                    cnt = api.upsert_batch_fast(
                        "facts", fact_buf,
                        on_conflict=(
                            "disclosure_id,period_id,"
                            "metric,scope"
                        ),
                    )
                    stats["facts_pushed"] += cnt
                except Exception:
                    logger.error(
                        f"[Phase4] facts batch {batch_num} "
                        f"例外:\n{traceback.format_exc()}"
                    )
                    stats["errors"] += len(fact_buf)

                elapsed = time.time() - t0
                pct = processed / target * 100
                done = max(processed - phase4_offset, 1)
                eta = (
                    (elapsed / done) * (target - processed)
                )
                logger.info(
                    f"  batch {batch_num}: "
                    f"{processed}/{target} ({pct:.1f}%) "
                    f"facts={stats['facts_pushed']} "
                    f"sanitized={stats['facts_sanitized']} "
                    f"経過={elapsed:.0f}秒 "
                    f"残り≈{eta:.0f}秒"
                )
                fact_buf = []
                _save_checkpoint(checkpoint_path, {
                    "phase": 4, "offset": processed, **stats,
                })

        # 残り flush
        if fact_buf:
            batch_num += 1
            try:
                cnt = api.upsert_batch_fast(
                    "facts", fact_buf,
                    on_conflict=(
                        "disclosure_id,period_id,metric,scope"
                    ),
                )
                stats["facts_pushed"] += cnt
            except Exception:
                logger.error(
                    f"[Phase4] facts batch {batch_num} "
                    f"例外:\n{traceback.format_exc()}"
                )
                stats["errors"] += len(fact_buf)

        elapsed = time.time() - t0
        logger.info(
            f"  → facts 完了: {stats['facts_pushed']} 件 "
            f"(sanitized={stats['facts_sanitized']}) "
            f"({elapsed:.1f}秒)"
        )

        # SANITIZE サマリ出力
        tracker.print_summary()

        # =============================================
        # Phase 5: TDnet → public.financials upsert
        # =============================================
        logger.info(
            "[Phase 5/5] TDnet → public.financials "
            "upsert..."
        )
        fin_rows = _build_financials_rows_from_tdnet(rows)
        stats["financials_scanned"] = len(rows)
        stats["financials_built"] = len(fin_rows)

        if fin_rows:
            logger.info(
                f"  upsert対象: {len(fin_rows):,} 行"
            )

            financials_upserted = 0
            financials_errors = 0

            for chunk in _chunks(fin_rows, batch_size):
                try:
                    api.upsert_batch_fast(
                        "financials", chunk,
                        on_conflict="ticker,period,quarter",
                    )
                    financials_upserted += len(chunk)
                except requests.HTTPError as he:
                    resp_body = ""
                    status = 0
                    if he.response is not None:
                        status = he.response.status_code
                        resp_body = he.response.text[:500]
                    logger.error(
                        f"[Phase5] financials batch HTTP {status}:\n"
                        f"  response: {resp_body}\n"
                        f"  payload_keys: {list(chunk[0].keys()) if chunk else '?'}\n"
                        f"  payload_sample: {chunk[0] if chunk else '?'}"
                    )
                    financials_errors += len(chunk)
                    stats["errors"] += len(chunk)
                except Exception:
                    logger.error(
                        "[Phase5] financials batch 例外:\n"
                        + traceback.format_exc()
                    )
                    financials_errors += len(chunk)
                    stats["errors"] += len(chunk)

            stats["financials_inserted"] = financials_upserted

            logger.info(
                f"[FINANCIALS] scanned={stats['financials_scanned']} "
                f"built={stats['financials_built']} "
                f"upserted={financials_upserted} "
                f"errors={financials_errors}"
            )

            # ── Phase 2-A: canonical dual-write (best-effort) ──
            try:
                from lib.pipeline.canonical_writer import write_financials_canonical
                from lib.pipeline.db import get_supabase_write_config
                canonical_config = get_supabase_write_config()
                if canonical_config:
                    canonical_total = 0
                    canonical_errors = 0
                    for fr in fin_rows:
                        metrics_dict = {
                            k: fr.get(k)
                            for k in ("sales", "gross_profit", "operating_profit")
                        }
                        cw_result = write_financials_canonical(
                            ticker=fr["ticker"],
                            period=fr["period"],
                            quarter=fr["quarter"],
                            metrics_dict=metrics_dict,
                            source=fr.get("source", "tdnet"),
                            config=canonical_config,
                        )
                        canonical_total += cw_result["written"]
                        canonical_errors += cw_result["errors"]
                    logger.info(
                        f"[CANONICAL] financials dual-write: "
                        f"written={canonical_total} errors={canonical_errors}"
                    )
                else:
                    logger.warning(
                        "[CANONICAL] financials dual-write skipped: "
                        "no write config"
                    )
            except Exception as _cw_err:
                logger.warning(
                    f"[CANONICAL] financials dual-write failed "
                    f"(best-effort, legacy unaffected): {_cw_err}"
                )
        else:
            logger.info(
                "[Phase5] financials: 投入対象行なし"
            )

    finally:
        qdb.close()

    stats["complete"] = True
    _clear_checkpoint(checkpoint_path)

    elapsed = time.time() - t0
    logger.info(
        f"[PUSH] 全完了 ({elapsed:.1f}秒): "
        f"companies={stats['companies_upserted']} "
        f"periods={stats['periods_upserted']} "
        f"disclosures={stats['disclosures_upserted']} "
        f"facts={stats['facts_pushed']} "
        f"sanitized={stats['facts_sanitized']} "
        f"financials_inserted={stats['financials_inserted']} "
        f"errors={stats['errors']}"
    )
    return stats


# ============================================================
# CLI
# ============================================================
def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in (
            "utf-8", "utf8"
        ):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8",
                errors="replace",
            )
    if sys.stderr and hasattr(sys.stderr, "encoding"):
        if sys.stderr.encoding and sys.stderr.encoding.lower() not in (
            "utf-8", "utf8"
        ):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8",
                errors="replace",
            )

    parser = argparse.ArgumentParser(
        description=(
            "SQLite (decision_db.db) → Supabase push (バッチ版)"
        ),
    )
    parser.add_argument(
        "--db", default="decision_db.db",
        help="SQLiteファイルパス (default: decision_db.db)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="処理する行数の上限(0=全件)。例: --limit 1000",
    )
    parser.add_argument(
        "--batch-size", type=int, default=_BATCH_SIZE,
        help=f"バッチサイズ (default: {_BATCH_SIZE})",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="前回のチェックポイントから再開",
    )
    parser.add_argument(
        "--reset-checkpoint", action="store_true",
        help="チェックポイントをリセット",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Supabaseへの書き込みをスキップ",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = (
        logging.DEBUG if args.verbose else logging.INFO
    )
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    checkpoint_path = os.path.join(
        _PROJECT_ROOT, _CHECKPOINT_FILE
    )

    if args.reset_checkpoint:
        _clear_checkpoint(checkpoint_path)
        print("[INFO] チェックポイントをリセットしました")
        if (not args.limit and not args.resume
                and not args.dry_run):
            sys.exit(0)

    print()
    print("=" * 55)
    print("  SQLite → Supabase push (バッチ版)")
    print("=" * 55)
    print(f"  DB         : {db_path}")
    print(f"  batch_size : {args.batch_size}")
    if args.limit:
        print(f"  limit      : {args.limit} 行")
    if args.resume:
        ckpt = _load_checkpoint(checkpoint_path)
        print(
            f"  resume     : phase={ckpt.get('phase', 0)} "
            f"offset={ckpt.get('offset', 0)}"
        )
    if args.dry_run:
        print("  Mode       : dry-run")
    print()

    try:
        stats = push_sqlite_to_supabase(
            db_path=db_path,
            dry_run=args.dry_run,
            limit=args.limit,
            resume=args.resume,
            batch_size=args.batch_size,
            checkpoint_path=checkpoint_path,
        )

        icon = "✅" if stats["complete"] else "⏸️"
        label = (
            "push 完了" if stats["complete"]
            else "push 中断"
        )
        print("=" * 55)
        print(f"  {icon} {label}")
        print("=" * 55)
        print(f"  SQLite総行数       : {stats['sqlite_rows']}")
        print(f"  今回対象           : {stats['target_rows']}")
        print(
            f"  companies upsert   : "
            f"{stats['companies_upserted']}"
        )
        print(
            f"  periods upsert     : "
            f"{stats['periods_upserted']}"
        )
        print(
            f"  disclosures upsert : "
            f"{stats['disclosures_upserted']}"
        )
        print(f"  facts push         : {stats['facts_pushed']}")
        print(
            f"  値サニタイズ       : "
            f"{stats['facts_sanitized']}"
        )
        print(
            f"  financials insert  : "
            f"{stats['financials_inserted']}"
        )
        print(
            f"  financials skip    : "
            f"{stats['financials_skipped_existing']}"
        )
        print(f"  エラー             : {stats['errors']}")
        print(
            f"  quarantine DB      : "
            f"{os.path.join(_PROJECT_ROOT, _QUARANTINE_DB)}"
        )
        print("=" * 55)
        print()
        sys.exit(0)

    except Exception:
        print()
        print("=" * 55)
        print("  ❌ push 失敗")
        print("=" * 55)
        traceback.print_exc()
        print("=" * 55)
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
