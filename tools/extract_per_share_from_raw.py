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
import calendar
import json
import logging
import math
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

from lib.runtime_paths import runtime_path
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
    """per_share_data テーブルが無ければ作成。既存テーブルへのカラム追加 migration も実行。"""
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
    else:
        # 既存テーブルへのカラム追加 migration（冪等: 失敗しても続行）
        _migrations = [
            "ALTER TABLE per_share_data ADD COLUMN initial_forecast_eps REAL",
            "ALTER TABLE per_share_data ADD COLUMN forecast_eps_basis_factor REAL NOT NULL DEFAULT 1.0",
        ]
        for sql in _migrations:
            try:
                conn.execute(sql)
                conn.commit()
                logger.info(f"[SCHEMA] migration 適用: {sql[:60]}")
            except Exception:
                pass  # カラム既存の場合は無視


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


def _annual_dividend(
    raw: dict,
    annual_key: str,
    component_keys: tuple[str, ...],
    *,
    require_fiscal_year_end: bool,
) -> float | None:
    """Return authoritative annual DPS, with a guarded component fallback.

    J-Quants annual fields already include ordinary, special, and
    commemorative dividends, so they must outrank a component sum.  A sum is
    only safe once the fiscal-year-end component is present; otherwise an
    interim-only disclosure would be mistaken for a full-year dividend.
    """
    annual = safe_float(raw.get(annual_key))
    if annual is not None:
        return annual

    components = [safe_float(raw.get(key)) for key in component_keys]
    if require_fiscal_year_end and components[-1] is None:
        return None
    present = [value for value in components if value is not None]
    return sum(present) if present else None


_DIVIDEND_COMPONENTS = (
    ("Div1Q", "FDiv1Q"),
    ("Div2Q", "FDiv2Q"),
    ("Div3Q", "FDiv3Q"),
    ("DivFY", "FDivFY"),
)


def _has_mixed_dividend_components(raw: dict) -> bool:
    """Return whether actual and still-forecast DPS coexist in one FY row."""
    has_actual = any(safe_float(raw.get(actual_key)) is not None
                     for actual_key, _ in _DIVIDEND_COMPONENTS)
    has_forecast = any(
        safe_float(raw.get(actual_key)) is None
        and safe_float(raw.get(forecast_key)) is not None
        for actual_key, forecast_key in _DIVIDEND_COMPONENTS
    )
    return has_actual and has_forecast


def _snap_corporate_action_multiplier(value: float) -> float | None:
    """Snap an independently-derived share-basis ratio to a safe rational."""
    if not math.isfinite(value) or value <= 0:
        return None
    if 0.95 <= value <= 1.05:
        return 1.0
    if 2 / 3 < value < 1.5:
        return None

    rational = Fraction(value).limit_denominator(20)
    if rational.numerator <= 0:
        return None
    is_whole_ratio = rational.numerator == 1 or rational.denominator == 1
    is_three_for_two = {rational.numerator, rational.denominator} == {2, 3}
    if not (is_whole_ratio or is_three_for_two):
        return None
    if max(rational.numerator, rational.denominator) > 10:
        return None
    snapped = rational.numerator / rational.denominator
    if not math.isclose(value, snapped, rel_tol=0.025, abs_tol=0.005):
        return None
    return snapped


def _inferred_forecast_share_basis_factor(raw: dict) -> float:
    """Infer the factor converting disclosed FEPS to disclosure-date basis.

    FNP / FEPS exposes the forecast share denominator.  Accept a non-unit
    factor only when both average shares and period-end net issued shares
    independently produce the same small rational ratio.  The raw FEPS stays
    untouched; consumers combine this metadata with effective-date market
    adjustment factors to normalize it to their price date.
    """
    forecast_profit = safe_float(raw.get("FNP") or raw.get("FNCNP"))
    forecast_eps = safe_float(raw.get("FEPS"))
    average_shares = safe_float(raw.get("AvgSh"))
    issued = safe_float(raw.get("ShOutFY"))
    treasury = safe_float(raw.get("TrShFY"))
    net_issued = None
    if issued is not None and treasury is not None and issued > treasury:
        net_issued = issued - treasury

    if (
        forecast_profit is None
        or forecast_eps is None
        or forecast_profit == 0
        or forecast_eps == 0
        or average_shares is None
        or average_shares <= 0
        or net_issued is None
    ):
        return 1.0

    forecast_shares = abs(forecast_profit / forecast_eps)
    from_average = _snap_corporate_action_multiplier(
        forecast_shares / average_shares
    )
    from_period_end = _snap_corporate_action_multiplier(
        forecast_shares / net_issued
    )
    if (
        from_average is None
        or from_period_end is None
        or not math.isclose(from_average, from_period_end, rel_tol=1e-12)
    ):
        return 1.0
    return from_average


