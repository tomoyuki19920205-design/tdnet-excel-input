"""Build the Company Viewer stock-screener snapshot from canonical local data.

The builder is deliberately side-effect free: it opens SQLite in read-only
mode and returns one row per current ordinary stock plus canonical forecast
revision events. Publishing is handled by ``tools/sync_screener_snapshot.py``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

from src.common_ticker import normalize_ticker


JST = timezone.utc  # timestamps are serialized with an explicit offset by callers
FORECAST_METRICS = ("sales", "operating_profit", "ordinary_profit", "net_income", "eps")
REVISION_WINDOW_DAYS = 365 * 3


def _number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        return parsed if math.isfinite(parsed) else None
    return None


def _period_days(start: str | None, end: str | None) -> int | None:
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
    except (TypeError, ValueError):
        return None


def _add_year(period: str | None, years: int = 1) -> str | None:
    try:
        parsed = date.fromisoformat(str(period))
        return parsed.replace(year=parsed.year + years).isoformat()
    except (TypeError, ValueError):
        return None


def _calendar_days(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def _pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def _reason(value: float | bool | None, reason: str | None) -> str | None:
    return None if value is not None else (reason or "missing")


def _latest(rows: Iterable[dict[str, Any]], *order_keys: str) -> dict[str, Any] | None:
    candidates = list(rows)
    if not candidates:
        return None
    return max(candidates, key=lambda row: tuple(str(row.get(key) or "") for key in order_keys))


@dataclass(frozen=True)
class SnapshotBuild:
    batch_id: str
    universe_date: str
    rows: list[dict[str, Any]]
    revision_events: list[dict[str, Any]]
    coverage: dict[str, dict[str, int | float]]
    null_reasons: dict[str, dict[str, int]]


class ScreenerSnapshotBuilder:
    def __init__(self, db_path: str | Path, *, as_of: str | None = None) -> None:
        self.db_path = Path(db_path).resolve()
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.as_of = as_of

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ScreenerSnapshotBuilder":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def build(self, *, batch_id: str | None = None) -> SnapshotBuild:
        universe_date = self.as_of or self.connection.execute(
            "SELECT MAX(date) FROM market_data_universe"
        ).fetchone()[0]
        universe = self._load_universe(universe_date)
        prices, actions, sessions = self._load_prices(set(universe))
        per_share = self._load_per_share(set(universe))
        actuals, forecasts, forecast_points = self._load_financials(set(universe))
        revision_events = self._build_revision_events(forecast_points, actions, universe_date)
        revision_counts = self._revision_counts(revision_events, forecast_points, universe_date)
        rows = [
            self._build_ticker_row(
                ticker=ticker,
                master=universe[ticker],
                universe_date=universe_date,
                prices=prices.get(ticker, []),
                actions=actions.get(ticker, []),
                sessions=sessions,
                per_share=per_share.get(ticker, []),
                actuals=actuals.get(ticker, {}),
                forecasts=forecasts.get(ticker, {}),
                revision_counts=revision_counts.get(ticker),
                batch_id=batch_id or "",
            )
            for ticker in sorted(universe)
        ]
        resolved_batch_id = batch_id or f"{universe_date}-{uuid4()}"
        calculated_at = datetime.now(timezone.utc).isoformat()
        for row in rows:
            row["batch_id"] = resolved_batch_id
            row["calculated_at"] = calculated_at
        for event in revision_events:
            event["batch_id"] = resolved_batch_id
        return SnapshotBuild(
            batch_id=resolved_batch_id,
            universe_date=universe_date,
            rows=rows,
            revision_events=revision_events,
            coverage=self._coverage(rows),
            null_reasons=self._null_reasons(rows),
        )

    def _load_universe(self, universe_date: str) -> dict[str, dict[str, Any]]:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(market_data_universe)")}
        optional = [
            name for name in (
                "sector17_code", "sector17_name", "sector33_code", "sector33_name",
                "market_name", "fetched_at",
            ) if name in columns
        ]
        select = "ticker,company_name,market_code,is_jquants_price_eligible"
        if optional:
            select += "," + ",".join(optional)
        result: dict[str, dict[str, Any]] = {}
        for raw in self.connection.execute(
            f"SELECT {select} FROM market_data_universe WHERE date=? AND is_ordinary_stock=1",
            (universe_date,),
        ):
            row = dict(raw)
            for name in ("sector17_code", "sector17_name", "sector33_code", "sector33_name", "market_name"):
                row.setdefault(name, None)
            result[str(row["ticker"])] = row
        return result

    def _load_prices(
        self, universe: set[str]
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[tuple[str, float]]], list[str]]:
        prices: dict[str, list[dict[str, Any]]] = defaultdict(list)
        actions: dict[str, list[tuple[str, float]]] = defaultdict(list)
        sessions: set[str] = set()
        for raw in self.connection.execute(
            "SELECT ticker,date,open,high,low,close,volume,turnover,adj_factor,adj_close,"
            "market_cap,fetched_at FROM market_data ORDER BY ticker,date"
        ):
            ticker = str(raw["ticker"])
            if ticker not in universe:
                continue
            row = dict(raw)
            prices[ticker].append(row)
            sessions.add(str(row["date"]))
            factor = row["adj_factor"]
            if factor not in (None, 1, 1.0):
                actions[ticker].append((str(row["date"]), float(factor)))
        return prices, actions, sorted(sessions)

    def _load_per_share(self, universe: set[str]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in self.connection.execute("SELECT * FROM per_share_data ORDER BY ticker,period,disclosed_date"):
            ticker = str(raw["ticker"])
            if ticker in universe:
                result[ticker].append(dict(raw))
        return result

    def _load_financials(
        self, universe: set[str]
    ) -> tuple[
        dict[str, dict[tuple[str, str], dict[str, Any]]],
        dict[str, dict[tuple[str, str], dict[str, Any]]],
        dict[tuple[str, str, str], list[dict[str, Any]]],
    ]:
        actuals: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
        forecast_points: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for (raw_json,) in self.connection.execute(
            "SELECT raw_json FROM jquants_financials_normalized WHERE raw_json IS NOT NULL"
        ):
            try:
                raw = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError):
                continue
            ticker = normalize_ticker(raw.get("Code") or raw.get("LocalCode") or "")
            if ticker not in universe:
                continue
            disclosed = str(raw.get("DiscDate") or "")
            disclosed_time = str(raw.get("DiscTime") or "")
            disclosure_id = str(raw.get("DiscNo") or f"{ticker}:{disclosed}:{disclosed_time}")
            order_key = f"{disclosed} {disclosed_time} {disclosure_id}"
            document_type = str(raw.get("DocType") or "")
            quarter = str(raw.get("CurPerType") or "").upper()
            fiscal_year_end = str(raw.get("CurFYEn") or "")
            if (
                fiscal_year_end
                and quarter in {"1Q", "2Q", "3Q", "FY"}
                and "FinancialStatements" in document_type
                and "ForecastRevision" not in document_type
            ):
                actual = {
                    "ticker": ticker,
                    "period": fiscal_year_end,
                    "quarter": quarter,
                    "sales": _number(raw, "Sales", "NCSales"),
                    "operating_profit": _number(raw, "OP", "NCOP"),
                    "ordinary_profit": _number(raw, "OdP", "NCOdP"),
                    "net_income": _number(raw, "NP", "NCNP"),
                    "disclosed_at": disclosed,
                    "order_key": order_key,
                    "period_days": _period_days(raw.get("CurPerSt"), raw.get("CurPerEn")),
                    "accounting_standard": self._accounting_standard(document_type),
                }
                key = (fiscal_year_end, quarter)
                previous = actuals[ticker].get(key)
                if previous is None or order_key >= previous["order_key"]:
                    actuals[ticker][key] = actual

            def append_forecast(target: str | None, prefix: str) -> None:
                if not target:
                    return
                key_map = {
                    "sales": (f"{prefix}Sales", f"{prefix}NCSales"),
                    "operating_profit": (f"{prefix}OP", f"{prefix}NCOP"),
                    "ordinary_profit": (f"{prefix}OdP", f"{prefix}NCOdP"),
                    "net_income": (f"{prefix}NP", f"{prefix}NCNP"),
                    "eps": (f"{prefix}EPS",),
                }
                for metric, keys in key_map.items():
                    value = _number(raw, *keys)
                    if value is None:
                        continue
                    forecast_points[(ticker, target, metric)].append({
                        "ticker": ticker,
                        "target_fiscal_year": target,
                        "metric": metric,
                        "value": value,
                        "disclosed_at": disclosed,
                        "disclosure_id": disclosure_id,
                        "document_type": document_type,
                        "order_key": order_key,
                        "is_correction": bool(raw.get("RetroRst")),
                    })

            append_forecast(fiscal_year_end or None, "F")
            next_fye = str(raw.get("NxtFYEn") or "")
            if not next_fye and quarter == "FY":
                next_fye = _add_year(fiscal_year_end) or ""
            append_forecast(next_fye or None, "NxF")

        forecasts: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
        for (ticker, target, metric), points in forecast_points.items():
            deduped = {
                (point["disclosed_at"], point["disclosure_id"]): point for point in points
            }
            latest = max(deduped.values(), key=lambda point: point["order_key"])
            forecasts[ticker][(target, metric)] = latest
            forecast_points[(ticker, target, metric)] = sorted(
                deduped.values(), key=lambda point: point["order_key"]
            )
        return actuals, forecasts, forecast_points

    @staticmethod
    def _accounting_standard(document_type: str) -> str:
        if "IFRS" in document_type:
            return "IFRS"
        if "_US" in document_type:
            return "US_GAAP"
        if "Foreign" in document_type:
            return "FOREIGN"
        return "JP_GAAP"

    @staticmethod
    def _action_factor(
        actions: list[tuple[str, float]], disclosed_at: str | None, price_date: str
    ) -> float:
        if not disclosed_at:
            return 1.0
        factor = 1.0
        for action_date, value in actions:
            if disclosed_at < action_date <= price_date:
                factor *= value
        return factor

    def _normalized_per_share(
        self,
        value: float | None,
        disclosed_at: str | None,
        price_date: str,
        actions: list[tuple[str, float]],
    ) -> float | None:
        if value is None:
            return None
        return value * self._action_factor(actions, disclosed_at, price_date)

    def _build_revision_events(
        self,
        points: dict[tuple[str, str, str], list[dict[str, Any]]],
        actions: dict[str, list[tuple[str, float]]],
        universe_date: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for (ticker, target, metric), history in points.items():
            previous: dict[str, Any] | None = None
            for point in history:
                value = point["value"]
                if previous is None:
                    direction = "initial"
                    previous_value = None
                else:
                    previous_value = previous["value"]
                    direction = "upward" if value > previous_value else (
                        "downward" if value < previous_value else "unchanged"
                    )
                split_only = False
                if metric == "eps" and previous is not None and previous_value not in (None, 0):
                    factor = self._action_factor(
                        actions.get(ticker, []), previous["disclosed_at"], point["disclosed_at"]
                    )
                    expected = previous_value * factor
                    split_only = factor != 1 and math.isclose(value, expected, rel_tol=0.015)
                if direction != "unchanged":
                    events.append({
                        "ticker": ticker,
                        "disclosure_id": point["disclosure_id"],
                        "disclosed_at": point["disclosed_at"],
                        "target_fiscal_year": target,
                        "metric": metric,
                        "previous_value": previous_value,
                        "revised_value": value,
                        "direction": direction,
                        "is_correction": point["is_correction"],
                        "is_split_only_change": split_only,
                        "source": "jquants",
                    })
                previous = point
        return events

    @staticmethod
    def _revision_counts(
        events: list[dict[str, Any]],
        points: dict[tuple[str, str, str], list[dict[str, Any]]],
        universe_date: str,
    ) -> dict[str, tuple[int, int]]:
        cutoff = (date.fromisoformat(universe_date).replace(year=date.fromisoformat(universe_date).year - 3)).isoformat()
        op_counts: Counter[str] = Counter()
        any_disclosures: dict[str, set[str]] = defaultdict(set)
        history_available = {
            ticker
            for (ticker, _target, _metric), history in points.items()
            if any(point["disclosed_at"] >= cutoff for point in history)
        }
        for event in events:
            if event["disclosed_at"] < cutoff:
                continue
            history_available.add(event["ticker"])
            if (
                event["direction"] != "upward"
                or event["is_correction"]
                or event["is_split_only_change"]
            ):
                continue
            ticker = event["ticker"]
            if event["metric"] == "operating_profit":
                op_counts[ticker] += 1
            if event["metric"] in FORECAST_METRICS:
                any_disclosures[ticker].add(event["disclosure_id"])
        return {
            ticker: (op_counts[ticker], len(any_disclosures[ticker]))
            for ticker in history_available
        }

    def _build_ticker_row(
        self,
        *,
        ticker: str,
        master: dict[str, Any],
        universe_date: str,
        prices: list[dict[str, Any]],
        actions: list[tuple[str, float]],
        sessions: list[str],
        per_share: list[dict[str, Any]],
        actuals: dict[tuple[str, str], dict[str, Any]],
        forecasts: dict[tuple[str, str], dict[str, Any]],
        revision_counts: tuple[int, int] | None,
        batch_id: str,
    ) -> dict[str, Any]:
        price = prices[-1] if prices else None
        price_as_of = str(price["date"]) if price else None
        current_price = float(price["close"]) if price and price["close"] is not None else None
        stale_sessions = (
            sum(price_as_of < session <= universe_date for session in sessions)
            if price_as_of else None
        )
        stale_calendar_days = _calendar_days(price_as_of, universe_date) if price_as_of else None
        if price_as_of == universe_date:
            price_status = "current"
        elif price is not None and master.get("is_jquants_price_eligible"):
            quarantined = self.connection.execute(
                "SELECT 1 FROM market_data_quarantine WHERE ticker=? AND date=? LIMIT 1",
                (ticker, universe_date),
            ).fetchone()
            price_status = "no_trade" if quarantined else "stale_unknown"
        elif not master.get("is_jquants_price_eligible"):
            price_status = "source_ineligible"
        else:
            price_status = "missing"
        price_missing = current_price is None

        fy_rows = sorted(
            (row for (period, quarter), row in actuals.items() if quarter == "FY" and period <= universe_date),
            key=lambda row: row["period"],
        )
        completed = fy_rows[-1] if fy_rows else None
        prior_completed = fy_rows[-2] if len(fy_rows) >= 2 else None
        completed_period = completed["period"] if completed else None
        target_period = _add_year(completed_period)
        fiscal_period_changed = False
        if completed:
            duration = completed.get("period_days")
            fiscal_period_changed = duration is not None and not 330 <= duration <= 400
        if completed and prior_completed:
            left = completed.get("period_days")
            right = prior_completed.get("period_days")
            if left is not None and right is not None and abs(left - right) > 31:
                fiscal_period_changed = True

        sales_forecast = forecasts.get((target_period, "sales")) if target_period else None
        op_forecast = forecasts.get((target_period, "operating_profit")) if target_period else None
        future_targets = sorted({period for period, metric in forecasts if metric == "sales" and (not completed_period or period > completed_period)})
        target_mismatch = bool(target_period and future_targets and target_period not in future_targets)

        actual_ps = _latest(
            (
                row for row in per_share
                if row.get("quarter") == "FY"
                and row.get("eps") is not None
                and str(row.get("period") or "") <= universe_date
            ),
            "period", "disclosed_date",
        )
        valuation_completed_period = actual_ps.get("period") if actual_ps else None
        valuation_target_period = _add_year(valuation_completed_period)
        forecast_ps = _latest(
            (row for row in per_share if row.get("period") == valuation_target_period),
            "disclosed_date", "quarter",
        ) if valuation_target_period else None
        valuation_target_mismatch = bool(
            target_period and valuation_target_period and target_period != valuation_target_period
        )

        forecast_eps = forecast_ps.get("forecast_eps") if forecast_ps else None
        actual_eps = actual_ps.get("eps") if actual_ps else None
        normalized_forecast_eps = self._normalized_per_share(
            forecast_eps,
            forecast_ps.get("disclosed_date") if forecast_ps else None,
            price_as_of or universe_date,
            actions,
        )
        normalized_actual_eps = self._normalized_per_share(
            actual_eps,
            actual_ps.get("disclosed_date") if actual_ps else None,
            price_as_of or universe_date,
            actions,
        )

        forward_per_reason = None
        if price_missing:
            forward_per_reason = "price_missing"
        elif forecast_ps is None or forecast_eps is None:
            forward_per_reason = "forecast_missing"
        elif forecast_eps <= 0:
            forward_per_reason = "forecast_loss"
        elif valuation_target_mismatch:
            forward_per_reason = "target_fy_mismatch"
        forward_per = (
            current_price / normalized_forecast_eps
            if forward_per_reason is None and normalized_forecast_eps and normalized_forecast_eps > 0
            else None
        )

        actual_per_reason = None
        if price_missing:
            actual_per_reason = "price_missing"
        elif actual_ps is None or actual_eps is None:
            actual_per_reason = "actual_missing"
        elif actual_eps <= 0:
            actual_per_reason = "prior_loss"
        actual_per = (
            current_price / normalized_actual_eps
            if actual_per_reason is None and normalized_actual_eps and normalized_actual_eps > 0
            else None
        )

        actual_dividend = actual_ps.get("dividend_annual") if actual_ps else None
        forecast_dividend = forecast_ps.get("forecast_dividend_annual") if forecast_ps else None
        normalized_actual_dividend = self._normalized_per_share(
            actual_dividend, actual_ps.get("disclosed_date") if actual_ps else None,
            price_as_of or universe_date, actions,
        )
        normalized_forecast_dividend = self._normalized_per_share(
            forecast_dividend, forecast_ps.get("disclosed_date") if forecast_ps else None,
            price_as_of or universe_date, actions,
        )
        actual_dividend_yield = (
            normalized_actual_dividend / current_price * 100
            if current_price and normalized_actual_dividend is not None and normalized_actual_dividend >= 0
            else None
        )
        forecast_dividend_yield = (
            normalized_forecast_dividend / current_price * 100
            if current_price and normalized_forecast_dividend is not None and normalized_forecast_dividend >= 0
            else None
        )

        actual_sales_growth = _pct(
            completed.get("sales") if completed else None,
            prior_completed.get("sales") if prior_completed else None,
        )
        forecast_sales_growth = _pct(
            sales_forecast.get("value") if sales_forecast else None,
            completed.get("sales") if completed else None,
        )
        if fiscal_period_changed or target_mismatch:
            actual_sales_growth = None if fiscal_period_changed else actual_sales_growth
            forecast_sales_growth = None

        sales_valuation_reason = None
        if forward_per is None:
            sales_valuation_reason = forward_per_reason
        elif forward_per <= 0:
            sales_valuation_reason = "nonpositive_forward_per"
        elif completed is None or completed.get("sales") in (None, 0) or completed["sales"] < 0:
            sales_valuation_reason = "previous_fy_sales_missing_or_nonpositive"
        elif fiscal_period_changed:
            sales_valuation_reason = "fiscal_period_changed"
        elif target_mismatch or valuation_target_mismatch:
            sales_valuation_reason = "target_fy_mismatch"
        elif sales_forecast is None:
            sales_valuation_reason = "forecast_sales_missing"
        elif forecast_sales_growth is None or forecast_sales_growth <= 0:
            sales_valuation_reason = "nonpositive_forecast_sales_growth"
        forward_per_per_sales_growth = (
            forward_per / forecast_sales_growth
            if sales_valuation_reason is None and forecast_sales_growth and forward_per
            else None
        )

        eps_growth = (
            _pct(normalized_forecast_eps, normalized_actual_eps)
            if normalized_actual_eps is not None and normalized_actual_eps > 0
            else None
        )
        peg_reason = None
        if price_missing:
            peg_reason = "price_missing"
        elif actual_eps is None:
            peg_reason = "actual_missing"
        elif actual_eps <= 0:
            peg_reason = "prior_loss"
        elif fiscal_period_changed:
            peg_reason = "fiscal_period_changed"
        elif target_mismatch or valuation_target_mismatch:
            peg_reason = "target_fy_mismatch"
        elif forecast_eps is None:
            peg_reason = "forecast_missing"
        elif forecast_eps <= 0:
            peg_reason = "forecast_loss"
        elif eps_growth is None or eps_growth <= 0:
            peg_reason = "negative_eps_growth"
        forward_peg = forward_per / eps_growth if peg_reason is None and forward_per and eps_growth else None

        latest_interim = _latest(
            (
                row for (period, quarter), row in actuals.items()
                if period == target_period and quarter in {"1Q", "2Q", "3Q"}
            ),
            "disclosed_at", "quarter",
        ) if target_period else None
        previous_interim = None
        if latest_interim and completed_period:
            previous_interim = actuals.get((completed_period, latest_interim["quarter"]))
        cumulative_sales_growth = _pct(
            latest_interim.get("sales") if latest_interim else None,
            previous_interim.get("sales") if previous_interim else None,
        )
        cumulative_op_growth = _pct(
            latest_interim.get("operating_profit") if latest_interim else None,
            previous_interim.get("operating_profit") if previous_interim else None,
        )
        forecast_op_growth = _pct(
            op_forecast.get("value") if op_forecast else None,
            completed.get("operating_profit") if completed else None,
        )
        prior_interim_op = previous_interim.get("operating_profit") if previous_interim else None
        current_interim_op = latest_interim.get("operating_profit") if latest_interim else None
        growth_rate_not_meaningful = prior_interim_op is not None and prior_interim_op <= 0
        turnaround = bool(prior_interim_op is not None and prior_interim_op < 0 and current_interim_op is not None and current_interim_op > 0)
        loss_expansion = bool(prior_interim_op is not None and prior_interim_op < 0 and current_interim_op is not None and current_interim_op < prior_interim_op)
        profit_to_loss = bool(prior_interim_op is not None and prior_interim_op > 0 and current_interim_op is not None and current_interim_op < 0)
        if fiscal_period_changed:
            cumulative_sales_growth = None
            cumulative_op_growth = None
        if growth_rate_not_meaningful:
            cumulative_op_growth = None

        candles5 = self._candle_ratio(prices, 5)
        candles10 = self._candle_ratio(prices, 10)
        return5 = self._return(prices, 5)
        return20 = self._return(prices, 20)
        return60 = self._return(prices, 60)
        psychological5 = self._psychological(prices, 5)
        psychological10 = self._psychological(prices, 10)
        ytd_high = self._new_ytd_high(prices, actions, universe_date)
        insufficient_history = len(prices) < 61

        equity_row = _latest(
            (row for row in per_share if row.get("equity_ratio") is not None),
            "disclosed_date", "period",
        )
        equity_ratio = equity_row.get("equity_ratio") if equity_row else None
        if equity_ratio is not None and abs(equity_ratio) <= 1.5:
            equity_ratio *= 100

        financial_dates = [
            row.get("disclosed_at") for row in (completed, latest_interim) if row and row.get("disclosed_at")
        ]
        forecast_dates = [
            row.get("disclosed_at") for row in (sales_forecast, op_forecast, forecast_ps) if row and row.get("disclosed_at")
        ]
        metric_reasons = {
            "forward_per": forward_per_reason,
            "actual_per": actual_per_reason,
            "actual_dividend_yield_pct": _reason(actual_dividend_yield, "dividend_missing" if not price_missing else "price_missing"),
            "forecast_dividend_yield_pct": _reason(forecast_dividend_yield, "forecast_missing" if not price_missing else "price_missing"),
            "actual_sales_growth_yoy_pct": _reason(actual_sales_growth, "fiscal_period_changed" if fiscal_period_changed else "prior_actual_missing"),
            "forecast_sales_growth_yoy_pct": _reason(forecast_sales_growth, "target_fy_mismatch" if target_mismatch else "forecast_missing"),
            "equity_ratio_pct": _reason(equity_ratio, "financial_missing"),
            "bullish_candle_ratio_5d_pct": None if candles5[0] is not None else "insufficient_price_history",
            "bullish_candle_ratio_10d_pct": None if candles10[0] is not None else "insufficient_price_history",
            "bearish_candle_ratio_5d_pct": None if candles5[1] is not None else "insufficient_price_history",
            "bearish_candle_ratio_10d_pct": None if candles10[1] is not None else "insufficient_price_history",
            "new_ytd_high_last_5d": None if ytd_high is not None else "insufficient_price_history",
            "return_5d_pct": None if return5 is not None else "insufficient_price_history",
            "return_20d_pct": None if return20 is not None else "insufficient_price_history",
            "return_60d_pct": None if return60 is not None else "insufficient_price_history",
            "sales_growth_beat_pp": None if (
                cumulative_sales_growth is not None and forecast_sales_growth is not None
            ) else ("forecast_missing" if forecast_sales_growth is None else "same_quarter_missing"),
            "operating_profit_growth_beat_pp": None if (
                cumulative_op_growth is not None and forecast_op_growth is not None
            ) else ("growth_rate_not_meaningful" if growth_rate_not_meaningful else (
                "forecast_missing" if forecast_op_growth is None else "same_quarter_missing"
            )),
            "op_upward_revision_count_3y": None if revision_counts else "revision_history_missing",
            "any_earnings_upward_revision_event_count_3y": None if revision_counts else "revision_history_missing",
            "market_cap": _reason(price.get("market_cap") if price else None, "price_or_shares_missing"),
            "sector17_code": _reason(master.get("sector17_code"), "master_missing"),
            "sector33_code": _reason(master.get("sector33_code"), "master_missing"),
            "market_code": _reason(master.get("market_code"), "master_missing"),
            "psychological_line_5d_pct": None if psychological5 is not None else "insufficient_price_history",
            "psychological_line_10d_pct": None if psychological10 is not None else "insufficient_price_history",
            "forward_per_per_forecast_sales_growth": sales_valuation_reason,
            "forward_peg": peg_reason,
        }
        return {
            "batch_id": batch_id,
            "ticker": ticker,
            "company_name": master.get("company_name"),
            "universe_date": universe_date,
            "price_as_of": price_as_of,
            "financial_as_of": max(financial_dates) if financial_dates else None,
            "forecast_as_of": max(forecast_dates) if forecast_dates else None,
            "master_as_of": master.get("fetched_at") or universe_date,
            "calculated_at": None,
            "price_status": price_status,
            "price_stale_sessions": stale_sessions,
            "price_stale_calendar_days": stale_calendar_days,
            "latest_valid_price": current_price,
            "market_cap": price.get("market_cap") if price else None,
            "market_code": master.get("market_code"),
            "market_name": master.get("market_name"),
            "sector17_code": master.get("sector17_code"),
            "sector17_name": master.get("sector17_name"),
            "sector33_code": master.get("sector33_code"),
            "sector33_name": master.get("sector33_name"),
            "accounting_standard": completed.get("accounting_standard") if completed else None,
            "forward_per": forward_per,
            "actual_per": actual_per,
            "actual_dividend_yield_pct": actual_dividend_yield,
            "forecast_dividend_yield_pct": forecast_dividend_yield,
            "actual_sales_growth_yoy_pct": actual_sales_growth,
            "forecast_sales_growth_yoy_pct": forecast_sales_growth,
            "equity_ratio_pct": equity_ratio,
            "bullish_candle_ratio_5d_pct": candles5[0],
            "bullish_candle_ratio_10d_pct": candles10[0],
            "bearish_candle_ratio_5d_pct": candles5[1],
            "bearish_candle_ratio_10d_pct": candles10[1],
            "new_ytd_high_last_5d": ytd_high,
            "return_5d_pct": return5,
            "return_20d_pct": return20,
            "return_60d_pct": return60,
            "sales_growth_beat_pp": (
                cumulative_sales_growth - forecast_sales_growth
                if cumulative_sales_growth is not None and forecast_sales_growth is not None else None
            ),
            "operating_profit_growth_beat_pp": (
                cumulative_op_growth - forecast_op_growth
                if cumulative_op_growth is not None and forecast_op_growth is not None else None
            ),
            "op_upward_revision_count_3y": revision_counts[0] if revision_counts else None,
            "any_earnings_upward_revision_event_count_3y": revision_counts[1] if revision_counts else None,
            "psychological_line_5d_pct": psychological5,
            "psychological_line_10d_pct": psychological10,
            "forward_per_per_forecast_sales_growth": forward_per_per_sales_growth,
            "forecast_eps_growth_yoy_pct": eps_growth,
            "forward_peg": forward_peg,
            "peg_denominator_small": bool(eps_growth is not None and 0 < eps_growth < 1),
            "fiscal_period_changed": fiscal_period_changed,
            "growth_rate_not_meaningful": growth_rate_not_meaningful,
            "turnaround": turnaround,
            "loss_expansion": loss_expansion,
            "profit_to_loss": profit_to_loss,
            "forecast_missing": sales_forecast is None or forecast_ps is None,
            "price_missing": price_missing,
            "insufficient_price_history": insufficient_history,
            "metric_reasons": metric_reasons,
        }

    @staticmethod
    def _candle_ratio(prices: list[dict[str, Any]], periods: int) -> tuple[float | None, float | None]:
        observations = [row for row in prices if row.get("open") is not None and row.get("close") is not None]
        if len(observations) < periods:
            return None, None
        window = observations[-periods:]
        bullish = sum(row["close"] > row["open"] for row in window) / periods * 100
        bearish = sum(row["close"] < row["open"] for row in window) / periods * 100
        return bullish, bearish

    @staticmethod
    def _return(prices: list[dict[str, Any]], periods: int) -> float | None:
        observations = [row for row in prices if row.get("adj_close") is not None]
        if len(observations) < periods + 1 or observations[-periods - 1]["adj_close"] == 0:
            return None
        return (observations[-1]["adj_close"] / observations[-periods - 1]["adj_close"] - 1) * 100

    @staticmethod
    def _psychological(prices: list[dict[str, Any]], periods: int) -> float | None:
        observations = [row for row in prices if row.get("adj_close") is not None]
        if len(observations) < periods + 1:
            return None
        window = observations[-periods - 1:]
        return sum(window[index]["adj_close"] > window[index - 1]["adj_close"] for index in range(1, len(window))) / periods * 100

    @staticmethod
    def _new_ytd_high(
        prices: list[dict[str, Any]], actions: list[tuple[str, float]], universe_date: str
    ) -> bool | None:
        year_start = f"{universe_date[:4]}-01-01"
        observations = [row for row in prices if row["date"] >= year_start and row.get("high") is not None]
        if len(observations) < 5:
            return None
        adjusted: list[float] = []
        for row in observations:
            factor = 1.0
            for action_date, action_factor in actions:
                if row["date"] < action_date <= universe_date:
                    factor *= action_factor
            adjusted.append(float(row["high"]) * factor)
        running: float | None = None
        hit = False
        boundary = len(adjusted) - 5
        for index, value in enumerate(adjusted):
            if index >= boundary and (running is None or value > running):
                hit = True
            running = value if running is None else max(running, value)
        return hit

    @staticmethod
    def _coverage(rows: list[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
        metrics = (
            "forward_per", "actual_per", "actual_dividend_yield_pct",
            "forecast_dividend_yield_pct", "actual_sales_growth_yoy_pct",
            "forecast_sales_growth_yoy_pct", "equity_ratio_pct",
            "bullish_candle_ratio_5d_pct", "bullish_candle_ratio_10d_pct",
            "bearish_candle_ratio_5d_pct", "bearish_candle_ratio_10d_pct",
            "new_ytd_high_last_5d", "return_5d_pct", "return_20d_pct",
            "return_60d_pct", "sales_growth_beat_pp",
            "operating_profit_growth_beat_pp", "op_upward_revision_count_3y",
            "any_earnings_upward_revision_event_count_3y", "market_cap",
            "sector17_code", "sector33_code", "market_code",
            "psychological_line_5d_pct", "psychological_line_10d_pct",
            "forward_per_per_forecast_sales_growth", "forward_peg",
        )
        universe = len(rows)
        result: dict[str, dict[str, int | float]] = {}
        for metric in metrics:
            numeric = sum(row.get(metric) is not None for row in rows)
            result[metric] = {
                "numeric": numeric,
                "null": universe - numeric,
                "universe": universe,
                "coverage_pct": round(numeric / universe * 100, 2) if universe else 0,
            }
        return result

    @staticmethod
    def _null_reasons(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        counters: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            for metric, reason in row.get("metric_reasons", {}).items():
                if row.get(metric) is None:
                    counters[metric][reason or "missing"] += 1
        return {metric: dict(sorted(counter.items())) for metric, counter in sorted(counters.items())}


def build_snapshot(db_path: str | Path, *, as_of: str | None = None) -> SnapshotBuild:
    with ScreenerSnapshotBuilder(db_path, as_of=as_of) as builder:
        return builder.build()
