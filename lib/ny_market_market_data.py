"""Deterministic acquisition of canonical NY daily market data.

Acquisition is deliberately separate from ``ny_market_research`` validation.
All prices in this module are unadjusted regular-session daily closes.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from lib.ny_market_price_basis import (close_matches, official_regular_close, parse_exchange_split_notice,
    round_market_display,
    classify_official_discrepancy, previous_on_target_basis, validate_notice_url, validate_vendor_actions)


INDEX_SYMBOLS = {
    "SOX": "^SOX",
    "S&P500": "^GSPC",
    "Dow": "^DJI",
    "Nasdaq": "^IXIC",
    "Russell 2000": "^RUT",
}
MARKET_DATA_CONTRACT_VERSION = "ny_market_data_v1"
SECTOR_NAMES = {
    "XLE": "Energy", "XLU": "Utilities", "XLV": "Health Care",
    "XLP": "Consumer Staples", "XLRE": "Real Estate",
    "XLC": "Communication Services", "XLF": "Financials",
    "XLB": "Materials", "XLI": "Industrials",
    "XLK": "Information Technology", "XLY": "Consumer Discretionary",
}
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
STOCKANALYSIS_GAINERS_URL = "https://stockanalysis.com/markets/gainers/"
NY_TZ = ZoneInfo("America/New_York")
EXCLUDED_INSTRUMENT_RE = re.compile(
    r"(?i)\b(?:warrants?|rights?|units?|preferred|depositary shares representing preferred|"
    r"exchange[- ]traded|ETF|fund|index|debenture|notes?|bonds?)\b"
)


class MarketDataError(RuntimeError):
    pass


_MARKET_NUMERIC_TOKEN_RE = re.compile(
    r"^\s*\$?\s*(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:[.,;:])?\s*$"
)


def parse_market_numeric_token(value: Any, field: str = "market value") -> float:
    """Parse one complete non-negative market numeric token, or fail closed."""
    if isinstance(value, bool):
        raise MarketDataError(f"{field} is not a single market numeric token")
    if isinstance(value, (int, float)):
        result = float(value)
        if result < 0:
            raise MarketDataError(f"{field} must be non-negative")
        return result
    if not isinstance(value, str):
        raise MarketDataError(f"{field} is not a single market numeric token")
    match = _MARKET_NUMERIC_TOKEN_RE.fullmatch(value)
    if match is None:
        raise MarketDataError(f"{field} is not a single market numeric token")
    result = float(match.group("number").replace(",", ""))
    if result < 0:
        raise MarketDataError(f"{field} must be non-negative")
    return result


Transport = Callable[[str, dict[str, str]], bytes]


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return response.read()


@dataclass(frozen=True)
class DailyBar:
    session_date: date
    regular_close: float
    volume: float | None = None


@dataclass(frozen=True)
class DailySeries:
    symbol: str
    provider: str
    source_identifier: str
    retrieved_at: str
    raw_response_sha256: str
    bars: tuple[DailyBar, ...]


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class BatchResult:
    provider: str
    series: dict[str, DailySeries]
    attempts: tuple[ProviderAttempt, ...]


class HistoricalProvider(Protocol):
    name: str

    def fetch(self, symbol: str, start_date: date, end_date: date) -> DailySeries: ...


class DiscrepancyArbitrator(Protocol):
    def resolve(
        self,
        *,
        ticker: str,
        candidate: dict[str, Any],
        historical_series: DailySeries,
        previous: DailyBar,
        target: DailyBar,
        target_session_date: date,
        tolerance_pct: float,
    ) -> dict[str, Any]: ...


class YahooChartProvider:
    """Yahoo historical chart endpoint using unadjusted quote.close only."""

    def __init__(
        self,
        *,
        host: str = "query1.finance.yahoo.com",
        transport: Transport = default_transport,
        now: Callable[[], datetime] | None = None,
        closed_meta_fallback: bool = False,
    ) -> None:
        if host not in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"}:
            raise ValueError("unsupported Yahoo chart host")
        self.host = host
        self.name = ("yahoo_closed_meta_query1" if host.startswith("query1") else "yahoo_closed_meta_query2") if closed_meta_fallback else ("yahoo_chart_query1" if host.startswith("query1") else "yahoo_chart_query2")
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.closed_meta_fallback = closed_meta_fallback

    @staticmethod
    def _unix(day: date) -> int:
        return int(datetime.combine(day, time.min, timezone.utc).timestamp())

    def build_url(self, symbol: str, start_date: date, end_date: date) -> str:
        params = urlencode({
            "period1": self._unix(start_date),
            # Yahoo period2 is exclusive. One extra day also avoids timezone-edge loss.
            "period2": self._unix(end_date + timedelta(days=2)),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "false",
        })
        return f"https://{self.host}/v8/finance/chart/{quote(symbol, safe='')}?{params}"

    def fetch(self, symbol: str, start_date: date, end_date: date) -> DailySeries:
        url = self.build_url(symbol, start_date, end_date)
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        raw_hash = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError(f"{self.name}:{symbol}: invalid JSON") from exc
        chart = payload.get("chart") if isinstance(payload, dict) else None
        if not isinstance(chart, dict) or chart.get("error"):
            raise MarketDataError(f"{self.name}:{symbol}: provider error {chart.get('error') if isinstance(chart, dict) else None}")
        results = chart.get("result")
        if not isinstance(results, list) or len(results) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: missing result")
        result = results[0]
        meta = result.get("meta", {})
        if meta.get("dataGranularity") not in {None, "1d"}:
            raise MarketDataError(f"{self.name}:{symbol}: non-daily response")
        timestamps = result.get("timestamp")
        quotes = result.get("indicators", {}).get("quote")
        if not isinstance(timestamps, list) or not isinstance(quotes, list) or len(quotes) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: missing daily quotes")
        closes = quotes[0].get("close")
        if not isinstance(closes, list) or len(closes) != len(timestamps):
            raise MarketDataError(f"{self.name}:{symbol}: close/timestamp mismatch")
        volumes = quotes[0].get("volume")
        if volumes is not None and (
            not isinstance(volumes, list) or len(volumes) != len(timestamps)
        ):
            raise MarketDataError(f"{self.name}:{symbol}: volume/timestamp mismatch")
        timezone_name = meta.get("exchangeTimezoneName") or "America/New_York"
        try:
            exchange_tz = ZoneInfo(timezone_name)
        except Exception as exc:
            raise MarketDataError(f"{self.name}:{symbol}: invalid exchange timezone") from exc
        bars: list[DailyBar] = []
        volume_values = volumes if isinstance(volumes, list) else [None] * len(timestamps)
        for stamp, close, volume in zip(timestamps, closes, volume_values):
            if close is None:
                continue
            if isinstance(close, bool) or not isinstance(close, (int, float)):
                raise MarketDataError(f"{self.name}:{symbol}: invalid regular close")
            if volume is not None and (isinstance(volume, bool) or not isinstance(volume, (int, float))):
                raise MarketDataError(f"{self.name}:{symbol}: invalid daily volume")
            session_date = datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(exchange_tz).date()
            if start_date <= session_date <= end_date:
                bars.append(DailyBar(
                    session_date=session_date,
                    regular_close=float(close),
                    volume=float(volume) if volume is not None else None,
                ))
        deduplicated = {bar.session_date: bar for bar in bars}
        used_closed_meta = False
        if self.closed_meta_fallback and end_date not in deduplicated:
            trading_period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            market_end = trading_period.get("end")
            market_time = meta.get("regularMarketTime")
            market_price = meta.get("regularMarketPrice")
            if (isinstance(market_end, int) and isinstance(market_time, int)
                    and market_end - 30 <= market_time <= market_end + 2
                    and self.now().timestamp() > market_end
                    and datetime.fromtimestamp(market_time, timezone.utc).astimezone(exchange_tz).date() == end_date
                    and isinstance(market_price, (int, float)) and not isinstance(market_price, bool)
                    and market_price > 0 and deduplicated):
                deduplicated[end_date] = DailyBar(
                    session_date=end_date, regular_close=float(market_price),
                    volume=float(meta["regularMarketVolume"]) if isinstance(meta.get("regularMarketVolume"), (int, float)) else None,
                )
                used_closed_meta = True
        ordered = tuple(deduplicated[day] for day in sorted(deduplicated))
        if not ordered:
            raise MarketDataError(f"{self.name}:{symbol}: no completed daily bars in requested window")
        returned_symbol = str(meta.get("symbol") or symbol)
        if returned_symbol.upper() != symbol.upper():
            raise MarketDataError(f"{self.name}:{symbol}: response symbol mismatch {returned_symbol}")
        return DailySeries(
            symbol=symbol,
            provider=self.name,
            source_identifier=url + ("#closed-regularMarketPrice" if used_closed_meta else ""),
            retrieved_at=self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            raw_response_sha256=raw_hash,
            bars=ordered,
        )


def fetch_all_or_fallback(
    providers: Iterable[HistoricalProvider],
    symbols: Iterable[str],
    start_date: date,
    end_date: date,
    *, required_session_date: date | None = None,
) -> BatchResult:
    """Fetch the complete group from one provider; never mix providers."""
    requested = tuple(symbols)
    attempts: list[ProviderAttempt] = []
    for provider in providers:
        fetched: dict[str, DailySeries] = {}
        errors: list[str] = []
        for symbol in requested:
            try:
                fetched[symbol] = provider.fetch(symbol, start_date, end_date)
                if required_session_date and not any(bar.session_date == required_session_date for bar in fetched[symbol].bars):
                    errors.append(f"{symbol}: required target session {required_session_date} missing")
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        if not errors and len(fetched) == len(requested):
            attempts.append(ProviderAttempt(provider.name, "success", ()))
            return BatchResult(provider=provider.name, series=fetched, attempts=tuple(attempts))
        attempts.append(ProviderAttempt(provider.name, "failed", tuple(errors)))
    summary = "; ".join(f"{item.provider}={len(item.errors)} errors" for item in attempts)
    target_note = f" for target session {required_session_date}" if required_session_date else ""
    raise MarketDataError(f"all historical providers failed complete-group acquisition{target_note}: {summary}")


def completed_session_pair(series: DailySeries, target_session_date: date) -> tuple[DailyBar, DailyBar]:
    eligible = [bar for bar in series.bars if bar.session_date <= target_session_date]
    if not eligible or eligible[-1].session_date != target_session_date:
        raise MarketDataError(f"{series.symbol}: target session {target_session_date} is missing")
    if len(eligible) < 2:
        raise MarketDataError(f"{series.symbol}: previous completed session is missing")
    return eligible[-2], eligible[-1]


def resolve_latest_completed_sessions(series: DailySeries, report_date_jst: date) -> tuple[date, date]:
    """Resolve sessions from actual daily-bar existence, including holidays/weekends."""
    eligible = sorted({bar.session_date for bar in series.bars if bar.session_date < report_date_jst})
    if len(eligible) < 2:
        raise MarketDataError("cannot resolve two completed NY sessions")
    return eligible[-2], eligible[-1]


def _canonical_price_record(
    display_symbol: str,
    series: DailySeries,
    previous: DailyBar,
    target: DailyBar,
) -> dict[str, Any]:
    change = target.regular_close - previous.regular_close
    change_pct = (target.regular_close / previous.regular_close - 1.0) * 100.0
    return {
        "symbol": display_symbol,
        "session_date": target.session_date.isoformat(),
        "previous_session_date": previous.session_date.isoformat(),
        "previous_regular_close": previous.regular_close,
        "regular_close": target.regular_close,
        # Compatibility aliases consumed by ny_market_research validation.
        "previous_close": previous.regular_close,
        "close": target.regular_close,
        "change": change,
        "change_pct": change_pct,
        "provider": series.provider,
        "source_identifier": series.source_identifier,
        "retrieved_at": series.retrieved_at,
        "raw_response_sha256": series.raw_response_sha256,
        "price_basis": "regular_close",
        "adjusted": False,
    }


def build_index_sector_snapshot(
    providers: Iterable[HistoricalProvider],
    target_session_date: date,
) -> dict[str, Any]:
    all_symbols = tuple(INDEX_SYMBOLS.values()) + tuple(SECTOR_NAMES)
    # Narrow window matches the audited successful path. Expand only when the
    # immediately prior calendar day is not a completed session.
    starts = (target_session_date - timedelta(days=1), target_session_date - timedelta(days=10))
    last_error: Exception | None = None
    for start_date in starts:
        try:
            batch = fetch_all_or_fallback(
                providers, all_symbols, start_date, target_session_date,
                required_session_date=target_session_date,
            )
            pairs = {
                symbol: completed_session_pair(series, target_session_date)
                for symbol, series in batch.series.items()
            }
            break
        except Exception as exc:
            last_error = exc
    else:
        raise MarketDataError(f"canonical index/sector group failed: {last_error}")

    indexes = [
        _canonical_price_record(name, batch.series[symbol], *pairs[symbol])
        for name, symbol in INDEX_SYMBOLS.items()
    ]
    sectors = [
        {
            **_canonical_price_record(symbol, batch.series[symbol], *pairs[symbol]),
            "sector": SECTOR_NAMES[symbol],
        }
        for symbol in SECTOR_NAMES
    ]
    sectors.sort(key=lambda row: row["change_pct"], reverse=True)
    for rank, row in enumerate(sectors, start=1):
        row["rank"] = rank
    retrieved_at = max(series.retrieved_at for series in batch.series.values())
    source_host = urlsplit(next(iter(batch.series.values())).source_identifier).hostname
    return {
        "price_basis": "regular_close",
        "adjusted": False,
        "market_session_date": target_session_date.isoformat(),
        "source": {
            "name": batch.provider,
            "url": f"https://{source_host}/v8/finance/chart/",
            "retrieved_at": retrieved_at,
        },
        "indexes": indexes,
        "sectors": sectors,
        "provider_attempts": [attempt.__dict__ for attempt in batch.attempts],
    }


class NasdaqScreenerProvider:
    name = "nasdaq_stock_screener"

    def __init__(self, *, transport: Transport = default_transport, now: Callable[[], datetime] | None = None) -> None:
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> dict[str, Any]:
        query = urlencode({"tableonly": "true", "limit": 10000, "offset": 0, "download": "true"})
        url = f"{NASDAQ_SCREENER_URL}?{query}"
        raw = self.transport(url, {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
        })
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError("Nasdaq screener returned invalid JSON") from exc
        status = payload.get("status", {})
        if status.get("rCode") not in {None, 200}:
            raise MarketDataError(f"Nasdaq screener error: {status}")
        rows = payload.get("data", {}).get("rows")
        if not isinstance(rows, list) or len(rows) < 100:
            raise MarketDataError("Nasdaq screener returned an incomplete universe")
        return {
            "provider": self.name,
            "source_identifier": url,
            "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "rows": rows,
        }


class StockAnalysisGainersProvider:
    """Current-session all-stock gainers table used when Nasdaq's bulk file lags."""

    name = "stockanalysis_stock_movers"
    _ROW_RE = re.compile(
        r'\{no:(?P<rank>\d+),s:"(?P<symbol>[^"]+)",n:"(?P<name>(?:[^"\\]|\\.)*)",'
        r'change:(?P<change>-?(?:\d+(?:\.\d+)?|\.\d+)),priceDate:"(?P<date>\d{4}-\d{2}-\d{2})",'
        r'price:(?P<price>-?(?:\d+(?:\.\d+)?|\.\d+)),volume:(?P<volume>-?(?:\d+(?:\.\d+)?|\.\d+)),'
        r'marketCap:(?P<market_cap>-?(?:\d+(?:\.\d+)?|\.\d+)|null)\}'
    )

    def __init__(self, *, transport: Transport = default_transport, now: Callable[[], datetime] | None = None) -> None:
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, target_session_date: date) -> dict[str, Any]:
        raw = self.transport(STOCKANALYSIS_GAINERS_URL, {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        page = raw.decode("utf-8")
        matches = list(self._ROW_RE.finditer(page))
        if len(matches) < 20:
            raise MarketDataError("StockAnalysis gainers table returned fewer than 20 rows")
        rows: list[dict[str, Any]] = []
        for match in matches:
            session_date = date.fromisoformat(match.group("date"))
            if session_date != target_session_date:
                raise MarketDataError(
                    f"StockAnalysis gainers table is stale: {session_date} != {target_session_date}"
                )
            name = json.loads(f'"{match.group("name")}"')
            rows.append({
                "symbol": match.group("symbol"),
                "name": name,
                "lastsale": f'${match.group("price")}',
                "netchange": None,
                "pctchange": f'{match.group("change")}% ',
                "volume": match.group("volume"),
                "marketCap": None if match.group("market_cap") == "null" else match.group("market_cap"),
                "instrument_type": "common_stock",
            })
        return {
            "provider": self.name,
            "source_identifier": STOCKANALYSIS_GAINERS_URL,
            "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "rows": rows,
        }


def provider_family(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized.startswith("yahoo_") or "finance.yahoo.com" in normalized:
        return "yahoo"
    if normalized.startswith("nasdaq_") or normalized == "nasdaq":
        return "nasdaq"
    return normalized


def _raw_json(raw: bytes, provider: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketDataError(f"{provider}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise MarketDataError(f"{provider}: response must be an object")
    return value


class NasdaqOfficialCloseProvider:
    """Official previous-close evidence for a Nasdaq-listed equity."""

    name = "nasdaq_official_historical_nls"
    family = "nasdaq"

    def __init__(self, *, transport: Transport = default_transport, now: Callable[[], datetime] | None = None) -> None:
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _headers(symbol: str) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}/historical",
        }

    def fetch(self, symbol: str, target_session_date: date, screener_last_sale: float) -> dict[str, Any]:
        start = target_session_date - timedelta(days=35)
        end = target_session_date + timedelta(days=1)
        historical_url = (
            f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/historical?"
            + urlencode({
                "assetclass": "stocks", "fromdate": start.isoformat(),
                "todate": end.isoformat(), "limit": 5000,
            })
        )
        historical_raw = self.transport(historical_url, self._headers(symbol))
        historical = _raw_json(historical_raw, self.name)
        if historical.get("status", {}).get("rCode") != 200:
            raise MarketDataError(f"{self.name}:{symbol}: historical request failed")
        rows = historical.get("data", {}).get("tradesTable", {}).get("rows")
        if not isinstance(rows, list):
            raise MarketDataError(f"{self.name}:{symbol}: historical rows missing")
        parsed_rows: list[tuple[date, dict[str, Any]]] = []
        target_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_date = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
            except ValueError:
                continue
            if row_date < target_session_date:
                parsed_rows.append((row_date, row))
            elif row_date == target_session_date:
                target_rows.append(row)
        if len(target_rows) > 1:
            raise MarketDataError(f"{self.name}:{symbol}: duplicate official target close")
        if not parsed_rows:
            raise MarketDataError(f"{self.name}:{symbol}: official previous close missing")
        previous_day, previous_row = max(parsed_rows, key=lambda item: item[0])
        previous_close = _numeric(previous_row.get("close"), "official previous close")

        nls_url = (
            f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/realtime-trades?"
            + urlencode({"assetclass": "stocks", "limit": 5000})
        )
        nls_raw = self.transport(nls_url, self._headers(symbol))
        nls = _raw_json(nls_raw, self.name)
        top_rows = nls.get("data", {}).get("topTable", {}).get("rows")
        nls_previous: float | None = None
        if isinstance(top_rows, list) and top_rows and isinstance(top_rows[0], dict):
            nls_previous = _numeric(top_rows[0].get("previousClose"), "NLS previous close", nullable=True)
        # NLS is a live quote. Its previousClose belongs to the current New York
        # trading day, so it cannot corroborate a historical session's prior close.
        nls_session_date = self.now().astimezone(NY_TZ).date()
        if (
            target_session_date == nls_session_date
            and nls_previous is not None
            and abs(nls_previous - previous_close) > max(0.02, previous_close * 0.002)
        ):
            raise MarketDataError(f"{self.name}:{symbol}: official historical/NLS previous close mismatch")
        info_url = f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/info?assetclass=stocks"
        info_raw = self.transport(info_url, self._headers(symbol))
        info = _raw_json(info_raw, self.name).get("data") or {}
        try:
            target_close, target_stamp = official_regular_close(info, symbol, target_session_date)
            target_close_source = "nasdaq_info"
        except ValueError as exc:
            if len(target_rows) != 1:
                raise MarketDataError(f"{self.name}:{symbol}: {exc}") from exc
            target_close = _numeric(target_rows[0].get("close"), "official target close")
            target_stamp = f"Nasdaq historical close {target_session_date:%m/%d/%Y}"
            target_close_source = "nasdaq_historical"
        if target_rows:
            historical_target_close = _numeric(target_rows[0].get("close"), "official target close")
            if abs(target_close - historical_target_close) > max(1e-7, historical_target_close * 1e-6):
                raise MarketDataError(f"{self.name}:{symbol}: official info/historical target close mismatch")
        return {
            "provider": self.name, "provider_family": self.family,
            "target_close_verified": True, "target_timestamp": target_stamp,
            "target_close_source": target_close_source,
            "target_session_date": target_session_date.isoformat(),
            "notifications": info.get("notifications", []),
            "previous_session_date": previous_day.isoformat(),
            "previous_close": previous_close, "target_close": target_close,
            "previous_volume": _numeric(previous_row.get("volume"), "official previous volume", nullable=True),
            "source_identifiers": [historical_url, nls_url, info_url],
            "raw_response_sha256": [
                hashlib.sha256(historical_raw).hexdigest(), hashlib.sha256(nls_raw).hexdigest(), hashlib.sha256(info_raw).hexdigest(),
            ],
            "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
        }


class YahooCorporateActionProvider:
    name = "yahoo_corporate_actions"
    family = "yahoo"

    def __init__(self, *, host: str = "query1.finance.yahoo.com", transport: Transport = default_transport) -> None:
        self.host = host
        self.transport = transport

    def fetch(self, symbol: str, target_session_date: date) -> dict[str, Any]:
        params = urlencode({
            "period1": YahooChartProvider._unix(target_session_date - timedelta(days=10)),
            "period2": YahooChartProvider._unix(target_session_date + timedelta(days=2)),
            "interval": "1d", "events": "div,splits,capitalGains",
        })
        url = f"https://{self.host}/v8/finance/chart/{quote(symbol, safe='')}?{params}"
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        payload = _raw_json(raw, self.name)
        results = payload.get("chart", {}).get("result")
        if not isinstance(results, list) or len(results) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: action response missing")
        events = results[0].get("events")
        found = bool(events)
        return {
            "provider": self.name, "provider_family": self.family,
            "status": "corporate_action_found" if found else "checked_none",
            "events": events or {}, "source_identifier": url,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        }


class YahooMinuteCloseProvider:
    name = "yahoo_minute_close"
    family = "yahoo"

    def __init__(self, *, host: str = "query1.finance.yahoo.com", transport: Transport = default_transport) -> None:
        self.host = host
        self.transport = transport

    def fetch(self, symbol: str, previous_date: date, target_date: date) -> dict[str, Any]:
        params = urlencode({
            "period1": YahooChartProvider._unix(previous_date),
            "period2": YahooChartProvider._unix(target_date + timedelta(days=1)),
            "interval": "1m", "events": "history", "includePrePost": "true",
        })
        url = f"https://{self.host}/v8/finance/chart/{quote(symbol, safe='')}?{params}"
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        payload = _raw_json(raw, self.name)
        results = payload.get("chart", {}).get("result")
        if not isinstance(results, list) or len(results) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: minute response missing")
        result = results[0]
        stamps = result.get("timestamp")
        quotes = result.get("indicators", {}).get("quote")
        if not isinstance(stamps, list) or not isinstance(quotes, list) or len(quotes) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: minute bars missing")
        closes = quotes[0].get("open")
        volumes = quotes[0].get("volume")
        if not isinstance(closes, list) or len(closes) != len(stamps):
            raise MarketDataError(f"{self.name}:{symbol}: minute close mismatch")
        close_by_date: dict[date, tuple[int, float, float | None]] = {}
        last_regular_bar: dict[date, float] = {}
        bar_closes = quotes[0].get("close") or []
        for index, (stamp, close) in enumerate(zip(stamps, closes)):
            if close is None:
                continue
            local = datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(NY_TZ)
            if local.time() == time(15, 59) and index < len(bar_closes) and bar_closes[index] is not None:
                last_regular_bar[local.date()] = float(bar_closes[index])
            if local.time() == time(16, 0):
                volume = volumes[index] if isinstance(volumes, list) and index < len(volumes) else None
                close_by_date[local.date()] = (int(stamp), float(close), float(volume) if volume is not None else None)
        if previous_date not in close_by_date or target_date not in close_by_date:
            raise MarketDataError(f"{self.name}:{symbol}: 16:00 close evidence missing")
        return {
            "provider": self.name, "provider_family": self.family,
            "price_field": "boundary_open",
            "boundary_note": "16:00 bar open corroborates exchange close; bar close is post-market",
            "previous_last_regular_bar_close": last_regular_bar.get(previous_date),
            "target_last_regular_bar_close": last_regular_bar.get(target_date),
            "previous_close": close_by_date[previous_date][1], "target_close": close_by_date[target_date][1],
            "previous_timestamp": close_by_date[previous_date][0], "target_timestamp": close_by_date[target_date][0],
            "previous_volume": close_by_date[previous_date][2], "target_volume": close_by_date[target_date][2],
            "source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        }


class PublicPreviousCloseProvider:
    """Independent public display used only as discrepancy evidence."""

    name = "public_com_previous_close"
    family = "public_com"
    _PATTERNS = (
        re.compile(r"previous market close(?:\s+of|\s+was)?\s*\$([0-9][0-9,.]*)", re.I),
        re.compile(r"previous close(?:\s+of|\s+was)?\s*\$([0-9][0-9,.]*)", re.I),
    )

    def __init__(self, *, transport: Transport = default_transport) -> None:
        self.transport = transport

    def fetch(self, symbol: str, target_session_date: date) -> dict[str, Any]:
        url = f"https://public.com/stocks/{quote(symbol.lower(), safe='')}/pre-market"
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        text = html.unescape(raw.decode("utf-8", errors="replace"))
        match = None
        for pattern in self._PATTERNS:
            match = pattern.search(text)
            if match:
                break
        if match is None:
            raise MarketDataError(f"{self.name}:{symbol}: previous close not found")
        raw_value = match.group(1)
        parsed_value = parse_market_numeric_token(raw_value, "independent previous close")
        return {
            "provider": self.name, "provider_family": self.family,
            "raw_value": raw_value, "parsed_value": parsed_value,
            "previous_close": parsed_value, "target_close": None,
            "target_session_date": target_session_date.isoformat(), "source_identifier": url,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        }


class CapturedEndOfDayQuoteProvider:
    """Parse a captured quote-page accessibility record as independent EOD evidence."""

    name = "captured_end_of_day_quote"
    family = "factset"

    def __init__(self, snapshots: dict[str, dict[str, str]]) -> None:
        self.snapshots = snapshots

    def fetch(self, symbol: str, target_session_date: date) -> dict[str, Any]:
        entry = self.snapshots.get(symbol)
        if not entry:
            raise MarketDataError(f"{self.name}:{symbol}: snapshot unavailable")
        url = str(entry["source_url"])
        if urlsplit(url).hostname == "chartexchange.com":
            return self._fetch_chartexchange(symbol, target_session_date, entry, url)
        if urlsplit(url).hostname != "www.marketwatch.com" or not url.endswith(f"/stock/{symbol.lower()}"):
            raise MarketDataError(f"{self.name}:{symbol}: snapshot URL/symbol mismatch")
        raw = Path(entry["path"]).read_bytes()
        state = raw.decode("utf-8")
        if not re.search(rf"AXWebArea .*\b{re.escape(symbol)}\b.*MarketWatch", state):
            raise MarketDataError(f"{self.name}:{symbol}: captured page identity mismatch")
        stamp = re.search(r"AFTER HOURS Last Updated:\s*([A-Za-z]{3} \d{1,2}, \d{4})", state)
        if not stamp or datetime.strptime(stamp[1], "%b %d, %Y").date() != target_session_date:
            raise MarketDataError(f"{self.name}:{symbol}: quote session date mismatch")
        row = re.search(
            r"text CLOSE\s+\d+ cell.*?text CHG\s+\d+ cell.*?text CHG %"
            r"\s+\d+ row.*?text \$(\d+(?:\.\d+)?)\s+\d+ cell.*?text ([+-]?\d+(?:\.\d+)?)"
            r"\s+\d+ cell.*?text ([+-]?\d+(?:\.\d+)?)%",
            state, re.S,
        )
        if not row:
            raise MarketDataError(f"{self.name}:{symbol}: separate regular close row missing")
        close_raw, delta_raw, pct_raw = row.groups()
        close, delta, pct = map(float, row.groups())
        previous = close - delta
        decimal_places = len(close_raw.partition(".")[2])
        quantum = 10 ** -decimal_places
        half_quantum = quantum / 2
        delta_low = delta - half_quantum
        delta_high = delta + half_quantum
        close_low = close - half_quantum
        close_high = close + half_quantum
        pct_bounds = [
            delta_value / (close_value - delta_value) * 100
            for close_value in (close_low, close_high)
            for delta_value in (delta_low, delta_high)
            if close_value - delta_value > 0
        ]
        pct_low = min(pct_bounds)
        pct_high = max(pct_bounds)
        pct_display_half_unit = 0.005
        if previous <= 0 or not (pct_low - pct_display_half_unit <= pct <= pct_high + pct_display_half_unit):
            raise MarketDataError(f"{self.name}:{symbol}: EOD quote arithmetic mismatch")
        if "Historical and current end-of-day data provided by" not in state or "FACTSET" not in state:
            raise MarketDataError(f"{self.name}:{symbol}: end-of-day source attribution missing")
        exchange_match = re.search(r"Stock Quote \(U\.S\.:\s*([^)]+)\)", state, re.I)
        primary_exchange = exchange_match.group(1).strip() if exchange_match else None
        return {
            "provider": self.name, "provider_family": self.family,
            "session": "regular_close", "target_session_date": target_session_date.isoformat(),
            "previous_close": previous, "target_close": close,
            "raw_value": close_raw, "parsed_value": close,
            "reported_change_raw": delta_raw, "reported_pct_raw": pct_raw,
            "display_decimal_places": decimal_places,
            "primary_exchange": primary_exchange,
            "capture_type": "browser_accessibility_snapshot",
            "source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _fetch_chartexchange(
        self, symbol: str, target_session_date: date,
        entry: dict[str, str], url: str,
    ) -> dict[str, Any]:
        from bs4 import BeautifulSoup

        if not re.fullmatch(
            rf"/symbol/[a-z0-9-]+-{re.escape(symbol.lower())}/historical/",
            urlsplit(url).path,
        ):
            raise MarketDataError(f"{self.name}:{symbol}: snapshot URL/symbol mismatch")
        raw = Path(entry["path"]).read_bytes()
        page = BeautifulSoup(raw, "html.parser")
        title = page.title.get_text(" ", strip=True) if page.title else ""
        canonical = page.find("link", rel="canonical")
        if title != f"{symbol} Historical Prices | ChartExchange" or not canonical or canonical.get("href") != url:
            raise MarketDataError(f"{self.name}:{symbol}: captured page identity mismatch")
        tables = []
        for table in page.find_all("table"):
            header = table.find("tr")
            if header is None:
                continue
            labels = [cell.get_text(" ", strip=True) for cell in header.find_all(["td", "th"])]
            if (len(labels) >= 6 and labels[0] in {"Date (EDT)", "Date (EST)"}
                    and labels[1:6] == ["Open", "High", "Low", "Close", "Change"]):
                tables.append(table)
        if len(tables) != 1:
            raise MarketDataError(f"{self.name}:{symbol}: historical table ambiguous")
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                for row in tables[0].find_all("tr")[1:]]
        dated = []
        for row in rows:
            if len(row) >= 6 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[0]):
                dated.append((date.fromisoformat(row[0]), row))
        targets = [row for day, row in dated if day == target_session_date]
        previous = [row for day, row in dated if day < target_session_date]
        if len(targets) != 1 or not previous:
            raise MarketDataError(f"{self.name}:{symbol}: target/previous session missing")
        target_row = targets[0]
        # The captured table is reverse chronological; select the nearest earlier dated row.
        previous_date = max(day for day, _ in dated if day < target_session_date)
        previous_row = next(row for day, row in dated if day == previous_date)
        close_raw, previous_raw, pct_raw = target_row[4], previous_row[4], target_row[5]
        close = parse_market_numeric_token(close_raw, "captured close")
        previous_close = parse_market_numeric_token(previous_raw, "captured previous close")
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", pct_raw):
            raise MarketDataError(f"{self.name}:{symbol}: invalid change percent")
        pct = float(pct_raw.removesuffix("%"))
        if previous_close <= 0 or abs((close / previous_close - 1) * 100 - pct) > 0.006:
            raise MarketDataError(f"{self.name}:{symbol}: EOD quote arithmetic mismatch")
        return {
            "provider": self.name, "provider_family": "chartexchange",
            "session": "regular_close", "target_session_date": target_session_date.isoformat(),
            "previous_close": previous_close, "target_close": close,
            "raw_value": close_raw, "parsed_value": close,
            "reported_change_raw": str(close - previous_close), "reported_pct_raw": pct_raw,
            "display_decimal_places": len(close_raw.partition(".")[2]),
            "primary_exchange": urlsplit(url).path.split("/")[2].rsplit("-", 1)[0].upper(),
            "capture_type": "captured_historical_html",
            "source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        }


def _numeric(value: Any, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise MarketDataError(f"{field} is not numeric")
    cleaned = value.strip().replace("$", "").replace("%", "").replace(",", "")
    if cleaned in {"", "N/A", "--"} and nullable:
        return None
    try:
        return float(cleaned)
    except ValueError as exc:
        raise MarketDataError(f"{field} is not numeric") from exc


def resolve_discrepancy(
    *,
    candidate: dict[str, Any],
    historical_provider: str,
    historical_previous_close: float,
    historical_target_close: float,
    historical_change_pct: float,
    official: dict[str, Any],
    corporate_action: dict[str, Any],
    minute_close: dict[str, Any] | None,
    independent_sources: list[dict[str, Any]],
    tolerance_pct: float,
    resolved_at: str,
) -> dict[str, Any]:
    """Resolve a mismatch using official, action, and independent evidence."""
    screener_change = float(candidate["_change_pct"])
    screener_last = float(candidate["_close"])
    screener_net = _numeric(candidate.get("netchange"), "screener net change", nullable=True)
    implied_previous = screener_last / (1.0 + screener_change / 100.0)
    official_previous = float(official["previous_close"])
    official_target = float(official["target_close"])
    official_change = (official_target / official_previous - 1.0) * 100.0
    official_supports = (
        abs(official_change - screener_change) <= tolerance_pct
        and abs(official_target - screener_last) <= max(0.02, official_target * 0.002)
    )
    if screener_net is not None:
        official_supports = official_supports and abs((screener_last - official_previous) - screener_net) <= 0.02

    action_status = corporate_action.get("status")
    action_checked = action_status in {"checked_none", "corporate_action_adjusted"}
    excluded_families = {"nasdaq", provider_family(historical_provider)}
    independent_support: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    for source in independent_sources:
        family = provider_family(str(source.get("provider_family") or source.get("provider") or ""))
        if not family or family in excluded_families or family in seen_families:
            continue
        source_previous = _numeric(source.get("previous_close"), "independent previous close", nullable=True)
        source_target = _numeric(source.get("target_close"), "independent target close", nullable=True)
        previous_matches = (
            source_previous is not None
            and abs(source_previous - official_previous) <= max(0.02, official_previous * 0.002)
        )
        target_matches = (
            source_target is None
            or abs(source_target - official_target) <= max(0.02, official_target * 0.002)
        )
        if previous_matches and target_matches:
            independent_support.append(source)
            seen_families.add(family)

    reason, basis_evidence = classify_official_discrepancy(
        official=official, history_previous=historical_previous_close,
        history_target=historical_target_close, minute=minute_close, action=corporate_action,
    )
    if reason is None and minute_close is not None and not corporate_action.get("official_action"):
        minute_previous = float(minute_close["previous_close"])
        minute_target = float(minute_close["target_close"])
        if (
            abs(minute_previous - official_previous) <= max(0.02, official_previous * 0.002)
            and abs(minute_target - official_target) <= max(0.02, official_target * 0.002)
            and abs(historical_previous_close - official_previous) > max(0.02, official_previous * 0.002)
            and abs(historical_target_close - official_target) <= max(0.02, official_target * 0.002)
        ):
            reason = "stale_daily_bar"

    if reason is None and action_status == "checked_none" and minute_close is not None:
        dated_closed = (
            official.get("target_close_verified") is True
            and official.get("target_close_source") in {"nasdaq_info", "nasdaq_historical"}
            and (
                re.fullmatch(r"Closed at .* 4:00 PM ET", str(official.get("target_timestamp", ""))) is not None
                or (
                    official.get("target_close_source") == "nasdaq_historical"
                    and str(official.get("target_timestamp", "")).startswith("Nasdaq historical close ")
                )
            )
        )
        eod_support = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and (
                close_matches(float(source["target_close"]), official_target)
                or abs(float(source["target_close"]) - official_target)
                <= 0.5 * 10 ** -int(source.get("display_decimal_places", 8)) + 1e-12
            )
            and (
                close_matches(float(source["previous_close"]), official_previous)
                or abs(float(source["previous_close"]) - official_previous)
                <= 10 ** -int(source.get("display_decimal_places", 8)) + 1e-12
            )
        )]
        rounded_vendor_previous = any(
            close_matches(historical_previous_close, round_market_display(official_previous, digits))
            for digits in range(2, 5)
        )
        rounded_vendor_target = any(
            close_matches(historical_target_close, round_market_display(official_target, digits))
            for digits in range(2, 5)
        )
        if (dated_closed and eod_support and rounded_vendor_previous
                and rounded_vendor_target
                and not close_matches(float(minute_close["target_close"]), official_target)):
            reason = "official_closed_eod_with_secondary_boundary_difference"
            basis_evidence = {
                "official_previous_close": official_previous,
                "vendor_daily_previous_close": historical_previous_close,
                "official_target_close": official_target,
                "vendor_daily_target_close": historical_target_close,
                "vendor_previous_rounding_places": [digits for digits in range(2, 5) if close_matches(historical_previous_close, round_market_display(official_previous, digits))],
                "vendor_target_rounding_places": [digits for digits in range(2, 5) if close_matches(historical_target_close, round_market_display(official_target, digits))],
                "secondary_boundary_open": float(minute_close["target_close"]),
                "secondary_boundary_price_field": minute_close.get("price_field"),
                "independent_eod": [{
                    "provider_family": s["provider_family"], "raw_value": s["raw_value"],
                    "reported_change_raw": s["reported_change_raw"],
                    "display_decimal_places": s["display_decimal_places"],
                    "source_identifier": s["source_identifier"],
                    "raw_response_sha256": s["raw_response_sha256"],
                } for s in eod_support],
                "comparison_basis": "dated_regular_close_not_secondary_boundary_open",
            }

    if (reason is None or (reason == "stale_daily_bar" and not official_supports)) and action_status == "checked_none":
        non_nasdaq_eod = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("primary_exchange")
            and "NASDAQ" not in str(source.get("primary_exchange")).upper()
            and abs(float(source["target_close"]) - screener_last)
            <= max(0.02, screener_last * 0.002)
            and abs(float(source["previous_close"]) - implied_previous)
            <= max(0.02, implied_previous * 0.002)
            and abs(historical_target_close - screener_last)
            <= max(0.02, screener_last * 0.002)
        )]
        if non_nasdaq_eod:
            reason = "non_nasdaq_primary_exchange_eod"
            official_supports = True
            basis_evidence = {
                "primary_exchange": non_nasdaq_eod[0]["primary_exchange"],
                "primary_exchange_eod_close": float(non_nasdaq_eod[0]["target_close"]),
                "nasdaq_info_close": official_target,
                "screener_close": screener_last,
                "vendor_daily_close": historical_target_close,
                "comparison_basis": "primary_exchange_regular_close",
                "nasdaq_info_role": "nasdaq_reported_last_sale_not_primary_exchange_close",
            }

    # Preserve the pre-existing sub-dollar Yahoo precision fallback when the
    # stronger dated-close/EOD and primary-exchange arbitration do not apply.
    target_tolerance = max(0.0001, official_target * 0.002)
    previous_tolerance = max(0.0001, official_previous * 0.002)
    previous_delta = abs(historical_previous_close - official_previous)
    if (
        reason is None
        and official_target < 1.0
        and abs(historical_target_close - official_target) <= target_tolerance
        and previous_delta > previous_tolerance
        and previous_delta <= 0.005
    ):
        reason = "daily_bar_precision_rounding"

    if not official_supports or not action_checked or not independent_support or reason is None:
        problems = []
        if not official_supports:
            problems.append(
                "official market source does not support Screener"
                f" (classified_reason={reason!r}, independent_exchanges="
                f"{[item.get('primary_exchange') for item in independent_support]!r})"
            )
        if not action_checked:
            problems.append("corporate action is not resolved")
        if not independent_support:
            problems.append("no independent supporting source")
        if reason is None:
            problems.append("provider discrepancy reason is unresolved")
        raise MarketDataError("unresolved Top20 discrepancy: " + "; ".join(problems))

    volume = _numeric(candidate.get("volume"), "screener volume", nullable=True)
    liquidity_flag = "low_liquidity" if volume is not None and volume < 250_000 else "normal_liquidity"
    sources = [
        {
            "provider": official["provider"], "provider_family": official.get("provider_family", "nasdaq"),
            "source_identifier": official.get("source_identifiers"),
            "raw_response_sha256": official.get("raw_response_sha256"), "role": "official_market_source",
        },
        {
            "provider": corporate_action["provider"], "provider_family": corporate_action.get("provider_family"),
            "source_identifier": corporate_action.get("source_identifier"),
            "raw_response_sha256": corporate_action.get("raw_response_sha256"), "role": "corporate_action_check",
        },
    ]
    if minute_close is not None:
        sources.append({
            "provider": minute_close["provider"], "provider_family": minute_close.get("provider_family"),
            "source_identifier": minute_close.get("source_identifier"),
            "raw_response_sha256": minute_close.get("raw_response_sha256"), "role": "stale_bar_diagnosis",
        })
    if corporate_action.get("official_action"):
        sources.append({**corporate_action["official_action"], "provider": "nasdaq_corporate_action_notice", "provider_family": "nasdaq", "role": "official_corporate_action"})
    sources.extend({
        "provider": item["provider"], "provider_family": item.get("provider_family"),
        "source_identifier": item.get("source_identifier"),
        "raw_response_sha256": item.get("raw_response_sha256"), "role": "independent_support",
        "raw_value": item.get("raw_value"), "parsed_value": item.get("parsed_value"),
    } for item in independent_support)
    compared = sorted({
        provider_family(historical_provider),
        provider_family(str(official.get("provider_family") or official["provider"])),
        *(provider_family(str(item.get("provider_family") or item["provider"])) for item in independent_support),
    })
    return {
        "discrepancy_status": "resolved", "discrepancy_reason": reason,
        "basis_evidence": basis_evidence,
        "screener_change_pct": screener_change, "screener_last_sale": screener_last,
        "screener_net_change": screener_net, "screener_implied_previous_close": implied_previous,
        "historical_previous_close": historical_previous_close,
        "historical_target_close": historical_target_close,
        "historical_change_pct": historical_change_pct,
        "official_previous_close": official_previous, "official_target_close": official_target,
        "corporate_action_status": action_status, "liquidity_flag": liquidity_flag,
        "compared_providers": compared, "supporting_sources": sources, "resolved_at": resolved_at,
    }