def _forecast_component_basis_multiplier(raw: dict) -> float:
    """Return a safe multiplier for homogeneous remaining DPS components."""
    if safe_float(raw.get("FDivAnn")) is not None:
        return 1.0
    if not _has_mixed_dividend_components(raw):
        return 1.0
    return _inferred_forecast_share_basis_factor(raw)


def _mixed_forecast_annual_dividend(
    raw: dict,
    *,
    actual_adjustments: dict[str, float] | None = None,
    forecast_multiplier: float = 1.0,
) -> float | None:
    """Build annual DPS from actual-to-date plus remaining forecast.

    Each component is converted independently to the disclosure-date basis.
    Viewer can then apply only actions after that disclosure to compare the
    annual DPS with a later raw close, without double adjustment.
    """
    annual = safe_float(raw.get("FDivAnn"))
    if annual is not None:
        return annual

    adjustments = actual_adjustments or {}
    values: list[float] = []
    for actual_key, forecast_key in _DIVIDEND_COMPONENTS:
        actual = safe_float(raw.get(actual_key))
        if actual is not None:
            values.append(actual * adjustments.get(actual_key, 1.0))
            continue
        forecast = safe_float(raw.get(forecast_key))
        if forecast is not None:
            values.append(forecast * forecast_multiplier)

    actual_fy = safe_float(raw.get("DivFY"))
    forecast_fy = safe_float(raw.get("FDivFY"))
    if actual_fy is None and forecast_fy is None:
        return None
    return sum(values) if values else None


def _cumulative_action_factor(
    actions: list[tuple[str, float]],
    *,
    after_date: str,
    through_date: str,
) -> float:
    return math.prod(
        factor
        for action_date, factor in actions
        if action_date > after_date and action_date <= through_date
    )


def _actual_component_adjustments(
    raw: dict,
    prior_rows: list[dict],
    actions: list[tuple[str, float]],
) -> dict[str, float]:
    """Infer a paid component's basis from its first matching disclosed value.

    Exact value continuity is required.  An action between that source
    disclosure and the current disclosure adjusts only the repeated actual
    component; values first disclosed after the action remain untouched.
    """
    disclosed = str(raw.get("DiscDate") or raw.get("DisclosedDate") or "")
    if not disclosed or not actions:
        return {}

    result: dict[str, float] = {}
    for actual_key, forecast_key in _DIVIDEND_COMPONENTS:
        current = safe_float(raw.get(actual_key))
        if current is None:
            continue
        basis_date: str | None = None
        # Use the first continuous disclosure of this exact component value,
        # not merely the latest repeat.  A paid interim dividend can continue
        # to be reported on its record-date (pre-split) basis for multiple
        # disclosures after the action (for example 2Q -> 3Q).
        for previous in prior_rows:
            previous_date = str(
                previous.get("DiscDate") or previous.get("DisclosedDate") or ""
            )
            if not previous_date or previous_date >= disclosed:
                continue
            candidates = (
                safe_float(previous.get(actual_key)),
                safe_float(previous.get(forecast_key)),
            )
            if any(
                candidate is not None
                and math.isclose(current, candidate, rel_tol=1e-12, abs_tol=1e-9)
                for candidate in candidates
            ):
                basis_date = previous_date
                break
        if basis_date is None:
            continue
        factor = _cumulative_action_factor(
            actions, after_date=basis_date, through_date=disclosed
        )
        if not math.isclose(factor, 1.0, rel_tol=1e-12):
            result[actual_key] = factor
    return result


def _derived_bps(raw: dict) -> float | None:
    """Derive current-basis BPS when J-Quants omits the explicit field."""
    explicit = safe_float(raw.get("BPS"))
    if explicit is not None:
        return explicit

    equity = safe_float(raw.get("Eq") or raw.get("Equity"))
    issued = safe_int(raw.get("ShOutFY"))
    treasury = safe_int(raw.get("TrShFY"))
    if equity is None or issued is None or treasury is None:
        return None
    denominator = issued - treasury
    if denominator <= 0:
        return None
    return round(equity / denominator, 2)


