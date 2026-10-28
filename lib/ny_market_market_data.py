"""Deterministic acquisition of canonical NY daily market data.

Acquisition is deliberately separate from ``ny_market_research`` validation.
All prices in this module are unadjusted regular-session daily closes.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import time as time_module

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
    """Retry only transient transport failures, with a fixed upper bound."""
    for attempt in range(3):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=30) as response:
                raw = response.read()
            evidence_dir = os.environ.get("NY_MARKET_EVIDENCE_DIR")
            if evidence_dir:
                folder = Path(evidence_dir)
                folder.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(raw).hexdigest()
                raw_path = folder / f"{digest}.raw"
                if not raw_path.exists():
                    temporary = folder / f".{digest}.{os.getpid()}.tmp"
                    temporary.write_bytes(raw)
                    os.replace(temporary, raw_path)
                with (folder / "responses.jsonl").open("a", encoding="utf-8") as index:
                    index.write(json.dumps({"url": url, "sha256": digest,
                                            "retrieved_at": datetime.now(timezone.utc).isoformat()},
                                           ensure_ascii=False) + "\n")
                return raw
            return raw
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time_module.sleep((0.3, 0.7)[attempt])
    raise AssertionError("unreachable transport retry state")


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
    API_URL = "https://stockanalysis.com/_api/endpoints/screener/table"
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
        params = urlencode({
            "type": "s", "m": "change", "s": "desc",
            "c": "no,s,n,change,price,volume,marketCap,priceDate",
            "cn": 50, "f": "change-over-0,priceDate-isLastTradingDay",
            "p": 1, "i": "stock-movers",
        })
        url = f"{self.API_URL}?{params}"
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        )
        try:
            raw = self.transport(url, {
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": STOCKANALYSIS_GAINERS_URL,
            })
            payload = _raw_json(raw, self.name)
        except (MarketDataError, HTTPError, URLError, TimeoutError):
            # The public page remains the established fallback. It may contain
            # only twenty rows; rank_top20 will stop if exclusions exhaust it.
            raw = self.transport(STOCKANALYSIS_GAINERS_URL, {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            return self._parse_html(raw, target_session_date)
        data = payload.get("data")
        api_rows = data.get("data") if isinstance(data, dict) else None
        result_count = data.get("resultsCount") if isinstance(data, dict) else None
        if payload.get("status") != 200 or not isinstance(api_rows, list) or not isinstance(result_count, int):
            raise MarketDataError("StockAnalysis gainers API returned an invalid response")
        if len(api_rows) != min(50, result_count) or len(api_rows) < 20:
            raise MarketDataError("StockAnalysis gainers API returned an incomplete candidate universe")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        previous_change = float("inf")
        stale_rows: list[dict[str, str]] = []
        for rank, item in enumerate(api_rows, 1):
            if not isinstance(item, dict) or item.get("no") != rank:
                raise MarketDataError("StockAnalysis gainers API returned unordered ranks")
            session_date = date.fromisoformat(str(item.get("priceDate")))
            symbol = str(item.get("s") or "").strip().upper()
            if session_date != target_session_date:
                stale_rows.append({"symbol": symbol, "price_date": session_date.isoformat()})
                continue
            name = str(item.get("n") or "").strip()
            change = _numeric(item.get("change"), "StockAnalysis change")
            price = _numeric(item.get("price"), "StockAnalysis price")
            if not symbol or not name or symbol in seen or price <= 0 or change > previous_change:
                raise MarketDataError("StockAnalysis gainers API returned an invalid or unsorted row")
            seen.add(symbol)
            previous_change = change
            rows.append({
                "symbol": symbol,
                "name": name,
                "lastsale": f'${price}',
                "netchange": None,
                "pctchange": f'{change}% ',
                "volume": item.get("volume"),
                "marketCap": item.get("marketCap"),
                "instrument_type": "common_stock",
            })
        if len(rows) < 20:
            raise MarketDataError(
                f"StockAnalysis gainers table has only {len(rows)} rows for {target_session_date}"
            )
        return {
            "provider": self.name,
            "source_identifier": url,
            "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "rows": rows,
            "excluded_stale_rows": stale_rows,
        }

    def _parse_html(self, raw: bytes, target_session_date: date) -> dict[str, Any]:
        matches = list(self._ROW_RE.finditer(raw.decode("utf-8")))
        if len(matches) < 20:
            raise MarketDataError("StockAnalysis gainers table returned fewer than 20 rows")
        rows = []
        stale_rows: list[dict[str, str]] = []
        for match in matches:
            session_date = date.fromisoformat(match.group("date"))
            if session_date != target_session_date:
                stale_rows.append({"symbol": match.group("symbol"), "price_date": session_date.isoformat()})
                continue
            rows.append({
                "symbol": match.group("symbol"),
                "name": json.loads(f'"{match.group("name")}"'),
                "lastsale": f'${match.group("price")}',
                "netchange": None,
                "pctchange": f'{match.group("change")}% ',
                "volume": match.group("volume"),
                "marketCap": None if match.group("market_cap") == "null" else match.group("market_cap"),
                "instrument_type": "common_stock",
            })
        if len(rows) < 20:
            raise MarketDataError(
                f"StockAnalysis gainers table has only {len(rows)} rows for {target_session_date}"
            )
        return {
            "provider": self.name,
            "source_identifier": STOCKANALYSIS_GAINERS_URL,
            "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "rows": rows,
            "excluded_stale_rows": stale_rows,
        }


class StockAnalysisHistoricalCloseProvider:
    """Independent, dated closes; this page explicitly normalizes past splits."""

    name = "stockanalysis_historical_s_p_global"

    def __init__(self, *, transport: Transport = default_transport) -> None:
        self.transport = transport

    def fetch_pair(self, symbol: str, previous_session: date, target_session: date) -> dict[str, Any]:
        from bs4 import BeautifulSoup

        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise MarketDataError("StockAnalysis history symbol is invalid")
        url = f"https://stockanalysis.com/stocks/{symbol.lower()}/history/"
        raw = self.transport(url, {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        soup = BeautifulSoup(raw, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        if "past prices are adjusted for stock splits" not in page_text.lower():
            raise MarketDataError(f"StockAnalysis {symbol}: split basis disclosure missing")
        values: dict[date, float] = {}
        raw_values: dict[date, str] = {}
        target_change_raw: str | None = None
        for table in soup.find_all("table"):
            headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
            if "Date" not in headers or "Close" not in headers:
                continue
            close_index = headers.index("Close")
            change_index = headers.index("Change") if "Change" in headers else None
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) <= close_index:
                    continue
                try:
                    day = datetime.strptime(cells[0].get_text(" ", strip=True), "%b %d, %Y").date()
                except ValueError:
                    continue
                if day in {previous_session, target_session}:
                    raw_close = cells[close_index].get_text(" ", strip=True)
                    price = parse_market_numeric_token(raw_close, "dated close")
                    if price <= 0 or day in values:
                        raise MarketDataError(f"StockAnalysis {symbol}: invalid or duplicate dated close")
                    values[day] = price
                    raw_values[day] = raw_close
                    if day == target_session and change_index is not None and len(cells) > change_index:
                        target_change_raw = cells[change_index].get_text(" ", strip=True)
        if set(values) != {previous_session, target_session}:
            raise MarketDataError(f"StockAnalysis {symbol}: required historical sessions missing")
        decimal_places = {}
        for day in (previous_session, target_session):
            raw_number = raw_values[day].strip().replace("$", "").replace(",", "")
            match = re.fullmatch(r"\d+(?:\.(\d+))?", raw_number)
            if match is None:
                raise MarketDataError(f"StockAnalysis {symbol}: dated close precision is ambiguous")
            decimal_places[day] = len(match.group(1) or "")
        return {
            "provider": self.name,
            "provider_family": "s_p_global",
            "source_identifier": url,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "previous_session_date": previous_session.isoformat(),
            "target_session_date": target_session.isoformat(),
            "previous_split_normalized_close": values[previous_session],
            "target_regular_close": values[target_session],
            "previous_raw_close": raw_values[previous_session],
            "target_raw_close": raw_values[target_session],
            "previous_display_decimal_places": decimal_places[previous_session],
            "target_display_decimal_places": decimal_places[target_session],
            "reported_change_raw": target_change_raw,
            "price_basis": "regular_close_normalized_for_split",
        }


def verify_corporate_action_evidence(
    ticker: str, entry: dict[str, Any], *, transport: Transport = default_transport,
) -> dict[str, Any]:
    """Verify a structured action against an issuer's SEC primary document."""
    from bs4 import BeautifulSoup

    if not isinstance(entry, dict):
        raise MarketDataError(f"Top20 {ticker}: corporate-action evidence must be an object")
    event_type = entry.get("event_type")
    if event_type not in {"reverse_split", "forward_split", "ticker_change", "business_combination", "new_listing"}:
        raise MarketDataError(f"Top20 {ticker}: unsupported corporate-action event")
    source_url = str(entry.get("source_url") or "")
    parsed = urlsplit(source_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "sec.gov"}
            or not parsed.path.startswith("/Archives/edgar/") or parsed.username or parsed.password):
        raise MarketDataError(f"Top20 {ticker}: SEC primary-source URL required")
    try:
        effective = date.fromisoformat(str(entry["effective_session_date"]))
    except (KeyError, ValueError) as exc:
        raise MarketDataError(f"Top20 {ticker}: invalid action effective date") from exc
    prior_ticker = str(entry.get("prior_ticker") or "").strip().upper()
    if event_type != "new_listing" and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", prior_ticker):
        raise MarketDataError(f"Top20 {ticker}: prior ticker required")
    exchange = str(entry.get("exchange") or "").strip()
    if not exchange:
        raise MarketDataError(f"Top20 {ticker}: target exchange required")
    raw = transport(source_url, {"User-Agent": "NYMarketReport/1.0 research@example.com"})
    source_text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True).split())
    upper = source_text.upper()
    if (not re.search(rf"\b{re.escape(ticker.upper())}\b", upper)
            or (prior_ticker and not re.search(rf"\b{re.escape(prior_ticker)}\b", upper))
            or exchange.upper() not in upper):
        raise MarketDataError(f"Top20 {ticker}: source does not confirm ticker identity and exchange")
    effective_text = f"{effective.strftime('%B')} {effective.day}, {effective.year}".upper()
    date_confirmation: dict[str, str] = {}
    if effective_text not in upper:
        confirmation_url = str(entry.get("date_confirmation_url") or "")
        confirmation = urlsplit(confirmation_url)
        if (event_type != "new_listing" or confirmation.scheme != "https"
                or confirmation.hostname != "www.nasdaq.com"
                or not confirmation.path.startswith("/events/")
                or confirmation.username or confirmation.password):
            raise MarketDataError(f"Top20 {ticker}: source does not confirm effective date")
        confirmation_raw = transport(confirmation_url, {"User-Agent": "Mozilla/5.0"})
        confirmation_text = " ".join(BeautifulSoup(
            confirmation_raw, "html.parser",
        ).get_text(" ", strip=True).split()).upper()
        if (effective_text not in confirmation_text
                or not re.search(rf"\b{re.escape(ticker.upper())}\b", confirmation_text)
                or "OPENING BELL" not in confirmation_text):
            raise MarketDataError(f"Top20 {ticker}: Nasdaq event does not confirm listing date")
        date_confirmation = {
            "date_confirmation_url": confirmation_url,
            "date_confirmation_sha256": hashlib.sha256(confirmation_raw).hexdigest(),
        }
    if event_type == "business_combination" and "BUSINESS COMBINATION" not in upper:
        raise MarketDataError(f"Top20 {ticker}: source does not confirm business combination")
    if event_type == "new_listing" and not any(
        term in upper for term in (
            "INITIAL PUBLIC OFFERING", "FIRST DAY OF TRADING", "BEGIN TRADING",
            "COMPLETES NASDAQ LISTING", "TRADING ON THE NASDAQ",
        )
    ):
        raise MarketDataError(f"Top20 {ticker}: source does not confirm new listing")
    if event_type in {"reverse_split", "forward_split"}:
        try:
            new_shares = int(entry["new_shares"])
            old_shares = int(entry["old_shares"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Top20 {ticker}: split ratio missing") from exc
        if (new_shares <= 0 or old_shares <= 0 or new_shares == old_shares
                or (event_type == "reverse_split" and new_shares >= old_shares)
                or (event_type == "forward_split" and new_shares <= old_shares)):
            raise MarketDataError(f"Top20 {ticker}: invalid split ratio")
        ratio_patterns = (
            rf"\b{new_shares}\s*[- ]?for\s*[- ]?{old_shares}\b",
            rf"\b{new_shares}\s*[:/]\s*{old_shares}\b",
        )
        if ("SPLIT" not in upper or not any(re.search(pattern, upper, re.I) for pattern in ratio_patterns)):
            raise MarketDataError(f"Top20 {ticker}: SEC source does not confirm split ratio")
    else:
        new_shares = old_shares = 1
    return {
        "event_type": event_type, "symbol": ticker.upper(), "prior_ticker": prior_ticker or None,
        "effective_session_date": effective.isoformat(), "exchange": exchange,
        "new_shares": new_shares, "old_shares": old_shares,
        "source_identifier": source_url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        **date_confirmation,
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

    def __init__(self, *, transport: Transport = default_transport, now: Callable[[], datetime] | None = None,
                 corporate_action_notices: dict[str, str] | None = None,
                 notice_transport: Transport = default_transport) -> None:
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.corporate_action_notices = corporate_action_notices or {}
        self.notice_transport = notice_transport

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
        historical_data = historical.get("data") or {}
        rows = (historical_data.get("tradesTable") or {}).get("rows")
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
        top_rows = ((nls.get("data") or {}).get("topTable") or {}).get("rows")
        nls_previous: float | None = None
        if isinstance(top_rows, list) and top_rows and isinstance(top_rows[0], dict):
            nls_previous = _numeric(top_rows[0].get("previousClose"), "NLS previous close", nullable=True)
        # Read and validate the exchange notice BEFORE comparing unlike share bases.
        notice = None
        normalization = {}
        notice_url = self.corporate_action_notices.get(symbol)
        if notice_url:
            notice = parse_exchange_split_notice(
                self.notice_transport(notice_url, {"User-Agent": "Mozilla/5.0"}), notice_url, symbol,
            )
            effective = date.fromisoformat(notice["effective_session_date"])
            if previous_day < effective <= target_session_date:
                normalized = previous_on_target_basis(previous_close, previous_day, target_session_date, notice)
                # Use the exchange's row-level split marker as well as its dated
                # live quote. A numerical match alone does not establish basis.
                volume_marker = str(previous_row.get("volume")).strip().upper()
                provider_marks_adjusted = volume_marker == "N/A"
                if target_session_date == self.now().astimezone(NY_TZ).date() and nls_previous is not None:
                    if close_matches(nls_previous, previous_close) and provider_marks_adjusted:
                        basis = "already_split_normalized"
                    elif close_matches(nls_previous, normalized) and not provider_marks_adjusted:
                        basis = "unadjusted"
                    else:
                        raise MarketDataError(f"{self.name}:{symbol}: official split/NLS basis unresolved")
                else:
                    if not provider_marks_adjusted:
                        raise MarketDataError(f"{self.name}:{symbol}: historical split basis needs dated corroboration")
                    basis = "already_split_normalized"
                normalization = {"previous_close_raw": previous_close, "previous_price_basis": basis,
                                 "applied_split_factor": notice["old_shares"] / notice["new_shares"] if basis == "unadjusted" else 1.0,
                                 "official_action": notice,
                                 "historical_volume_basis_marker": volume_marker}
                if basis == "unadjusted":
                    previous_close = normalized
                normalization["previous_close_on_target_basis"] = True
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
            **normalization,
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
            if (len(labels) >= 6 and re.fullmatch(r"Date \(\s*E[DS]T\s*\)", labels[0])
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
    vendor_previous_session_omitted: bool = False,
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

    # Same-day exchange splits: official daily history and NLS must first be
    # on one share basis. Diagnose vendor last-trade/rounding differences only
    # with a separate dated EOD pair, never with the 16:00 bar close.
    notice = corporate_action.get("official_action")
    if reason is None and notice and official.get("previous_close_on_target_basis"):
        effective = date.fromisoformat(notice["effective_session_date"])
        previous_session = date.fromisoformat(official["previous_session_date"])
        target_session = date.fromisoformat(official["target_session_date"])
        dated_support = [s for s in independent_support if (
            s.get("session") == "regular_close" and s.get("target_session_date") == target_session.isoformat()
            and s.get("target_close") is not None and close_matches(float(s["target_close"]), official_target)
            and s.get("previous_close") is not None
            and abs(float(s["previous_close"]) - official_previous) <= 0.5 * 10 ** -int(s.get("display_decimal_places",8)) + 1e-12
        )]
        daily_matches_previous_trade = (minute_close and minute_close.get("previous_last_regular_bar_close") is not None
            and close_matches(historical_previous_close,float(minute_close["previous_last_regular_bar_close"])))
        daily_matches_rounded_previous = any(close_matches(historical_previous_close,round_market_display(official_previous,d)) for d in range(2,5))
        if (previous_session < effective <= target_session and dated_support and official_supports
                and close_matches(historical_target_close,official_target)
                and (daily_matches_previous_trade or daily_matches_rounded_previous)):
            reason = "official_split_normalized_eod"
            basis_evidence = {"official_action":notice,"official_previous_close_raw":official.get("previous_close_raw"),
                "official_previous_price_basis":official.get("previous_price_basis"),
                "applied_split_factor":official.get("applied_split_factor"),
                "official_previous_close":official_previous,"official_target_close":official_target,
                "vendor_daily_previous_close":historical_previous_close,
                "vendor_daily_basis":"already_split_normalized_last_regular_trade" if daily_matches_previous_trade else "already_split_normalized_rounded_close",
                "independent_eod":dated_support,"comparison_basis":"dated_regular_closes_on_target_share_basis"}

    if reason is None and vendor_previous_session_omitted and action_status == "checked_none":
        dated_eod_support = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("previous_close") is not None
            and source.get("target_close") is not None
        )]
        if (
            official_supports
            and official.get("target_close_verified") is True
            and official.get("target_close_source") in {"nasdaq_info", "nasdaq_historical"}
            and dated_eod_support
            and abs(historical_target_close - official_target)
            <= max(0.02, official_target * 0.002)
        ):
            reason = "vendor_omitted_previous_session_with_independent_eod"
            basis_evidence = {
                "official_previous_close": official_previous,
                "official_target_close": official_target,
                "vendor_previous_close": historical_previous_close,
                "vendor_target_close": historical_target_close,
                "independent_eod": [{
                    "provider_family": source.get("provider_family"),
                    "source_identifier": source.get("source_identifier"),
                    "raw_response_sha256": source.get("raw_response_sha256"),
                } for source in dated_eod_support],
                "comparison_basis": "dated_official_and_independent_regular_closes",
            }

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
        previous_boundary_differs = not close_matches(
            float(minute_close["previous_close"]), official_previous,
        )
        target_boundary_differs = not close_matches(
            float(minute_close["target_close"]), official_target,
        )
        if (dated_closed and eod_support and rounded_vendor_previous
                and rounded_vendor_target
                and (previous_boundary_differs or target_boundary_differs)):
            reason = "official_closed_eod_with_secondary_boundary_difference"
            basis_evidence = {
                "official_previous_close": official_previous,
                "vendor_daily_previous_close": historical_previous_close,
                "official_target_close": official_target,
                "vendor_daily_target_close": historical_target_close,
                "vendor_previous_rounding_places": [digits for digits in range(2, 5) if close_matches(historical_previous_close, round_market_display(official_previous, digits))],
                "vendor_target_rounding_places": [digits for digits in range(2, 5) if close_matches(historical_target_close, round_market_display(official_target, digits))],
                "secondary_boundary_open": float(minute_close["target_close"]),
                "secondary_previous_boundary_open": float(minute_close["previous_close"]),
                "secondary_boundary_mismatch_sessions": [
                    session for session, differs in (
                        ("previous", previous_boundary_differs),
                        ("target", target_boundary_differs),
                    ) if differs
                ],
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

    conflicting_eod = [
        {
            "provider": source.get("provider"),
            "provider_family": source.get("provider_family"),
            "previous_close": source.get("previous_close"),
            "target_close": source.get("target_close"),
            "source_identifier": source.get("source_identifier"),
            "raw_response_sha256": source.get("raw_response_sha256"),
        }
        for source in independent_sources
        if source.get("session") == "regular_close"
        and source.get("target_session_date") == official.get("target_session_date")
        and source.get("target_close") is not None
        and abs(float(source["target_close"]) - official_target) > max(0.02, official_target * 0.002)
    ]
    if conflicting_eod:
        basis_evidence = {**basis_evidence, "conflicting_independent_eod": conflicting_eod}

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
        corporate_action_evidence: dict[str, dict[str, Any]] | None = None,
        notice_transport: Transport = default_transport,
        predecessor_provider: HistoricalProvider | None = None,
        split_history_provider: StockAnalysisHistoricalCloseProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.corporate_action_notices = corporate_action_notices or {}
        self.corporate_action_evidence = corporate_action_evidence or {}
        self.notice_transport = notice_transport
        self.predecessor_provider = predecessor_provider or YahooChartProvider()
        self.split_history_provider = split_history_provider or StockAnalysisHistoricalCloseProvider()
        self.official_provider = official_provider or NasdaqOfficialCloseProvider(
            corporate_action_notices=self.corporate_action_notices, notice_transport=notice_transport,
        )
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
        expected_previous_session_date: date | None = None,
        source_top20_floor_pct: float | None = None,
    ) -> dict[str, Any]:
        evidence = self.corporate_action_evidence.get(ticker)
        if evidence is not None:
            return self._resolve_documented_missing_previous(
                ticker=ticker, candidate=candidate, historical_series=historical_series,
                target=target, target_session_date=target_session_date,
                expected_previous_session_date=expected_previous_session_date,
                source_top20_floor_pct=source_top20_floor_pct,
                tolerance_pct=tolerance_pct, evidence=evidence,
            )
        try:
            official = self.official_provider.fetch(ticker, target_session_date, float(candidate["_close"]))
        except Exception as exc:
            raise MarketDataError(f"Top20 {ticker}: official close acquisition failed: {exc}") from exc
        official_previous_session = date.fromisoformat(str(official.get("previous_session_date")))
        vendor_previous_session_omitted = (
            previous.session_date < official_previous_session < target_session_date
        )
        if official_previous_session != previous.session_date and not vendor_previous_session_omitted:
            raise MarketDataError(f"Top20 {ticker}: official/history previous session mismatch")
        action = self.action_provider.fetch(ticker, target_session_date)
        if source_top20_floor_pct is not None and expected_previous_session_date is not None:
            official_change = (float(official["target_close"]) / float(official["previous_close"]) - 1.0) * 100.0
            if (float(candidate["_change_pct"]) >= source_top20_floor_pct
                    and official_change < source_top20_floor_pct - tolerance_pct
                    and official_previous_session == expected_previous_session_date
                    and official["target_session_date"] == target_session_date.isoformat()
                    and action.get("status") == "checked_none"
                    and abs(float(official["previous_close"]) - previous.regular_close) <= max(0.02, previous.regular_close * 0.002)
                    and abs(float(official["target_close"]) - target.regular_close) <= max(0.02, target.regular_close * 0.002)
                    and abs(float(official["target_close"]) - float(candidate["_close"])) <= max(0.02, float(candidate["_close"]) * 0.002)):
                independent = []
                for provider in self.independent_providers:
                    try:
                        independent.append(provider.fetch(ticker, target_session_date))
                    except (MarketDataError, HTTPError, URLError, TimeoutError):
                        continue
                independent_previous = [source for source in independent if (
                    provider_family(str(source.get("provider_family") or source.get("provider") or ""))
                    not in {"nasdaq", provider_family(historical_series.provider)}
                    and source.get("previous_close") is not None
                    and abs(float(source["previous_close"]) - float(official["previous_close"]))
                    <= max(0.02, float(official["previous_close"]) * 0.002)
                )]
                if independent_previous:
                    return {
                        "ranking_disposition": "exclude_below_top20_floor",
                        "exclusion_reason": "screener_change_conflicts_with_verified_regular_closes",
                        "canonical_change_pct": official_change,
                        "screener_change_pct": float(candidate["_change_pct"]),
                        "source_top20_floor_pct": source_top20_floor_pct,
                        "supporting_sources": [
                            {**official, "role": "dated_official_regular_closes"},
                            {"provider": historical_series.provider, "provider_family": provider_family(historical_series.provider),
                             "source_identifier": historical_series.source_identifier,
                             "raw_response_sha256": historical_series.raw_response_sha256, "role": "vendor_daily_regular_closes"},
                            {**action, "role": "corporate_action_check"},
                            {**independent_previous[0], "role": "independent_previous_close"},
                        ],
                        "basis_evidence": {
                            "previous_session_date": expected_previous_session_date.isoformat(),
                            "target_session_date": target_session_date.isoformat(),
                            "official_previous_close": official["previous_close"],
                            "official_target_close": official["target_close"],
                            "vendor_previous_close": previous.regular_close,
                            "vendor_target_close": target.regular_close,
                            "comparison_basis": "unadjusted_dated_regular_closes",
                        },
                    }
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
            if not official.get("previous_close_on_target_basis"):
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
        def resolve_with_current_sources() -> dict[str, Any]:
            return resolve_discrepancy(
                candidate=candidate, historical_provider=historical_series.provider,
                historical_previous_close=previous.regular_close,
                historical_target_close=target.regular_close, historical_change_pct=historical_change,
                official=official, corporate_action=action, minute_close=minute,
                independent_sources=independent, tolerance_pct=tolerance_pct,
                resolved_at=self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                vendor_previous_session_omitted=vendor_previous_session_omitted,
            )
        try:
            try:
                resolved = resolve_with_current_sources()
            except MarketDataError as initial_error:
                # A 16:00 minute bar can begin at a price other than the
                # exchange's final regular close. Require a second, dated EOD
                # source before classifying this boundary difference.
                if not (
                    action.get("status") in {"checked_none", "corporate_action_adjusted"}
                    and official.get("target_close_verified") is True
                    and official.get("target_close_source") in {"nasdaq_info", "nasdaq_historical"}
                    and minute is not None
                    and not close_matches(float(minute["target_close"]), float(official["target_close"]))
                    and "provider discrepancy reason is unresolved" in str(initial_error)
                ):
                    raise
                try:
                    dated = self.split_history_provider.fetch_pair(
                        ticker, official_previous_session, target_session_date,
                    )
                    if (
                        dated.get("previous_session_date") != official_previous_session.isoformat()
                        or dated.get("target_session_date") != target_session_date.isoformat()
                        or dated.get("price_basis") != "regular_close_normalized_for_split"
                        or provider_family(str(dated.get("provider_family") or dated.get("provider")))
                        in {"nasdaq", provider_family(historical_series.provider)}
                    ):
                        raise MarketDataError("dated independent close identity or basis disagrees")
                    independent.append({
                        "provider": dated["provider"], "provider_family": dated["provider_family"],
                        "session": "regular_close", "target_session_date": dated["target_session_date"],
                        "previous_close": dated["previous_split_normalized_close"],
                        "target_close": dated["target_regular_close"],
                        "raw_value": dated["target_raw_close"],
                        "parsed_value": dated["target_regular_close"],
                        "reported_change_raw": dated.get("reported_change_raw"),
                        "display_decimal_places": max(
                            int(dated["previous_display_decimal_places"]),
                            int(dated["target_display_decimal_places"]),
                        ),
                        "primary_exchange": None,
                        "price_basis": dated["price_basis"],
                        "source_identifier": dated["source_identifier"],
                        "raw_response_sha256": dated["raw_response_sha256"],
                    })
                except (MarketDataError, HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError) as exc:
                    errors.append(f"{self.split_history_provider.name}: {exc}")
                    raise initial_error from exc
                resolved = resolve_with_current_sources()
            if vendor_previous_session_omitted:
                if resolved["discrepancy_reason"] not in {
                    "stale_daily_bar", "non_nasdaq_primary_exchange_eod",
                    "vendor_omitted_previous_session_with_independent_eod",
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
        expected_previous_session_date: date | None = None,
        source_top20_floor_pct: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a missing prior bar with dated primary and independent evidence."""
        evidence = self.corporate_action_evidence.get(ticker)
        if evidence is not None:
            return self._resolve_documented_missing_previous(
                ticker=ticker, candidate=candidate, historical_series=historical_series,
                target=target, target_session_date=target_session_date,
                expected_previous_session_date=expected_previous_session_date,
                source_top20_floor_pct=source_top20_floor_pct,
                tolerance_pct=tolerance_pct, evidence=evidence,
            )
        notice_url = self.corporate_action_notices.get(ticker)
        if not notice_url:
            raise MarketDataError(
                f"Top20 {ticker}: missing previous bar needs verified corporate-action evidence "
                "or an official future-split notice"
            )
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

    def _resolve_documented_missing_previous(
        self, *, ticker: str, candidate: dict[str, Any], historical_series: DailySeries,
        target: DailyBar, target_session_date: date,
        expected_previous_session_date: date | None,
        source_top20_floor_pct: float | None,
        tolerance_pct: float, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        action = verify_corporate_action_evidence(
            ticker, evidence, transport=self.notice_transport,
        )
        effective = date.fromisoformat(action["effective_session_date"])
        if effective > target_session_date:
            raise MarketDataError(f"Top20 {ticker}: action is later than target session")
        action_source = {
            **action, "provider": "sec_primary_filing", "provider_family": "sec",
            "role": "official_corporate_action",
        }
        if action["event_type"] == "business_combination" and expected_previous_session_date is not None:
            if effective <= expected_previous_session_date:
                if source_top20_floor_pct is None:
                    raise MarketDataError(f"Top20 {ticker}: source cutoff required")
                official = self.official_provider.fetch(ticker, target_session_date, float(candidate["_close"]))
                history = self.split_history_provider.fetch_pair(
                    ticker, expected_previous_session_date, target_session_date,
                )
                official_previous = float(official["previous_close"])
                official_target = float(official["target_close"])
                if (official["previous_session_date"] != expected_previous_session_date.isoformat()
                        or official["target_session_date"] != target_session_date.isoformat()
                        or abs(official_previous - float(history["previous_split_normalized_close"])) > 0.01
                        or abs(official_target - float(history["target_regular_close"])) > 0.01
                        or not close_matches(official_target, target.regular_close)):
                    raise MarketDataError(f"Top20 {ticker}: post-combination same-symbol dated closes disagree")
                corrected_change = (official_target / official_previous - 1.0) * 100.0
                if corrected_change >= source_top20_floor_pct - tolerance_pct:
                    raise MarketDataError(f"Top20 {ticker}: post-combination corrected move may qualify; ranking review required")
                return {
                    "ranking_disposition": "exclude_below_top20_floor",
                    "exclusion_reason": "post_combination_same_symbol_move_below_verified_top20_floor",
                    "canonical_change_pct": corrected_change,
                    "screener_change_pct": float(candidate["_change_pct"]),
                    "source_top20_floor_pct": source_top20_floor_pct,
                    "corporate_action_status": "official_action_verified",
                    "supporting_sources": [
                        action_source, {**official, "role": "official_post_combination_closes"},
                        {**history, "role": "independent_post_combination_closes"},
                        {"provider": historical_series.provider,
                         "provider_family": provider_family(historical_series.provider),
                         "source_identifier": historical_series.source_identifier,
                         "raw_response_sha256": historical_series.raw_response_sha256,
                         "role": "target_regular_close"},
                    ],
                    "basis_evidence": {
                        "previous_session_date": expected_previous_session_date.isoformat(),
                        "target_session_date": target_session_date.isoformat(),
                        "official_previous_close": official_previous,
                        "official_target_close": official_target,
                        "comparison_basis": "post_effective_same_ticker_regular_closes",
                    },
                }
        if action["event_type"] in {"new_listing", "business_combination"}:
            # An opening session without a completed prior session, or a merger
            # that crosses the comparison boundary, is not a normal return.
            return {
                "ranking_disposition": "exclude_non_comparable",
                "exclusion_reason": (
                    "new_listing_no_comparable_previous_close" if action["event_type"] == "new_listing"
                    else "business_combination_conversion_not_verified"
                ),
                "corporate_action_status": "official_action_verified",
                "supporting_sources": [action_source],
                "basis_evidence": {"action": action, "vendor_history_dates": [
                    bar.session_date.isoformat() for bar in historical_series.bars
                ]},
            }
        if expected_previous_session_date is None or source_top20_floor_pct is None:
            raise MarketDataError(f"Top20 {ticker}: expected prior session and source cutoff required")
        if not (expected_previous_session_date < target_session_date and
                expected_previous_session_date < effective <= target_session_date):
            raise MarketDataError(f"Top20 {ticker}: action does not bridge the two market sessions")
        prior_symbol = action["prior_ticker"]
        prior_series = self.predecessor_provider.fetch(
            prior_symbol, expected_previous_session_date - timedelta(days=10),
            expected_previous_session_date,
        )
        prior_bars = [bar for bar in prior_series.bars if bar.session_date == expected_previous_session_date]
        if len(prior_bars) != 1:
            raise MarketDataError(f"Top20 {ticker}: predecessor dated close is missing")
        raw_previous = prior_bars[0].regular_close
        if raw_previous <= 0 or target.regular_close <= 0:
            raise MarketDataError(f"Top20 {ticker}: nonpositive regular close")
        factor = action["old_shares"] / action["new_shares"]
        normalized_previous = raw_previous * factor
        independently_adjusted = self.split_history_provider.fetch_pair(
            ticker, expected_previous_session_date, target_session_date,
        )
        independent_previous = independently_adjusted["previous_split_normalized_close"]
        independent_target = independently_adjusted["target_regular_close"]
        # Yahoo's historic quote may display only cents. Bound the possible
        # difference caused by that precision before accepting S&P's already
        # split-normalized prior close; never multiply it a second time.
        source_rounding_bound = 0.005 * factor + 0.005
        # Decimal cent boundaries are not exact in binary floats (1.51 - 1.50
        # can evaluate slightly above .01). Keep the existing one-cent limit.
        source_already_normalized = abs(raw_previous - independent_previous) <= 0.01 + 1e-12
        source_unadjusted = abs(normalized_previous - independent_previous) <= source_rounding_bound
        if source_already_normalized == source_unadjusted:
            raise MarketDataError(f"Top20 {ticker}: predecessor split basis is ambiguous or disagrees")
        applied_factor = 1 if source_already_normalized else factor
        normalized_previous = raw_previous * applied_factor
        if (not close_matches(independent_target, target.regular_close)
                or not close_matches(float(candidate["_close"]), target.regular_close)):
            raise MarketDataError(f"Top20 {ticker}: target regular closes disagree")
        canonical_change = (independent_target / independent_previous - 1.0) * 100.0
        sources = [
            action_source,
            {"provider": prior_series.provider, "provider_family": provider_family(prior_series.provider),
             "source_identifier": prior_series.source_identifier,
             "raw_response_sha256": prior_series.raw_response_sha256, "role": "predecessor_regular_close"},
            {"provider": historical_series.provider, "provider_family": provider_family(historical_series.provider),
             "source_identifier": historical_series.source_identifier,
             "raw_response_sha256": historical_series.raw_response_sha256, "role": "target_regular_close"},
            {**independently_adjusted, "role": "split_normalized_independent_history"},
        ]
        if canonical_change < source_top20_floor_pct - tolerance_pct:
            return {
                "ranking_disposition": "exclude_below_top20_floor",
                "exclusion_reason": "screener_split_artifact_below_verified_top20_floor",
                "canonical_change_pct": canonical_change,
                "screener_change_pct": float(candidate["_change_pct"]),
                "source_top20_floor_pct": source_top20_floor_pct,
                "corporate_action_status": "official_action_verified",
                "supporting_sources": sources,
                "basis_evidence": {
                    "previous_session_date": expected_previous_session_date.isoformat(),
                    "target_session_date": target_session_date.isoformat(),
                    "predecessor_regular_close": raw_previous,
                    "share_basis_factor": factor,
                    "applied_factor": applied_factor,
                    "predecessor_price_basis": "already_split_normalized" if source_already_normalized else "unadjusted",
                    "normalized_predecessor_close": normalized_previous,
                    "independent_normalized_previous_close": independent_previous,
                    "rounding_bound": source_rounding_bound,
                },
            }
        raise MarketDataError(
            f"Top20 {ticker}: corrected action change {canonical_change:.3f}% may qualify "
            "for Top20, but official exchange close corroboration is unavailable"
        )

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
    issuer_components: dict[str, Any] | None = None,
    strict_issuer_total: bool = False,
    discrepancy_arbitrator: DiscrepancyArbitrator | None = None,
    mismatch_tolerance_pct: float = 0.20,
    excluded_candidates: list[dict[str, Any]] | None = None,
    excluded_action_candidates: list[dict[str, Any]] | None = None,
    expected_previous_session_date: date | None = None,
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
    if len(candidates) < 20:
        raise MarketDataError(f"only {len(candidates)} eligible screener candidates")
    source_top20_floor_pct = candidates[19]["_change_pct"]

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
                    expected_previous_session_date=expected_previous_session_date,
                    source_top20_floor_pct=source_top20_floor_pct,
                )
                if str(discrepancy.get("ranking_disposition", "")).startswith("exclude_"):
                    if excluded_action_candidates is None:
                        raise MarketDataError(f"Top20 {ticker}: action exclusion audit output is required")
                    excluded_action_candidates.append({
                            "ticker": ticker, "session_date": target_session_date.isoformat(),
                            "reason": discrepancy["exclusion_reason"],
                            "screener_change_pct": candidate["_change_pct"],
                            "screener_rank": candidates.index(candidate) + 1,
                            "verified_change_pct": discrepancy.get("canonical_change_pct"),
                            "source_top20_floor_pct": source_top20_floor_pct,
                            "supporting_sources": discrepancy["supporting_sources"],
                            "basis_evidence": discrepancy["basis_evidence"],
                    })
                    continue
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
                if discrepancy_arbitrator is None:
                    raise MarketDataError(
                        f"Top20 {ticker} screener/history mismatch: {candidate['_change_pct']:.3f} vs {historical_change:.3f}"
                    )
                if discrepancy["discrepancy_status"] == "not_applicable":
                    discrepancy = discrepancy_arbitrator.resolve(
                        ticker=ticker, candidate=candidate, historical_series=series,
                        previous=previous, target=target, target_session_date=target_session_date,
                        tolerance_pct=mismatch_tolerance_pct,
                        expected_previous_session_date=expected_previous_session_date,
                        source_top20_floor_pct=source_top20_floor_pct,
                    )
                    if str(discrepancy.get("ranking_disposition", "")).startswith("exclude_"):
                        if excluded_action_candidates is None:
                            raise MarketDataError(f"Top20 {ticker}: action exclusion audit output is required")
                        excluded_action_candidates.append({
                                "ticker": ticker, "session_date": target_session_date.isoformat(),
                                "reason": discrepancy["exclusion_reason"],
                                "screener_change_pct": candidate["_change_pct"],
                                "screener_rank": candidates.index(candidate) + 1,
                                "verified_change_pct": discrepancy.get("canonical_change_pct"),
                                "source_top20_floor_pct": source_top20_floor_pct,
                                "supporting_sources": discrepancy["supporting_sources"],
                                "basis_evidence": discrepancy["basis_evidence"],
                        })
                        continue
                    if discrepancy.get("discrepancy_reason") == "official_split_normalized_eod":
                        corrected_change = (float(discrepancy["official_target_close"]) / float(discrepancy["official_previous_close"]) - 1.0) * 100.0
                        candidate = {**candidate, "_change_pct":corrected_change}
                        historical_change = corrected_change
                    review_flags.extend(["discrepancy_resolved", str(discrepancy["discrepancy_reason"])])
                    if discrepancy.get("liquidity_flag") == "low_liquidity":
                        review_flags.append("low_liquidity")
            if abs(target.regular_close - candidate["_close"]) > max(0.02, target.regular_close * 0.005):
                raise MarketDataError(f"Top20 {ticker} screener close is stale or non-regular")
        vendor_cap = _numeric(candidate.get("marketCap"), "marketCap", nullable=True)
        if vendor_cap == 0:
            vendor_cap = None
        evidence = issuer_components.get(ticker)
        if strict_issuer_total:
            from lib.ny_market_issuer_cap import issuer_cap_from_evidence
            if evidence is None:
                raise MarketDataError(f"Top20 {ticker} lacks issuer-total evidence or documented source attempts")
            market_cap, market_cap_method, components, cap_evidence = issuer_cap_from_evidence(
                ticker, candidate["_close"], target_session_date, evidence,
                (discrepancy.get("basis_evidence") or {}).get("official_action"),
            )
        else:
            components = evidence or []
            cap_evidence = None
        if not strict_issuer_total and components:
            market_cap = issuer_total_market_cap(components)
            if len(components) > 1:
                market_cap_method = "issuer_total_dual_class"
            elif re.search(r"(?i)American Depositary|ADS\b|ADR\b", str(candidate.get("name"))):
                market_cap_method = "issuer_total_ads"
            else:
                market_cap_method = "issuer_total_single_class"
        elif not strict_issuer_total:
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
            **({"market_cap_evidence": cap_evidence} if strict_issuer_total else {}),
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
            ranked.sort(key=lambda row: row["change_pct"], reverse=True)
            for rank, row in enumerate(ranked, 1):
                row["rank"] = rank
            return ranked
    raise MarketDataError(
        f"only {len(ranked)} eligible Top20 records remained; "
        f"documented action exclusions={len(excluded_action_candidates or [])}; "
        f"no-trade exclusions={len(excluded_candidates or [])}"
    )


def reject_stale_undated_screener(
    screener: dict[str, Any], historical: dict[str, DailySeries],
    target_session_date: date, symbols: Iterable[str],
) -> None:
    """Reject an undated bulk screener when its prices are plainly one session old."""
    if screener.get("provider") != NasdaqScreenerProvider.name:
        return
    rows = {str(row.get("symbol", "")).upper(): row for row in screener.get("rows", [])}
    comparable = prior_only = target_matches = 0
    for symbol in symbols:
        row, series = rows.get(symbol), historical.get(symbol)
        if row is None or series is None:
            continue
        target = next((bar for bar in series.bars if bar.session_date == target_session_date), None)
        previous = max(
            (bar for bar in series.bars if bar.session_date < target_session_date),
            key=lambda bar: bar.session_date, default=None,
        )
        if target is None or previous is None:
            continue
        try:
            quoted = _numeric(row.get("lastsale"), "screener last sale")
        except MarketDataError:
            continue
        comparable += 1
        matches_target = close_matches(quoted, target.regular_close)
        target_matches += int(matches_target)
        prior_only += int(close_matches(quoted, previous.regular_close) and not matches_target)
    if comparable >= 20 and prior_only >= max(12, (comparable * 3 + 4) // 5) and target_matches <= 2:
        raise MarketDataError(
            f"Nasdaq screener is stale for target session {target_session_date}: "
            f"{prior_only}/{comparable} candidate prices match the prior session, "
            f"but only {target_matches}/{comparable} match the target session"
        )


def build_canonical_market_data_packet(
    target_session_date: date,
    *,
    historical_providers: Iterable[HistoricalProvider] | None = None,
    issuer_components: dict[str, Any] | None = None,
    screener_provider: NasdaqScreenerProvider | None = None,
    discrepancy_arbitrator: DiscrepancyArbitrator | None = None,
    candidate_limit: int = 25,
    verified_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sole deterministic market-data input consumed by report generation."""
    providers = tuple(historical_providers or (
        YahooChartProvider(host="query1.finance.yahoo.com"),
        YahooChartProvider(host="query2.finance.yahoo.com"),
        YahooChartProvider(host="query1.finance.yahoo.com", closed_meta_fallback=True),
        YahooChartProvider(host="query2.finance.yahoo.com", closed_meta_fallback=True),
    ))
    snapshot = verified_snapshot if verified_snapshot is not None else build_index_sector_snapshot(providers, target_session_date)
    if snapshot.get("market_session_date") != target_session_date.isoformat():
        raise MarketDataError("checkpoint market session differs from requested session")
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
        required_session_date=target_session_date,
    )
    reject_stale_undated_screener(screener, historical.series, target_session_date, symbols)
    excluded_candidates: list[dict[str, Any]] = []
    excluded_action_candidates: list[dict[str, Any]] = []
    top20 = rank_top20(
        screener,
        target_session_date=target_session_date,
        historical_series=historical.series,
        issuer_components=issuer_components,
        strict_issuer_total=issuer_components is not None,
        discrepancy_arbitrator=discrepancy_arbitrator or LiveDiscrepancyArbitrator(),
        excluded_candidates=excluded_candidates,
        excluded_action_candidates=excluded_action_candidates,
        expected_previous_session_date=date.fromisoformat(snapshot["indexes"][0]["previous_session_date"]),
    )
    provider_names = {
        snapshot["source"]["name"], screener["provider"], historical.provider,
    }
    raw_hashes = {screener["raw_response_sha256"]}
    for item in (*snapshot["indexes"], *snapshot["sectors"], *top20, *excluded_action_candidates):
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
            date_confirmation_hash = source.get("date_confirmation_sha256")
            if isinstance(date_confirmation_hash, str):
                raw_hashes.add(date_confirmation_hash)
    generated_at = max(
        snapshot["source"]["retrieved_at"], screener["retrieved_at"],
        *(item.retrieved_at for item in historical.series.values()),
    )
    return {
        **snapshot,
        "market_data_contract_version": "ny_market_data_v2" if issuer_components is not None else MARKET_DATA_CONTRACT_VERSION,
        "market_data_generated_at": generated_at,
        "providers": sorted(provider_names),
        "discrepancy_count": sum(item["discrepancy_status"] != "not_applicable" for item in top20),
        "top_gainers_20": top20,
        "excluded_no_trade_candidates": excluded_candidates,
        "excluded_corporate_action_candidates": excluded_action_candidates,
        "excluded_corporate_action_count": len(excluded_action_candidates),
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
