"""Leakage-safe between-earnings market and valuation analysis.

The module is deliberately independent from alerting and candidate selection.  It
reads immutable daily market rows plus disclosure-dated per-share observations and
applies each observation from the next available trading day.  No result labels,
post-event reactions, or current fundamentals are backfilled into prior days.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable


CALCULATION_VERSION = "earnings_market_interval_v1"
FORMAL_DOCUMENT_TOKEN = "FinancialStatements"


@dataclass(frozen=True)
class DisclosureEvent:
    ticker: str
    disclosure_id: str
    disclosed_date: str
    disclosed_at: str | None
    fiscal_year_end: str
    quarter: str
    document_type: str
    correction_flag: bool = False


@dataclass(frozen=True)
class FundamentalInterval:
    ticker: str
    metric: str
    value: float | None
    status: str
    disclosure_id: str
    disclosed_date: str
    effective_trade_date: str
    source_adj_factor: float | None
    fiscal_year_end: str
    quarter: str
    source: str


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _std(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def next_trading_day(dates: list[str], disclosed_date: str) -> str | None:
    """First observed trading date strictly after a disclosure date."""
    return next((day for day in dates if day > disclosed_date), None)


def formal_events(rows: Iterable[dict[str, Any]], *, as_of_date: str) -> list[DisclosureEvent]:
    """Deduplicate formal result documents to economic reporting events.

    A correction is never used as a new boundary.  When a raw feed has multiple
    formal rows for the same fiscal period, the first non-correction publication
    is the primary event; ties are deterministically resolved by disclosure id.
    """
    candidates: list[DisclosureEvent] = []
    for row in rows:
        doc_type = str(row.get("type_of_document") or "")
        disclosed_date = str(row.get("disclosed_date") or "")
        if FORMAL_DOCUMENT_TOKEN not in doc_type or not disclosed_date or disclosed_date > as_of_date:
            continue
        raw = row.get("raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        title = str(raw.get("DocTitle") or raw.get("DocumentTitle") or "")
        correction = "Correction" in doc_type or "訂正" in title
        disc_no = str(raw.get("DiscNo") or row.get("disclosure_id") or "")
        if not disc_no:
            disc_no = hashlib.sha256(
                f"{row.get('ticker')}|{disclosed_date}|{row.get('fiscal_year_end')}|{row.get('quarter')}|{doc_type}".encode()
            ).hexdigest()
        disc_time = str(raw.get("DiscTime") or "")
        disclosed_at = f"{disclosed_date}T{disc_time}+09:00" if disc_time else None
        candidates.append(DisclosureEvent(
            ticker=str(row["ticker"]), disclosure_id=disc_no, disclosed_date=disclosed_date,
            disclosed_at=disclosed_at, fiscal_year_end=str(row["fiscal_year_end"]),
            quarter=str(row["quarter"]), document_type=doc_type, correction_flag=correction,
        ))
    primary: dict[tuple[str, str, str], DisclosureEvent] = {}
    for event in sorted(candidates, key=lambda x: (
        x.ticker, x.fiscal_year_end, x.quarter, x.correction_flag,
        "Consolidated" not in x.document_type, x.disclosed_date, x.disclosure_id,
    )):
        primary.setdefault((event.ticker, event.fiscal_year_end, event.quarter), event)
    return sorted(primary.values(), key=lambda x: (x.ticker, x.disclosed_date, x.disclosure_id))


def select_interval_boundaries(events: list[DisclosureEvent], dates: list[str], as_of_date: str) -> dict[str, Any] | None:
    """Return E2/E1 and the two date intervals, or None without two events."""
    if len(events) < 2:
        return None
    e2, e1 = events[-2], events[-1]
    a_start = next_trading_day(dates, e2.disclosed_date)
    b_start = next_trading_day(dates, e1.disclosed_date)
    a_end = next((day for day in reversed(dates) if day < e1.disclosed_date), None)
    b_end = next((day for day in reversed(dates) if day <= as_of_date), None)
    if not all((a_start, a_end, b_start, b_end)):
        return None
    return {
        "previous_previous_disclosure_id": e2.disclosure_id,
        "previous_disclosure_id": e1.disclosure_id,
        "previous_previous_disclosed_date": e2.disclosed_date,
        "previous_disclosed_date": e1.disclosed_date,
        "period_a": (a_start, a_end), "period_b": (b_start, b_end),
    }


def build_fundamental_intervals(
    ticker: str, rows: Iterable[dict[str, Any]], dates: list[str], disclosure_ids: dict[tuple[str, str, str], str], *, as_of_date: str,
    factor_by_date: dict[str, float | None],
) -> list[FundamentalInterval]:
    """Build dated forecast EPS/DPS and actual BPS observations from source rows."""
    observations: list[FundamentalInterval] = []
    # Same-day rows can contain an FY next-forecast row and an actual statement.
    # A deterministic source/quarter order makes their selection reproducible.
    priority = {"jquants": 0, "jquants_nxf": 1}
    for row in sorted(rows, key=lambda r: (str(r.get("disclosed_date") or ""), priority.get(str(r.get("source") or ""), 9), str(r.get("period") or ""), str(r.get("quarter") or ""))):
        disclosed = str(row.get("disclosed_date") or "")
        if not disclosed or disclosed > as_of_date:
            continue
        effective = next_trading_day(dates, disclosed)
        if effective is None:
            continue
        common = dict(
            ticker=ticker, disclosure_id=disclosure_ids.get(
                (disclosed, str(row.get("period") or ""), str(row.get("quarter") or "")),
                disclosure_ids.get((disclosed, "", ""), f"per_share:{ticker}:{disclosed}:{row.get('period')}:{row.get('quarter')}"),
            ),
            disclosed_date=disclosed, effective_trade_date=effective, source_adj_factor=factor_by_date.get(effective),
            fiscal_year_end=str(row.get("period") or ""), quarter=str(row.get("quarter") or ""), source=str(row.get("source") or "jquants"),
        )
        eps = _float(row.get("forecast_eps"))
        dps = _float(row.get("forecast_dividend_annual"))
        bps = _float(row.get("bps"))
        if eps is not None:
            observations.append(FundamentalInterval(metric="forecast_eps", value=eps, status="available" if eps > 0 else ("negative_eps" if eps < 0 else "zero_eps"), **common))
        if dps is not None:
            observations.append(FundamentalInterval(metric="forecast_annual_dps", value=dps, status="available", **common))
        if bps is not None:
            observations.append(FundamentalInterval(metric="actual_bps", value=bps, status="available" if bps > 0 else ("negative_bps" if bps < 0 else "zero_bps"), **common))
    # Multiple observations effective on one day: later source priority is not a
    # correction; prefer the actual current-statement row over generated NXF.
    best: dict[tuple[str, str], FundamentalInterval] = {}
    for item in observations:
        key = (item.metric, item.effective_trade_date)
        old = best.get(key)
        if old is None or (old.source == "jquants_nxf" and item.source != "jquants_nxf"):
            best[key] = item
    return sorted(best.values(), key=lambda x: (x.metric, x.effective_trade_date, x.disclosure_id))


def _asof_observation(observations: list[FundamentalInterval], metric: str, trade_date: str) -> FundamentalInterval | None:
    candidates = [x for x in observations if x.metric == metric and x.effective_trade_date <= trade_date]
    return candidates[-1] if candidates else None


def _adjusted_per_share(value: float | None, source_factor: float | None, current_factor: float | None) -> float | None:
    """Express a disclosed per-share value in the current unadjusted share basis.

    J-Quants adjustment factors map raw close to adjusted close.  Therefore an
    old per-share value is multiplied by old_factor/current_factor before it is
    paired with a raw close.  Missing/non-positive factors are intentionally not
    guessed.
    """
    if value is None:
        return None
    if source_factor is None or current_factor is None or source_factor <= 0 or current_factor <= 0:
        return value if source_factor == current_factor else None
    return value * source_factor / current_factor


def daily_valuations(market_rows: list[dict[str, Any]], observations: list[FundamentalInterval]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in market_rows:
        day = str(row["date"])
        close = _float(row.get("close")); factor = _float(row.get("adj_factor"))
        out = {key: row.get(key) for key in ("ticker", "date", "close", "adj_close", "volume", "adj_volume", "turnover", "adj_factor")}
        for metric, field, ratio_field, unavailable in (
            ("forecast_eps", "forecast_eps", "forward_per", "forecast_not_disclosed"),
            ("forecast_annual_dps", "forecast_annual_dps", "forward_dividend_yield", "dividend_not_disclosed"),
            ("actual_bps", "latest_bps", "pbr", "bps_not_disclosed"),
        ):
            item = _asof_observation(observations, metric, day)
            value = _adjusted_per_share(item.value, item.source_adj_factor, factor) if item else None
            status = item.status if item else unavailable
            out[field] = value
            out[f"{field}_status"] = status
            out[f"{field}_source_disclosure_id"] = item.disclosure_id if item else None
            ratio: float | None = None
            if close is not None and value is not None:
                if metric == "forecast_eps":
                    ratio = close / value if value > 0 else None
                elif metric == "forecast_annual_dps":
                    ratio = value / close if close > 0 else None
                else:
                    ratio = close / value if value > 0 else None
            out[ratio_field] = ratio
        result.append(out)
    return result


def _period_rows(rows: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    return [x for x in rows if start <= str(x["date"]) <= end]


def _ratio_stats(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [_float(x.get(field)) for x in rows]
    valid = [x for x in values if x is not None]
    return {
        f"{field}_start": valid[0] if valid else None, f"{field}_end": valid[-1] if valid else None,
        f"{field}_mean": _mean(valid), f"{field}_median": _median(valid), f"{field}_min": min(valid) if valid else None,
        f"{field}_max": max(valid) if valid else None, f"{field}_standard_deviation": _std(valid),
        f"{field}_valid_days": len(valid), f"{field}_coverage_ratio": len(valid) / len(rows) if rows else 0.0,
    }


def summarize_period(rows: list[dict[str, Any]], *, label: str, start: str, end: str) -> dict[str, Any]:
    prices = [_float(x.get("adj_close")) for x in rows]
    prices = [x for x in prices if x is not None and x > 0]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    simple_returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    running = -math.inf; drawdowns: list[float] = []
    for price in prices:
        running = max(running, price); drawdowns.append(price / running - 1)
    volumes = [_float(x.get("volume")) for x in rows]; adj_volumes = [_float(x.get("adj_volume")) for x in rows]; turnover = [_float(x.get("turnover")) for x in rows]
    valid_volume = [x for x in volumes if x is not None]; valid_adj_volume = [x for x in adj_volumes if x is not None]; valid_turnover = [x for x in turnover if x is not None]
    lo, hi = (min(prices), max(prices)) if prices else (None, None)
    position = None if lo is None or hi is None or hi == lo else (prices[-1] - lo) / (hi - lo)
    result: dict[str, Any] = {
        "period_label": label, "period_start_date": start, "period_end_date": end, "trading_days": len(rows),
        "start_adj_close": prices[0] if prices else None, "end_adj_close": prices[-1] if prices else None,
        "total_return": prices[-1] / prices[0] - 1 if len(prices) >= 2 else None,
        "annualized_volatility": _std(returns) * math.sqrt(252) if _std(returns) is not None else None,
        "max_drawdown": min(drawdowns) if drawdowns else None, "min_adj_close": lo, "max_adj_close": hi,
        "price_position": position, "positive_day_ratio": sum(x > 0 for x in simple_returns) / len(simple_returns) if simple_returns else None,
        "negative_day_ratio": sum(x < 0 for x in simple_returns) / len(simple_returns) if simple_returns else None,
        "average_abs_return": _mean([abs(x) for x in simple_returns]), "max_daily_up": max(simple_returns) if simple_returns else None,
        "max_daily_down": min(simple_returns) if simple_returns else None,
        "average_volume": _mean(valid_volume), "median_volume": _median(valid_volume), "maximum_volume": max(valid_volume) if valid_volume else None,
        "average_adj_volume": _mean(valid_adj_volume), "median_adj_volume": _median(valid_adj_volume), "maximum_adj_volume": max(valid_adj_volume) if valid_adj_volume else None,
        "average_turnover": _mean(valid_turnover), "median_turnover": _median(valid_turnover), "maximum_turnover": max(valid_turnover) if valid_turnover else None,
        "turnover_trading_days": len(valid_turnover),
    }
    for field in ("forward_per", "pbr", "forward_dividend_yield"):
        result.update(_ratio_stats(rows, field))
    for field in ("forecast_eps", "forecast_annual_dps", "latest_bps", "close"):
        values = [_float(x.get(field)) for x in rows]
        values = [x for x in values if x is not None]
        result[f"{field}_end"] = values[-1] if values else None
    reasons = []
    if len(rows) < 2: reasons.append("insufficient_market_history")
    if result["forward_per_coverage_ratio"] == 0: reasons.append("forecast_eps_unavailable")
    if result["pbr_coverage_ratio"] == 0: reasons.append("bps_unavailable")
    if result["forward_dividend_yield_coverage_ratio"] == 0: reasons.append("forecast_dividend_unavailable")
    result["calculation_status"] = "available" if not reasons else "partial"
    result["insufficient_reasons"] = reasons
    return result


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator not in (None, 0) else None


def compare_periods(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    result = {
        "return_change": (b.get("total_return") or 0) - (a.get("total_return") or 0) if a.get("total_return") is not None and b.get("total_return") is not None else None,
        "volatility_ratio": _ratio(b.get("annualized_volatility"), a.get("annualized_volatility")),
        "drawdown_change": (b.get("max_drawdown") or 0) - (a.get("max_drawdown") or 0) if a.get("max_drawdown") is not None and b.get("max_drawdown") is not None else None,
        "average_volume_ratio": _ratio(b.get("average_volume"), a.get("average_volume")), "median_volume_ratio": _ratio(b.get("median_volume"), a.get("median_volume")),
        "average_turnover_ratio": _ratio(b.get("average_turnover"), a.get("average_turnover")), "median_turnover_ratio": _ratio(b.get("median_turnover"), a.get("median_turnover")),
        "per_median_change": (b.get("forward_per_median") or 0) - (a.get("forward_per_median") or 0) if a.get("forward_per_median") is not None and b.get("forward_per_median") is not None else None,
        "per_median_ratio": _ratio(b.get("forward_per_median"), a.get("forward_per_median")), "pbr_median_change": (b.get("pbr_median") or 0) - (a.get("pbr_median") or 0) if a.get("pbr_median") is not None and b.get("pbr_median") is not None else None,
        "pbr_median_ratio": _ratio(b.get("pbr_median"), a.get("pbr_median")), "dividend_yield_median_change": (b.get("forward_dividend_yield_median") or 0) - (a.get("forward_dividend_yield_median") or 0) if a.get("forward_dividend_yield_median") is not None and b.get("forward_dividend_yield_median") is not None else None,
    }
    result["price_change_factor"] = _ratio(b.get("close_end"), a.get("close_end"))
    result["eps_change_factor"] = _ratio(b.get("forecast_eps_end"), a.get("forecast_eps_end"))
    result["per_change_factor"] = _ratio(b.get("forward_per_end"), a.get("forward_per_end"))
    return result


def analysis_tags(a: dict[str, Any], b: dict[str, Any], comparison: dict[str, Any]) -> list[str]:
    """Produce explainable, non-decisional tags from fixed disclosed thresholds."""
    if a.get("calculation_status") != "available" or b.get("calculation_status") != "available":
        return ["insufficient_data"]
    tags: list[str] = []
    price = comparison.get("price_change_factor")
    eps = comparison.get("eps_change_factor")
    per = comparison.get("per_change_factor")
    turnover = comparison.get("average_turnover_ratio")
    yield_ratio = _ratio(b.get("forward_dividend_yield_median"), a.get("forward_dividend_yield_median"))
    if price is not None and eps is not None and per is not None:
        if price >= 1.10 and eps >= 1.05 and 0.95 <= per <= 1.05:
            tags.append("earnings_driven_rise")
        if per >= 1.10:
            tags.append("multiple_expansion")
        elif per <= 0.90:
            tags.append("multiple_contraction")
        if eps >= 1.05 and price <= 1.03:
            tags.append("forecast_improvement_not_priced")
    if yield_ratio is not None and (yield_ratio >= 1.15 or yield_ratio <= 0.85):
        tags.append("dividend_yield_repricing")
    if turnover is not None and price is not None:
        if turnover >= 1.50 and abs(price - 1) >= 0.10:
            tags.append("high_volume_repricing")
        elif turnover <= 0.67 and abs(price - 1) >= 0.10:
            tags.append("low_volume_move")
    return tags or ["neutral_repricing"]


def analyze_ticker(ticker: str, market_rows: list[dict[str, Any]], financial_rows: list[dict[str, Any]], per_share_rows: list[dict[str, Any]], *, as_of_date: str, as_of_timestamp: str) -> dict[str, Any]:
    market_rows = sorted([r for r in market_rows if str(r.get("ticker")) == ticker and str(r.get("date")) <= as_of_date], key=lambda r: str(r["date"]))
    dates = [str(r["date"]) for r in market_rows]
    if not dates:
        return {"ticker": ticker, "calculation_status": "insufficient_market_history", "insufficient_reasons": ["no_market_data"]}
    events = formal_events(financial_rows, as_of_date=as_of_date)
    boundaries = select_interval_boundaries(events, dates, as_of_date)
    if boundaries is None:
        return {"ticker": ticker, "calculation_status": "insufficient_disclosure_history", "insufficient_reasons": ["fewer_than_two_formal_events"], "formal_events": [asdict(x) for x in events]}
    ids = {(x.disclosed_date, x.fiscal_year_end, x.quarter): x.disclosure_id for x in events}
    ids.update({(x.disclosed_date, "", ""): x.disclosure_id for x in events})
    factors = {str(r["date"]): _float(r.get("adj_factor")) for r in market_rows}
    observations = build_fundamental_intervals(ticker, per_share_rows, dates, ids, as_of_date=as_of_date, factor_by_date=factors)
    valuations = daily_valuations(market_rows, observations)
    a_start, a_end = boundaries["period_a"]; b_start, b_end = boundaries["period_b"]
    period_a = summarize_period(_period_rows(valuations, a_start, a_end), label="A", start=a_start, end=a_end)
    period_b = summarize_period(_period_rows(valuations, b_start, b_end), label="B", start=b_start, end=b_end)
    comparison = compare_periods(period_a, period_b)
    payload = {
        "ticker": ticker, "as_of_date": as_of_date, "as_of_timestamp": as_of_timestamp, "data_cutoff": as_of_date,
        "latest_market_date_used": dates[-1], "latest_disclosure_datetime_used": events[-1].disclosed_at or events[-1].disclosed_date,
        "calculation_version": CALCULATION_VERSION, **boundaries, "formal_events": [asdict(x) for x in events],
        "fundamental_intervals": [asdict(x) for x in observations], "periods": [period_a, period_b], "comparison": comparison,
        "analysis_tags": analysis_tags(period_a, period_b, comparison),
        "daily_valuations": valuations,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    payload["input_hash"] = hashlib.sha256(canonical).hexdigest()
    return payload