def _next_fiscal_year_end(period_str: str) -> str | None:
    """当期末日 → 翌期末日を計算する。閏年・月末ズレを考慮。

    例: 2026-03-31 → 2027-03-31
        2024-02-29 → 2025-02-28 (翌年は閏年でない)
        2026-12-31 → 2027-12-31
    """
    try:
        d = date.fromisoformat(period_str)
        next_year = d.year + 1
        last_day = calendar.monthrange(next_year, d.month)[1]
        return date(next_year, d.month, min(d.day, last_day)).isoformat()
    except Exception:
        return None


def _normalize_period(raw: str) -> str:
    raw = (raw or "").strip().upper()
    return raw if raw in ("1Q", "2Q", "3Q", "4Q", "FY") else (raw or "UNKNOWN")


def extract_per_share(
    raw: dict,
    *,
    actual_dividend_adjustments: dict[str, float] | None = None,
) -> dict | None:
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
    forecast_eps_basis_factor = _inferred_forecast_share_basis_factor(raw)
    forecast_multiplier = _forecast_component_basis_multiplier(raw)
    raw_forecast_eps = safe_float(raw.get("FEPS"))
    forecast_div = _mixed_forecast_annual_dividend(
        raw,
        actual_adjustments=actual_dividend_adjustments,
        forecast_multiplier=forecast_multiplier,
    )
    forecast_payout = safe_float(raw.get("FPayoutRatioAnn"))
    actual_adjustments = actual_dividend_adjustments or {}

    def normalized_actual(key: str) -> float | None:
        value = safe_float(raw.get(key))
        return (
            value * actual_adjustments.get(key, 1.0)
            if value is not None
            else None
        )

    actual_dividend_annual = safe_float(raw.get("DivAnn"))
    if actual_dividend_annual is None and normalized_actual("DivFY") is not None:
        actual_dividend_annual = sum(
            value
            for key in ("Div1Q", "Div2Q", "Div3Q", "DivFY")
            if (value := normalized_actual(key)) is not None
        )

    return {
        "ticker": ticker,
        "period": fy_end,
        "quarter": quarter,
        "disclosed_date": disclosed or None,
        # 実績
        "eps": safe_float(raw.get("EPS")),
        "diluted_eps": safe_float(raw.get("DEPS")),
        "bps": _derived_bps(raw),
        "dividend_q1": normalized_actual("Div1Q"),
        "dividend_q2": normalized_actual("Div2Q"),
        "dividend_q3": normalized_actual("Div3Q"),
        "dividend_fy_end": normalized_actual("DivFY"),
        "dividend_annual": actual_dividend_annual,
        "payout_ratio": safe_float(raw.get("PayoutRatioAnn")),
        # 予想 (FEPS/FDivAnn 優先, Nx系フォールバック)
        "forecast_eps": raw_forecast_eps,
        "forecast_eps_basis_factor": forecast_eps_basis_factor,
        "forecast_dividend_annual": forecast_div,
        "forecast_payout_ratio": forecast_payout,
        # 株式数
        "shares_outstanding": safe_int(raw.get("ShOutFY")),
        "treasury_stock": safe_int(raw.get("TrShFY")),
        "avg_shares": safe_int(raw.get("AvgSh")),
        # BS
        "total_assets": safe_int(raw.get("TA") or raw.get("TotalAssets")),
        "equity": safe_int(raw.get("Eq") or raw.get("Equity")),
        "equity_ratio": safe_float(raw.get("EqAR") or raw.get("EquityRatio")),
        "source": "jquants",
    }


# ============================================================
# 翌期予想行 (NxFEPS) 生成
# ============================================================
_NXF_SOURCE = "jquants_nxf"  # 翌期予想行を識別するソースタグ