class LiveDiscrepancyArbitrator:
    def __init__(
        self,
        *,
        official_provider: NasdaqOfficialCloseProvider | None = None,
        action_provider: YahooCorporateActionProvider | None = None,
        minute_provider: YahooMinuteCloseProvider | None = None,
        independent_providers: Iterable[PublicPreviousCloseProvider] | None = None,
        independent_snapshots: dict[str, dict[str, str]] | None = None,
        corporate_action_notices: dict[str, str] | None = None,
        notice_transport: Transport = default_transport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.corporate_action_notices = corporate_action_notices or {}
        self.notice_transport = notice_transport
        self.official_provider = official_provider or NasdaqOfficialCloseProvider()
        self.action_provider = action_provider or YahooCorporateActionProvider()
        self.minute_provider = minute_provider or YahooMinuteCloseProvider()
        self.independent_providers = tuple(independent_providers or (
            PublicPreviousCloseProvider(),
            CapturedEndOfDayQuoteProvider(independent_snapshots or {}),
        ))
        self.now = now or (lambda: datetime.now(timezone.utc))

    def resolve(
        self,
        *,
        ticker: str,
        candidate: dict[str, Any],
        historical_series: DailySeries,
        previous: DailyBar,
        target: DailyBar,
        target_session_date: date,
        tolerance_pct: float,
    ) -> dict[str, Any]:
        official = self.official_provider.fetch(ticker, target_session_date, float(candidate["_close"]))
        official_previous_session = date.fromisoformat(str(official.get("previous_session_date")))
        vendor_previous_session_omitted = (
            previous.session_date < official_previous_session < target_session_date
        )
        if official_previous_session != previous.session_date and not vendor_previous_session_omitted:
            raise MarketDataError(f"Top20 {ticker}: official/history previous session mismatch")
        action = self.action_provider.fetch(ticker, target_session_date)
        notice_url = self.corporate_action_notices.get(ticker)
        split_announced = any("Stock Split" in str(item) for item in official.get("notifications", []))
        if split_announced and not notice_url:
            raise MarketDataError(f"Top20 {ticker}: official split notice URL required")
        if notice_url:
            try:
                validate_notice_url(notice_url)
                raw_notice = self.notice_transport(notice_url, {"User-Agent": "Mozilla/5.0"})
                notice = parse_exchange_split_notice(raw_notice, notice_url, ticker)
                validate_vendor_actions(action.get("events") or {}, notice)
            except ValueError as exc:
                raise MarketDataError(str(exc)) from exc
            action = {**action, "status": "corporate_action_adjusted", "official_action": notice}
            official["previous_close_raw"] = official["previous_close"]
            official["previous_close"] = previous_on_target_basis(
                official["previous_close"], date.fromisoformat(official["previous_session_date"]),
                target_session_date, notice,
            )
        minute_previous_session = (
            official_previous_session if vendor_previous_session_omitted else previous.session_date
        )
        minute = self.minute_provider.fetch(ticker, minute_previous_session, target_session_date)
        independent: list[dict[str, Any]] = []
        errors: list[str] = []
        for provider in self.independent_providers:
            try:
                independent.append(provider.fetch(ticker, target_session_date))
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        historical_change = (target.regular_close / previous.regular_close - 1.0) * 100.0
        try:
            resolved = resolve_discrepancy(
                candidate=candidate, historical_provider=historical_series.provider,
                historical_previous_close=previous.regular_close,
                historical_target_close=target.regular_close, historical_change_pct=historical_change,
                official=official, corporate_action=action, minute_close=minute,
                independent_sources=independent, tolerance_pct=tolerance_pct,
                resolved_at=self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            )
            if vendor_previous_session_omitted:
                if resolved["discrepancy_reason"] not in {
                    "stale_daily_bar", "non_nasdaq_primary_exchange_eod",
                }:
                    raise MarketDataError(
                        f"Top20 {ticker}: vendor previous-session omission was not independently resolved"
                    )
                resolved["discrepancy_reason"] = (
                    "vendor_omitted_previous_session_non_nasdaq_primary_exchange_eod"
                    if resolved["discrepancy_reason"] == "non_nasdaq_primary_exchange_eod"
                    else "vendor_omitted_previous_session"
                )
                resolved["basis_evidence"] = {
                    **resolved.get("basis_evidence", {}),
                    "vendor_previous_session_date": previous.session_date.isoformat(),
                    "official_previous_session_date": official_previous_session.isoformat(),
                    "official_previous_close": float(official["previous_close"]),
                    "vendor_previous_close": previous.regular_close,
                    "comparison_basis": "dated_regular_close",
                }
            return resolved
        except MarketDataError as exc:
            detail = f"; independent errors: {', '.join(errors)}" if errors else ""
            raise MarketDataError(f"Top20 {ticker} arbitration failed: {exc}{detail}") from exc

    def resolve_missing_previous(
        self,
        *,
        ticker: str,
        candidate: dict[str, Any],
        historical_series: DailySeries,
        target: DailyBar,
        target_session_date: date,
        tolerance_pct: float,
    ) -> dict[str, Any]:
        """Resolve a vendor history omission only around a verified future split."""
        notice_url = self.corporate_action_notices.get(ticker)
        if not notice_url:
            raise MarketDataError(f"Top20 {ticker}: official split notice URL required for missing history")
        try:
            validate_notice_url(notice_url)
            raw_notice = self.notice_transport(notice_url, {"User-Agent": "Mozilla/5.0"})
            notice = parse_exchange_split_notice(raw_notice, notice_url, ticker)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc
        effective = date.fromisoformat(notice["effective_session_date"])
        if effective <= target_session_date:
            raise MarketDataError(f"Top20 {ticker}: missing history is not before the official split effective date")

        official = self.official_provider.fetch(ticker, target_session_date, float(candidate["_close"]))
        previous_date = date.fromisoformat(official["previous_session_date"])
        if previous_date >= target_session_date:
            raise MarketDataError(f"Top20 {ticker}: official previous session is invalid")
        action = self.action_provider.fetch(ticker, target_session_date)
        try:
            validate_vendor_actions(action.get("events") or {}, notice)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc
        minute = self.minute_provider.fetch(ticker, previous_date, target_session_date)
        official_previous = float(official["previous_close"])
        official_target = float(official["target_close"])
        if not close_matches(target.regular_close, official_target):
            raise MarketDataError(f"Top20 {ticker}: remaining vendor target bar differs from official close")
        if not (
            official.get("target_close_verified")
            and minute.get("price_field") == "boundary_open"
            and close_matches(float(minute["previous_close"]), official_previous)
            and close_matches(float(minute["target_close"]), official_target)
        ):
            raise MarketDataError(f"Top20 {ticker}: official and boundary evidence do not agree")

        screener_change = float(candidate["_change_pct"])
        screener_last = float(candidate["_close"])
        screener_net = _numeric(candidate.get("netchange"), "screener net change", nullable=True)
        official_change = (official_target / official_previous - 1.0) * 100.0
        if abs(official_change - screener_change) > tolerance_pct or not close_matches(screener_last, official_target):
            raise MarketDataError(f"Top20 {ticker}: official market source does not support Screener")
        if screener_net is not None and abs((screener_last - official_previous) - screener_net) > 0.02:
            raise MarketDataError(f"Top20 {ticker}: Screener net change differs from official closes")

        independent_support: list[dict[str, Any]] = []
        errors: list[str] = []
        for provider in self.independent_providers:
            try:
                source = provider.fetch(ticker, target_session_date)
                source_previous = _numeric(source.get("previous_close"), "independent previous close", nullable=True)
                if source_previous is not None and abs(source_previous - official_previous) <= max(0.02, official_previous * 0.002):
                    independent_support.append(source)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        if not independent_support:
            detail = f"; independent errors: {', '.join(errors)}" if errors else ""
            raise MarketDataError(f"Top20 {ticker}: no independent supporting source{detail}")

        resolved_at = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        volume = _numeric(candidate.get("volume"), "screener volume", nullable=True)
        liquidity_flag = "low_liquidity" if volume is not None and volume < 250_000 else "normal_liquidity"
        sources = [
            {
                "provider": official["provider"], "provider_family": official.get("provider_family", "nasdaq"),
                "source_identifier": official.get("source_identifiers"),
                "raw_response_sha256": official.get("raw_response_sha256"), "role": "official_market_source",
            },
            {
                "provider": action["provider"], "provider_family": action.get("provider_family"),
                "source_identifier": action.get("source_identifier"),
                "raw_response_sha256": action.get("raw_response_sha256"), "role": "corporate_action_check",
            },
            {
                **notice, "provider": "nasdaq_corporate_action_notice", "provider_family": "nasdaq",
                "role": "official_corporate_action",
            },
            {
                "provider": minute["provider"], "provider_family": minute.get("provider_family"),
                "source_identifier": minute.get("source_identifier"),
                "raw_response_sha256": minute.get("raw_response_sha256"), "role": "missing_history_diagnosis",
            },
            {
                "provider": historical_series.provider,
                "provider_family": provider_family(historical_series.provider),
                "source_identifier": historical_series.source_identifier,
                "raw_response_sha256": historical_series.raw_response_sha256,
                "role": "missing_history_diagnosis",
            },
        ]
        sources.extend({
            "provider": item["provider"], "provider_family": item.get("provider_family"),
            "source_identifier": item.get("source_identifier"),
            "raw_response_sha256": item.get("raw_response_sha256"), "role": "independent_support",
            "raw_value": item.get("raw_value"), "parsed_value": item.get("parsed_value"),
        } for item in independent_support)
        return {
            "discrepancy_status": "resolved",
            "discrepancy_reason": "future_corporate_action_vendor_history_omission",
            "basis_evidence": {
                "vendor_history_dates": [bar.session_date.isoformat() for bar in historical_series.bars],
                "official_previous_session_date": previous_date.isoformat(),
                "official_split_effective_session_date": effective.isoformat(),
                "comparison_basis": "pre_action_regular_close",
            },
            "screener_change_pct": screener_change,
            "screener_last_sale": screener_last,
            "screener_net_change": screener_net,
            "screener_implied_previous_close": screener_last / (1.0 + screener_change / 100.0),
            "historical_previous_close": None,
            "historical_target_close": target.regular_close,
            "historical_change_pct": official_change,
            "official_previous_close": official_previous,
            "official_target_close": official_target,
            "corporate_action_status": "official_future_action_verified",
            "liquidity_flag": liquidity_flag,
            "compared_providers": sorted({
                "nasdaq", provider_family(historical_series.provider),
                *(provider_family(str(item.get("provider_family") or item["provider"])) for item in independent_support),
            }),
            "supporting_sources": sources,
            "resolved_at": resolved_at,
        }

def eligible_screener_row(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    name = str(row.get("name") or "").strip()
    if not symbol or not name or EXCLUDED_INSTRUMENT_RE.search(name):
        return False
    if row.get("instrument_type") == "common_stock":
        return True
    # Nasdaq ordinary/common/class shares and foreign ordinary shares/ADS/ADR are eligible.
    allowed = re.search(r"(?i)(common|ordinary|american depositary|ADS\b|ADR\b)", name)
    return bool(allowed)


def issuer_total_market_cap(share_class_components: list[dict[str, Any]]) -> float:
    if not share_class_components:
        raise MarketDataError("issuer-total market cap needs share-class components")
    total = 0.0
    for component in share_class_components:
        price = _numeric(component.get("price"), "share class price")
        shares = _numeric(component.get("shares_outstanding"), "shares outstanding")
        if price < 0 or shares < 0:
            raise MarketDataError("share class inputs must be non-negative")
        total += price * shares
    return total


def rank_top20(
    screener: dict[str, Any],
    *,
    target_session_date: date,
    historical_series: dict[str, DailySeries] | None = None,
    issuer_components: dict[str, list[dict[str, Any]]] | None = None,
    discrepancy_arbitrator: DiscrepancyArbitrator | None = None,
    mismatch_tolerance_pct: float = 0.20,
    excluded_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Filter and rank Nasdaq rows, optionally fail-closing on history mismatch."""
    issuer_components = issuer_components or {}
    candidates: list[dict[str, Any]] = []
    for raw in screener["rows"]:
        if not isinstance(raw, dict) or not eligible_screener_row(raw):
            continue
        try:
            change_pct = _numeric(raw.get("pctchange"), "pctchange")
            close = _numeric(raw.get("lastsale"), "lastsale")
        except MarketDataError:
            continue
        candidates.append({**raw, "_change_pct": change_pct, "_close": close})
    candidates.sort(key=lambda row: row["_change_pct"], reverse=True)

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        ticker = str(candidate["symbol"]).upper()
        review_flags: list[str] = []
        historical_change: float | None = None
        discrepancy: dict[str, Any] = {
            "discrepancy_status": "not_applicable", "discrepancy_reason": None,
            "compared_providers": [], "official_previous_close": None,
            "official_target_close": None, "supporting_sources": [], "resolved_at": None,
            "liquidity_flag": None, "corporate_action_status": None,
        }
        if historical_series is not None:
            series = historical_series.get(ticker)
            if series is None:
                raise MarketDataError(f"Top20 candidate {ticker} lacks historical cross-check")
            try:
                previous, target = completed_session_pair(series, target_session_date)
            except MarketDataError as exc:
                target_bars = [bar for bar in series.bars if bar.session_date == target_session_date]
                missing_resolver = getattr(discrepancy_arbitrator, "resolve_missing_previous", None)
                if "previous completed session is missing" not in str(exc) or len(target_bars) != 1 or missing_resolver is None:
                    raise
                target = target_bars[0]
                discrepancy = missing_resolver(
                    ticker=ticker, candidate=candidate, historical_series=series,
                    target=target, target_session_date=target_session_date,
                    tolerance_pct=mismatch_tolerance_pct,
                )
                historical_change = float(discrepancy["historical_change_pct"])
                review_flags.extend(["discrepancy_resolved", str(discrepancy["discrepancy_reason"])])
                if discrepancy.get("liquidity_flag") == "low_liquidity":
                    review_flags.append("low_liquidity")
            else:
                historical_change = (target.regular_close / previous.regular_close - 1.0) * 100.0
            if target.volume == 0:
                if excluded_candidates is not None:
                    excluded_candidates.append({
                        "ticker": ticker,
                        "reason": "zero_volume_target_bar",
                        "session_date": target_session_date.isoformat(),
                        "provider": series.provider,
                        "source_identifier": series.source_identifier,
                        "raw_response_sha256": series.raw_response_sha256,
                    })
                continue
            if abs(historical_change - candidate["_change_pct"]) > mismatch_tolerance_pct:
                # A large screener move that vanishes in split-adjusted history is
                # treated as a possible reverse split and excluded, never silently used.
                if abs(candidate["_change_pct"]) >= 30 and abs(historical_change) <= 5:
                    continue
                if discrepancy_arbitrator is None:
                    raise MarketDataError(
                        f"Top20 {ticker} screener/history mismatch: {candidate['_change_pct']:.3f} vs {historical_change:.3f}"
                    )
                if discrepancy["discrepancy_status"] == "not_applicable":
                    discrepancy = discrepancy_arbitrator.resolve(
                        ticker=ticker, candidate=candidate, historical_series=series,
                        previous=previous, target=target, target_session_date=target_session_date,
                        tolerance_pct=mismatch_tolerance_pct,
                    )
                    review_flags.extend(["discrepancy_resolved", str(discrepancy["discrepancy_reason"])])
                    if discrepancy.get("liquidity_flag") == "low_liquidity":
                        review_flags.append("low_liquidity")
            if abs(target.regular_close - candidate["_close"]) > max(0.02, target.regular_close * 0.005):
                raise MarketDataError(f"Top20 {ticker} screener close is stale or non-regular")
        vendor_cap = _numeric(candidate.get("marketCap"), "marketCap", nullable=True)
        if vendor_cap == 0:
            vendor_cap = None
        components = issuer_components.get(ticker, [])
        if components:
            market_cap = issuer_total_market_cap(components)
            if len(components) > 1:
                market_cap_method = "issuer_total_dual_class"
            elif re.search(r"(?i)American Depositary|ADS\b|ADR\b", str(candidate.get("name"))):
                market_cap_method = "issuer_total_ads"
            else:
                market_cap_method = "issuer_total_single_class"
        else:
            market_cap = vendor_cap
            market_cap_method = "vendor_market_cap" if market_cap is not None else "unavailable"
            if re.search(r"(?i)Class [A-Z]|American Depositary|ADS\b|ADR\b", str(candidate.get("name"))):
                review_flags.append("issuer_total_market_cap_review")
        ranked.append({
            "rank": len(ranked) + 1,
            "ticker": ticker,
            "company_name": str(candidate["name"]).strip(),
            "close": candidate["_close"],
            "change_pct": candidate["_change_pct"],
            "historical_change_pct": historical_change,
            "market_cap": market_cap,
            "market_cap_method": market_cap_method,
            "share_class_components": components,
            "provider": screener["provider"],
            "source_identifier": f"{screener['source_identifier']}#{ticker}",
            "retrieved_at": screener["retrieved_at"],
            "raw_response_sha256": screener["raw_response_sha256"],
            "session_date": target_session_date.isoformat(),
            "price_basis": "regular_close",
            "adjusted": False,
            "review_flags": review_flags,
            **discrepancy,
        })
        if len(ranked) == 20:
            return ranked
    raise MarketDataError(f"only {len(ranked)} eligible Top20 records remained")


def build_canonical_market_data_packet(
    target_session_date: date,
    *,
    historical_providers: Iterable[HistoricalProvider] | None = None,
    issuer_components: dict[str, list[dict[str, Any]]] | None = None,
    screener_provider: NasdaqScreenerProvider | None = None,
    discrepancy_arbitrator: DiscrepancyArbitrator | None = None,
    candidate_limit: int = 60,
) -> dict[str, Any]:
    """Build the sole deterministic market-data input consumed by report generation."""
    providers = tuple(historical_providers or (
        YahooChartProvider(host="query1.finance.yahoo.com"),
        YahooChartProvider(host="query2.finance.yahoo.com"),
        YahooChartProvider(host="query1.finance.yahoo.com", closed_meta_fallback=True),
        YahooChartProvider(host="query2.finance.yahoo.com", closed_meta_fallback=True),
    ))
    snapshot = build_index_sector_snapshot(providers, target_session_date)
    if screener_provider is not None:
        screener = screener_provider.fetch()
    else:
        try:
            screener = StockAnalysisGainersProvider().fetch(target_session_date)
        except Exception:
            screener = NasdaqScreenerProvider().fetch()
    symbols = screener_candidate_symbols(screener, limit=candidate_limit)
    historical = fetch_all_or_fallback(
        providers, symbols, target_session_date - timedelta(days=10), target_session_date,
    )
    excluded_candidates: list[dict[str, Any]] = []
    top20 = rank_top20(
        screener,
        target_session_date=target_session_date,
        historical_series=historical.series,
        issuer_components=issuer_components,
        discrepancy_arbitrator=discrepancy_arbitrator or LiveDiscrepancyArbitrator(),
        excluded_candidates=excluded_candidates,
    )
    provider_names = {
        snapshot["source"]["name"], screener["provider"], historical.provider,
    }
    raw_hashes = {screener["raw_response_sha256"]}
    for item in (*snapshot["indexes"], *snapshot["sectors"], *top20):
        provider = item.get("provider")
        if provider:
            provider_names.add(str(provider))
        raw_hash = item.get("raw_response_sha256")
        if isinstance(raw_hash, str):
            raw_hashes.add(raw_hash)
        for source in item.get("supporting_sources", []):
            provider = source.get("provider")
            if provider:
                provider_names.add(str(provider))
            hashes = source.get("raw_response_sha256")
            hashes = hashes if isinstance(hashes, list) else [hashes]
            raw_hashes.update(value for value in hashes if isinstance(value, str))
    generated_at = max(
        snapshot["source"]["retrieved_at"], screener["retrieved_at"],
        *(item.retrieved_at for item in historical.series.values()),
    )
    return {
        **snapshot,
        "market_data_contract_version": MARKET_DATA_CONTRACT_VERSION,
        "market_data_generated_at": generated_at,
        "providers": sorted(provider_names),
        "discrepancy_count": sum(item["discrepancy_status"] != "not_applicable" for item in top20),
        "top_gainers_20": top20,
        "excluded_no_trade_candidates": excluded_candidates,
        "screener": {key: screener[key] for key in (
            "provider", "source_identifier", "retrieved_at", "raw_response_sha256",
        )},
        "top20_history_provider_attempts": [attempt.__dict__ for attempt in historical.attempts],
        "raw_response_hashes": sorted(raw_hashes),
    }


def screener_candidate_symbols(screener: dict[str, Any], *, limit: int = 60) -> list[str]:
    """Return the highest screener candidates for deterministic history checks."""
    candidates: list[tuple[float, str]] = []
    for raw in screener.get("rows", []):
        if not isinstance(raw, dict) or not eligible_screener_row(raw):
            continue
        try:
            change_pct = _numeric(raw.get("pctchange"), "pctchange")
            _numeric(raw.get("lastsale"), "lastsale")
        except MarketDataError:
            continue
        candidates.append((change_pct, str(raw["symbol"]).strip().upper()))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [symbol for _, symbol in candidates[:limit]]