def _extract_next_year_forecast(raw: dict, fy_record: dict) -> dict | None:
    """FY確定行から翌期予想専用行を生成する。

    条件:
      - quarter == "FY"  (FY行のみ対象)
      - NxFEPS に値がある
    生成行:
      - period = 翌期末日, quarter = "FY"
      - eps = None (実績未確定)
      - forecast_eps = NxFEPS
      - source = "jquants_nxf"
    """
    if fy_record.get("quarter") != "FY":
        return None

    nx_feps = safe_float(raw.get("NxFEPS"))
    if nx_feps is None:
        return None

    next_period = _next_fiscal_year_end(fy_record.get("period", ""))
    if not next_period:
        return None

    return {
        "ticker":                   fy_record["ticker"],
        "period":                   next_period,
        "quarter":                  "FY",
        "disclosed_date":           fy_record.get("disclosed_date"),
        "eps":                      None,
        "diluted_eps":              None,
        "bps":                      None,
        "dividend_q1":              None,
        "dividend_q2":              None,
        "dividend_q3":              None,
        "dividend_fy_end":          None,
        "dividend_annual":          None,
        "payout_ratio":             None,
        "forecast_eps":             nx_feps,
        "forecast_eps_basis_factor": 1.0,
        "initial_forecast_eps":     nx_feps,   # 本決算発表時のNxFEPS = 期初予想。原則不変。
        "forecast_dividend_annual": _annual_dividend(
            raw,
            "NxFDivAnn",
            ("NxFDiv1Q", "NxFDiv2Q", "NxFDiv3Q", "NxFDivFY"),
            require_fiscal_year_end=True,
        ),
        "forecast_payout_ratio":    safe_float(raw.get("NxFPayoutRatioAnn")),
        "shares_outstanding":       None,
        "treasury_stock":           None,
        "avg_shares":               None,
        "total_assets":             None,
        "equity":                   None,
        "equity_ratio":             None,
        "source":                   _NXF_SOURCE,
    }


def _merge_primary_record(best: dict[tuple, dict], record: dict) -> None:
    """Merge a period-native J-Quants row into the canonical shadow map.

    A synthetic NxF row is only a forecast placeholder for the following
    fiscal year.  Once a period-native FY result exists, the native row owns
    the canonical provenance even when a late re-publication of the previous
    FY gave the placeholder a later disclosed date.  The original NxF value
    remains useful solely as the period's initial forecast.
    """
    key = (record["ticker"], record["period"], record["quarter"])
    existing = best.get(key)
    if existing is None:
        best[key] = record
        return

    if (
        existing.get("source") == _NXF_SOURCE
        and record.get("source") != _NXF_SOURCE
        and record.get("quarter") == "FY"
        and record.get("eps") is not None
    ):
        initial_forecast_eps = existing.get("initial_forecast_eps")
        promoted = dict(record)
        # A completed period must not regain the old NxF EPS as current
        # guidance. Dividend fields keep the existing extractor semantics.
        promoted["forecast_eps"] = None
        if initial_forecast_eps is not None:
            promoted["initial_forecast_eps"] = initial_forecast_eps
        best[key] = promoted
        return

    # disclosed_date が新しい方を優先、ただしフィールドレベルCOALESCE
    if (record.get("disclosed_date") or "") >= (existing.get("disclosed_date") or ""):
        for field in record:
            if record[field] is not None:
                existing[field] = record[field]
        # FY確定行: FEPS=空の場合でも forecast_eps を明示的に上書きして
        # Supabase の旧予想値（通期予想修正値）残留を防ぐ。
        if (
            record.get("quarter") == "FY"
            and record.get("eps") is not None
            and record.get("forecast_eps") is None
        ):
            existing["forecast_eps"] = None
    else:
        for field in record:
            if existing[field] is None and record[field] is not None:
                existing[field] = record[field]


def _merge_next_year_record(best: dict[tuple, dict], next_row: dict) -> None:
    """Merge an NxF placeholder without overriding a period-native result."""
    key = (next_row["ticker"], next_row["period"], next_row["quarter"])
    existing = best.get(key)
    if existing is None:
        best[key] = next_row
        return

    if existing.get("eps") is not None:
        # A completed FY must not regain a current forecast from an old NxF.
        # Keep the first forecast only as historical initial guidance.
        if existing.get("initial_forecast_eps") is None:
            existing["initial_forecast_eps"] = next_row.get("initial_forecast_eps")
        return

    # まだ実績なし → より新しい開示日の NxFEPS で上書き
    if (next_row.get("disclosed_date") or "") >= (existing.get("disclosed_date") or ""):
        existing["forecast_eps"] = next_row["forecast_eps"]
        existing["forecast_eps_basis_factor"] = next_row["forecast_eps_basis_factor"]
        existing["forecast_dividend_annual"] = next_row.get("forecast_dividend_annual")
        existing["forecast_payout_ratio"] = next_row.get("forecast_payout_ratio")
        existing["disclosed_date"] = next_row["disclosed_date"]


# ============================================================
# メイン処理
# ============================================================
def run(
    db_path: str,
    dry_run: bool = True,
    limit: int = 0,
    ticker: str | None = None,
) -> dict:
    stats = {"total_raw": 0, "extracted": 0, "upserted": 0, "nxf_upserted": 0, "skipped": 0, "errors": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)

    # raw_json を持つ全行を取得
    query = """
        SELECT local_code, current_fiscal_year_end_date, type_of_current_period,
               disclosed_date, raw_json
        FROM jquants_financials_normalized
        WHERE raw_json IS NOT NULL AND raw_json != ''
    """
    query_params: list[str] = []
    ticker_filter = normalize_ticker(ticker) if ticker else None
    if ticker_filter:
        query += " AND substr(local_code, 1, 4) = ?"
        query_params.append(ticker_filter)
    query += " ORDER BY local_code, current_fiscal_year_end_date, type_of_current_period"
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, query_params).fetchall()
    stats["total_raw"] = len(rows)
    logger.info(f"[RAW] raw_json 付き: {len(rows):,} 行")

    parsed_rows: list[dict] = []
    for row in rows:
        try:
            raw = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            stats["skipped"] += 1
            continue
        parsed_rows.append(raw)

    history: dict[tuple[str, str], list[dict]] = {}
    for raw in parsed_rows:
        code = str(raw.get("Code") or raw.get("LocalCode") or "")
        fiscal_year = str(
            raw.get("CurFYEn") or raw.get("CurrentFiscalYearEndDate") or ""
        )
        if not code or not fiscal_year:
            continue
        key = (normalize_ticker(code), fiscal_year)
        history.setdefault(key, []).append(raw)
    for prior_rows in history.values():
        prior_rows.sort(key=lambda item: str(
            item.get("DiscDate") or item.get("DisclosedDate") or ""
        ))

    action_map: dict[str, list[tuple[str, float]]] = {}
    for action_row in conn.execute(
        """
        SELECT ticker, date, adj_factor
        FROM market_data
        WHERE adj_factor IS NOT NULL AND adj_factor > 0 AND adj_factor != 1
        ORDER BY ticker, date
        """
    ):
        action_map.setdefault(str(action_row["ticker"]), []).append(
            (str(action_row["date"]), float(action_row["adj_factor"]))
        )

    # 重複排除: (ticker, period, quarter) → 最新の disclosed_date のデータを優先
    best: dict[tuple, dict] = {}

    for raw in parsed_rows:
        code = str(raw.get("Code") or raw.get("LocalCode") or "")
        fiscal_year = str(
            raw.get("CurFYEn") or raw.get("CurrentFiscalYearEndDate") or ""
        )
        raw_ticker = normalize_ticker(code)
        adjustments = _actual_component_adjustments(
            raw,
            history.get((raw_ticker, fiscal_year), []),
            action_map.get(raw_ticker, []),
        )
        record = extract_per_share(
            raw,
            actual_dividend_adjustments=adjustments,
        )
        if not record:
            stats["skipped"] += 1
            continue
        if ticker_filter and record["ticker"] != ticker_filter:
            continue

        stats["extracted"] += 1
        _merge_primary_record(best, record)

        # ---- 翌期予想行 (NxFEPS) の生成 ----
        next_row = _extract_next_year_forecast(raw, record)
        if next_row:
            _merge_next_year_record(best, next_row)

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

    # ============================================================
    # UPSERT — 通常行 と 翌期予想行 (NxF) を分けて処理
    # ============================================================
    # 通常行: 全カラム上書き。ただし initial_forecast_eps は COALESCE 保護
    # (main 行は initial_forecast_eps=None なので既存値を消さない)
    cols = [
        "ticker", "period", "quarter", "disclosed_date",
        "eps", "diluted_eps", "bps",
        "dividend_q1", "dividend_q2", "dividend_q3", "dividend_fy_end",
        "dividend_annual", "payout_ratio",
        "forecast_eps", "forecast_eps_basis_factor", "initial_forecast_eps",
        "forecast_dividend_annual", "forecast_payout_ratio",
        "shares_outstanding", "treasury_stock", "avg_shares",
        "total_assets", "equity", "equity_ratio",
        "source", "updated_at",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(
        # initial_forecast_eps は COALESCE: NULLで既存値を消さない
        (
            "initial_forecast_eps = COALESCE(excluded.initial_forecast_eps, "
            "per_share_data.initial_forecast_eps)"
            if c == "initial_forecast_eps"
            else f"{c} = excluded.{c}"
        )
        for c in cols
        if c not in ("ticker", "period", "quarter")
    )
    sql_main = f"""
        INSERT INTO per_share_data ({', '.join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(ticker, period, quarter)
        DO UPDATE SET {updates}
    """

    # 翌期予想行 (source=jquants_nxf): forecast_eps / initial_forecast_eps のみ更新
    # 実績EPS (eps) がある行は絶対に上書きしない。
    # initial_forecast_eps は「期初予想」なので一度書いたら原則上書きしない (COALESCE)。
    _NXF_COLS = [
        "ticker", "period", "quarter", "disclosed_date",
        "forecast_eps", "forecast_eps_basis_factor", "initial_forecast_eps",
        "forecast_dividend_annual", "forecast_payout_ratio",
        "source", "updated_at",
    ]
    sql_nxf = """
        INSERT INTO per_share_data (ticker, period, quarter, disclosed_date,
            forecast_eps, forecast_eps_basis_factor, initial_forecast_eps,
            forecast_dividend_annual, forecast_payout_ratio,
            source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, period, quarter)
        DO UPDATE SET
            forecast_eps = CASE
                WHEN per_share_data.eps IS NOT NULL THEN per_share_data.forecast_eps
                ELSE excluded.forecast_eps
            END,
            forecast_eps_basis_factor = CASE
                WHEN per_share_data.eps IS NOT NULL THEN per_share_data.forecast_eps_basis_factor
                ELSE excluded.forecast_eps_basis_factor
            END,
            initial_forecast_eps = COALESCE(
                per_share_data.initial_forecast_eps,
                excluded.initial_forecast_eps
            ),
            forecast_dividend_annual = CASE
                WHEN per_share_data.eps IS NOT NULL THEN per_share_data.forecast_dividend_annual
                ELSE excluded.forecast_dividend_annual
            END,
            forecast_payout_ratio = CASE
                WHEN per_share_data.eps IS NOT NULL THEN per_share_data.forecast_payout_ratio
                ELSE excluded.forecast_payout_ratio
            END,
            updated_at = excluded.updated_at
    """

    now_iso = datetime.now(JST).isoformat()
    main_batch: list[tuple] = []
    nxf_batch: list[tuple] = []

    for rec in best.values():
        rec["updated_at"] = now_iso
        if rec.get("source") == _NXF_SOURCE:
            nxf_batch.append(tuple(rec.get(c) for c in _NXF_COLS))
        else:
            main_batch.append(tuple(rec.get(c) for c in cols))

    try:
        if main_batch:
            conn.executemany(sql_main, main_batch)
        if nxf_batch:
            conn.executemany(sql_nxf, nxf_batch)
        conn.commit()
        stats["upserted"] = len(main_batch)
        stats["nxf_upserted"] = len(nxf_batch)
        logger.info(f"[UPSERT] 通常行={len(main_batch):,}  翌期予想行={len(nxf_batch):,}")
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
    parser.add_argument("--db", default=str(runtime_path(_DEFAULT_DB)), help="SQLite DB パス")
    parser.add_argument("--apply", action="store_true", help="本番反映")
    parser.add_argument("--dry-run", action="store_true", help="ドライラン (デフォルト)")
    parser.add_argument("--limit", type=int, default=0, help="処理行数上限")
    parser.add_argument("--ticker", help="対象銘柄だけを再構築（例: 7480）")
    args = parser.parse_args()
    args.db = str(runtime_path(args.db))

    os.makedirs(str(runtime_path(_LOG_DIR)), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    mode = "apply" if args.apply else "dryrun"
    log_file = os.path.join(str(runtime_path(_LOG_DIR)), f"extract_per_share_{mode}_{ts}.log")

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
    logger.info(f"  ticker: {args.ticker or '全銘柄'}")

    stats = run(
        args.db,
        dry_run=is_dry_run,
        limit=args.limit,
        ticker=args.ticker,
    )

    logger.info(f"\n{'='*60}")
    logger.info(f"  完了")
    logger.info(f"  raw行数     : {stats['total_raw']:,}")
    logger.info(f"  抽出成功    : {stats['extracted']:,}")
    logger.info(f"  UPSERT(通常): {stats['upserted']:,}")
    logger.info(f"  UPSERT(NxF) : {stats['nxf_upserted']:,}")
    logger.info(f"  スキップ    : {stats['skipped']:,}")
    logger.info(f"  エラー      : {stats['errors']}")
    logger.info(f"{'='*60}")

    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
