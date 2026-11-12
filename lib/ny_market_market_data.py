"""Deterministic acquisition of canonical NY daily market data.

Acquisition is deliberately separate from ``ny_market_research`` validation.
All prices in this module are unadjusted regular-session daily closes.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
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
NASDAQ_TRADER_DIRECTORY_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
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
_nasdaq_request_lock = threading.Lock()
_nasdaq_next_request_at = 0.0


def _pace_nasdaq_api_request() -> None:
    """Space public API requests across concurrent universe workers."""
    global _nasdaq_next_request_at
    with _nasdaq_request_lock:
        now = time_module.monotonic()
        scheduled = max(now, _nasdaq_next_request_at)
        _nasdaq_next_request_at = scheduled + 1.0
    if scheduled > now:
        time_module.sleep(scheduled - now)


def default_transport(url: str, headers: dict[str, str]) -> bytes:
    """Retry only transient transport failures, with a fixed upper bound."""
    for attempt in range(3):
        try:
            if urlsplit(url).hostname == "api.nasdaq.com":
                # Nasdaq's public API can reject urllib's HTTP client while
                # accepting the same endpoint and headers through requests.
                # Keep the URL, response body, and validation contract intact.
                try:
                    _pace_nasdaq_api_request()
                    response = requests.get(url, headers=headers, timeout=(10, 30),
                                            allow_redirects=False)
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                    raise URLError(str(exc)) from exc
                if response.status_code != 200:
                    raise HTTPError(url, response.status_code, "Nasdaq API request failed",
                                    response.headers, None)
                raw = response.content
            else:
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
    symbol_mapping_evidence: dict[str, Any] | None = None


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
        expected_names: dict[str, str] | None = None,
    ) -> None:
        if host not in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"}:
            raise ValueError("unsupported Yahoo chart host")
        self.host = host
        self.name = ("yahoo_closed_meta_query1" if host.startswith("query1") else "yahoo_closed_meta_query2") if closed_meta_fallback else ("yahoo_chart_query1" if host.startswith("query1") else "yahoo_chart_query2")
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.closed_meta_fallback = closed_meta_fallback
        self.expected_names = expected_names or {}

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
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        provider_symbol = symbol
        try:
            raw = self.transport(url, headers)
        except HTTPError as exc:
            expected_name = self.expected_names.get(symbol)
            if exc.code != 404 or not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}\.[A-Z]", symbol) or not expected_name:
                raise
            provider_symbol = symbol.replace(".", "-")
            url = self.build_url(provider_symbol, start_date, end_date)
            raw = self.transport(url, headers)
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
        returned_symbol = str(meta.get("symbol") or provider_symbol)
        if provider_symbol != symbol and not meta.get("symbol"):
            raise MarketDataError(f"{self.name}:{symbol}: class-symbol alias response lacks symbol identity")
        if returned_symbol.upper() != provider_symbol.upper():
            raise MarketDataError(f"{self.name}:{symbol}: response symbol mismatch {returned_symbol}")
        symbol_mapping_evidence = None
        if provider_symbol != symbol:
            expected_name = self.expected_names[symbol].strip()
            actual_names = [str(meta.get(key) or "") for key in ("longName", "shortName")]
            normalize = lambda name: re.sub(r"[^a-z0-9]", "", name.casefold())
            matching_name = next((name for name in actual_names if normalize(name) == normalize(expected_name)), None)
            ads_descriptor = re.fullmatch(
                r"(.+?)\s+((?:(?:ADS|ADR)\s+)?American Depositary (?:Shares|Receipts))",
                expected_name, re.I,
            )
            class_descriptor = re.fullmatch(
                r"(.+?)\s+(Class ([A-Z])(?: (?:Common|Ordinary) (?:Stock|Shares))?)",
                expected_name, re.I,
            )
            voting_descriptor = re.fullmatch(
                r"(.+?)\s+((?:Non-Voting|Voting) Common Stock)", expected_name, re.I,
            )
            common_descriptor = re.fullmatch(
                r"(.+?)\s+((?:Common Stock|Ordinary Shares))", expected_name, re.I,
            )
            descriptor = ads_descriptor or class_descriptor or voting_descriptor or common_descriptor
            matching_descriptor_name = next((name for name in actual_names if descriptor
                                             and normalize(descriptor.group(1)) == normalize(name)), None)
            name_with_security_descriptor = (
                bool(descriptor)
                and len(normalize(descriptor.group(1))) >= 6
                and bool(matching_descriptor_name)
                and bool(actual_names[0])
                and bool(meta.get("fullExchangeName"))
                and (class_descriptor is None or class_descriptor.group(3).upper() == symbol.rsplit(".", 1)[-1])
            )
            if ((matching_name is None and not name_with_security_descriptor)
                    or meta.get("instrumentType") != "EQUITY" or meta.get("currency") != "USD"):
                raise MarketDataError(f"{self.name}:{symbol}: class-symbol alias identity mismatch")
            symbol_mapping_evidence = {
                "canonical_symbol": symbol, "provider_symbol": provider_symbol,
                "screener_company_name": expected_name,
                "provider_company_name": matching_name or matching_descriptor_name,
                "provider_instrument_type": meta["instrumentType"],
                "provider_currency": meta["currency"],
                "source_identifier": url, "raw_response_sha256": raw_hash,
                "rule": "single_class_suffix_dot_to_hyphen_after_404",
                **({"name_match_rule": (
                        "issuer_short_name_with_ads_security_descriptor" if ads_descriptor
                        else "issuer_short_name_with_class_security_descriptor" if class_descriptor
                        else "issuer_name_with_voting_security_descriptor" if voting_descriptor
                        else "issuer_name_with_common_stock_descriptor"),
                    "provider_long_name": actual_names[0],
                    "screener_security_descriptor": descriptor.group(2),
                    "provider_exchange": meta["fullExchangeName"]}
                   if name_with_security_descriptor and matching_name is None else {}),
            }
        return DailySeries(
            symbol=symbol,
            provider=self.name,
            source_identifier=url + ("#closed-regularMarketPrice" if used_closed_meta else ""),
            retrieved_at=self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            raw_response_sha256=raw_hash,
            bars=ordered,
            symbol_mapping_evidence=symbol_mapping_evidence,
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
        try:
            raw = self.transport(url, {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            })
        except HTTPError as exc:
            if exc.code != 403:
                raise
            return self._fetch_official_directory_fallback()
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

    def _fetch_official_directory_fallback(self) -> dict[str, Any]:
        """Use Nasdaq Trader's complete symbol files if its screener denies access."""
        sources = []
        rows_by_symbol: dict[str, dict[str, Any]] = {}
        raw_parts = []
        for filename, symbol_field in (("nasdaqlisted.txt", "Symbol"),
                                       ("otherlisted.txt", "ACT Symbol")):
            url = NASDAQ_TRADER_DIRECTORY_URL + filename
            raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "text/plain"})
            try:
                lines = raw.decode("utf-8-sig").splitlines()
            except UnicodeDecodeError as exc:
                raise MarketDataError(f"Nasdaq Trader {filename} is not UTF-8") from exc
            if len(lines) < 100:
                raise MarketDataError(f"Nasdaq Trader {filename} is incomplete")
            fields = lines[0].split("|")
            required = {symbol_field, "Security Name", "ETF", "Test Issue"}
            if not required.issubset(fields):
                raise MarketDataError(f"Nasdaq Trader {filename} has unexpected columns")
            timestamp = re.fullmatch(r"File Creation Time: (\d{8}\d{2}:\d{2})\|*", lines[-1])
            if timestamp is None:
                raise MarketDataError(f"Nasdaq Trader {filename} lacks a creation timestamp")
            try:
                created = datetime.strptime(timestamp.group(1), "%m%d%Y%H:%M")
            except ValueError as exc:
                raise MarketDataError(f"Nasdaq Trader {filename} creation timestamp is invalid") from exc
            for line in lines[1:-1]:
                values = line.split("|")
                if len(values) != len(fields):
                    raise MarketDataError(f"Nasdaq Trader {filename} has a malformed row")
                item = dict(zip(fields, values))
                symbol = item[symbol_field].strip().upper()
                name = item["Security Name"].strip()
                if item["ETF"] != "N" or item["Test Issue"] != "N":
                    continue
                candidate = {"symbol": symbol, "name": name}
                if not eligible_screener_row(candidate):
                    continue
                if symbol in rows_by_symbol:
                    raise MarketDataError(f"Nasdaq Trader directory duplicates {symbol}")
                rows_by_symbol[symbol] = candidate
            sources.append({"source_identifier": url,
                            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                            "file_creation_date": created.date().isoformat(),
                            "file_creation_time": timestamp.group(1)})
            raw_parts.append(raw)
        if len(rows_by_symbol) < 100 or len({source["file_creation_date"] for source in sources}) != 1:
            raise MarketDataError("Nasdaq Trader directory files disagree or are incomplete")
        return {"provider": "nasdaqtrader_symbol_directory", "source_identifier": sources[0]["source_identifier"],
                "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                "raw_response_sha256": hashlib.sha256(b"\n".join(raw_parts)).hexdigest(),
                "directory_files": sources, "rows": list(rows_by_symbol.values())}


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


class NasdaqYahooRegularUniverseProvider:
    """Discover candidates from a full listing and dated regular-session closes."""

    name = "nasdaq_listing_yahoo_regular_universe"
    SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"

    def __init__(self, *, listing_provider: NasdaqScreenerProvider | None = None,
                 transport: Transport = default_transport,
                 now: Callable[[], datetime] | None = None,
                 max_workers: int = 8,
                 corporate_action_notices: dict[str, str] | None = None,
                 notice_transport: Transport = default_transport,
                 action_provider: YahooCorporateActionProvider | None = None,
                 split_history_provider: StockAnalysisHistoricalCloseProvider | None = None) -> None:
        self.listing_provider = listing_provider or NasdaqScreenerProvider(transport=transport, now=now)
        self.transport = transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_workers = max_workers
        self.corporate_action_notices = corporate_action_notices or {}
        self.notice_transport = notice_transport
        self.action_provider = action_provider or YahooCorporateActionProvider(transport=transport)
        self.split_history_provider = split_history_provider or StockAnalysisHistoricalCloseProvider(transport=transport)

    def _recover_split_missing_previous(
        self, symbol: str, target: date, target_bar: DailyBar,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Recover a Yahoo history gap only from a verified, already normalized pair."""
        notice_url = self.corporate_action_notices.get(symbol)
        if not notice_url:
            raise MarketDataError(f"regular-universe {symbol}: missing prior bar needs an official split notice")
        try:
            validate_notice_url(notice_url)
            notice_raw = self.notice_transport(notice_url, {"User-Agent": "Mozilla/5.0"})
            notice = parse_exchange_split_notice(notice_raw, notice_url, symbol)
        except ValueError as exc:
            raise MarketDataError(f"regular-universe {symbol}: invalid split notice: {exc}") from exc
        if date.fromisoformat(notice["effective_session_date"]) != target:
            raise MarketDataError(f"regular-universe {symbol}: split effective date differs from target")
        action = self.action_provider.fetch(symbol, target)
        try:
            validate_vendor_actions(action.get("events") or {}, notice)
        except ValueError as exc:
            raise MarketDataError(f"regular-universe {symbol}: vendor split conflicts with notice: {exc}") from exc
        url = (f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/historical?"
               + urlencode({"assetclass": "stocks", "fromdate": (target - timedelta(days=10)).isoformat(),
                            "todate": (target + timedelta(days=1)).isoformat(), "limit": 5000}))
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                   "Origin": "https://www.nasdaq.com"})
        payload = _raw_json(raw, self.name)
        if payload.get("status", {}).get("rCode") != 200 or (payload.get("data") or {}).get("symbol") != symbol:
            raise MarketDataError(f"regular-universe {symbol}: official split history unavailable or wrong symbol")
        rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows"))
        if not isinstance(rows, list):
            raise MarketDataError(f"regular-universe {symbol}: official split history rows missing")
        dated = []
        for row in rows:
            try:
                day = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
            except ValueError:
                continue
            dated.append((day, row))
        targets = [row for day, row in dated if day == target]
        previous = [(day, row) for day, row in dated if day < target]
        if len(targets) != 1 or not previous:
            raise MarketDataError(f"regular-universe {symbol}: dated official split pair missing")
        previous_day, previous_row = max(previous, key=lambda item: item[0])
        official_previous = _numeric(previous_row.get("close"), "official prior split close")
        official_target = _numeric(targets[0].get("close"), "official target split close")
        if not close_matches(target_bar.regular_close, official_target):
            raise MarketDataError(f"regular-universe {symbol}: Yahoo target differs from official split close")
        independent = self.split_history_provider.fetch_pair(symbol, previous_day, target)
        if (independent.get("previous_session_date") != previous_day.isoformat()
                or independent.get("target_session_date") != target.isoformat()
                or independent.get("price_basis") != "regular_close_normalized_for_split"
                or provider_family(str(independent.get("provider_family") or independent.get("provider")))
                in {"nasdaq", "yahoo"}):
            raise MarketDataError(f"regular-universe {symbol}: independent split basis or dates disagree")
        previous_digits = int(independent["previous_display_decimal_places"])
        target_digits = int(independent["target_display_decimal_places"])
        if (not close_matches(float(independent["previous_split_normalized_close"]),
                              round_market_display(official_previous, previous_digits))
                or not close_matches(float(independent["target_regular_close"]),
                                     round_market_display(official_target, target_digits))):
            raise MarketDataError(f"regular-universe {symbol}: normalized independent split pair disagrees")
        change = (official_target / official_previous - 1.0) * 100.0
        evidence = {
            "disposition": "official_split_normalized_pair_recovers_missing_yahoo_previous",
            "official_previous_session_date": previous_day.isoformat(),
            "official_previous_close": official_previous,
            "official_target_close": official_target,
            "official_previous_price_basis": "already_split_normalized",
            "applied_split_factor": 1.0,
            "official_source_identifier": url,
            "official_raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "official_action": notice,
            "vendor_action_source_identifier": action.get("source_identifier"),
            "vendor_action_raw_response_sha256": action.get("raw_response_sha256"),
            "independent_split_pair": independent,
        }
        recovered = {"symbol": symbol, "close": official_target, "change_pct": change,
                     "volume": target_bar.volume, "quote_source_identifier": url,
                     "quote_raw_response_sha256": evidence["official_raw_response_sha256"]}
        return recovered, evidence

    def _spark_batch(self, batch: list[str], target: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        url = self.SPARK_URL + "?" + urlencode({
            "symbols": ",".join(batch), "range": "5d", "interval": "1d",
        })
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        data = _raw_json(raw, self.name)
        results = (data.get("spark") or {}).get("result")
        if not isinstance(results, list):
            raise MarketDataError("regular-universe Spark batch is missing results")
        evidence = {"source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                    "requested_symbols": batch, "display_rounding_reconciliations": [],
                    "dated_historical_discovery": []}
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in results:
            symbol = str(item.get("symbol") or "").upper() if isinstance(item, dict) else ""
            if symbol not in batch or symbol in seen:
                raise MarketDataError("regular-universe Spark returned an unexpected or duplicate symbol")
            seen.add(symbol)
            responses = item.get("response")
            if not isinstance(responses, list) or len(responses) != 1:
                continue
            chart = responses[0]
            meta = chart.get("meta") or {}
            stamps = chart.get("timestamp") or []
            quotes = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quotes.get("close") or []
            volumes = quotes.get("volume") or []
            regular_price = meta.get("regularMarketPrice")
            if len(stamps) < 2 or len(stamps) != len(closes):
                continue
            days = [datetime.fromtimestamp(stamp, NY_TZ).date() for stamp in stamps]
            target_indexes = [index for index, day in enumerate(days) if day == target]
            if len(target_indexes) != 1:
                continue
            target_index = target_indexes[0]
            previous_indexes = [index for index, day in enumerate(days) if day < target]
            if not previous_indexes:
                continue
            previous_index = max(previous_indexes, key=lambda index: days[index])
            if not all(isinstance(closes[index], (int, float)) and math.isfinite(float(closes[index]))
                       and float(closes[index]) > 0 for index in (target_index, previous_index)):
                continue
            meta_day = datetime.fromtimestamp(meta.get("regularMarketTime", 0), NY_TZ).date()
            canonical_close = float(closes[target_index])
            if meta_day > target:
                previous_close = float(closes[previous_index])
                change = (canonical_close / previous_close - 1.0) * 100.0
                evidence["dated_historical_discovery"].append({
                    "symbol": symbol, "target_session_date": target.isoformat(),
                    "target_close": canonical_close,
                    "previous_session_date": days[previous_index].isoformat(),
                    "previous_close": previous_close,
                    "current_meta_session_date": meta_day.isoformat(),
                    "disposition": "ignore_next_session_live_meta_for_prior_dated_bars",
                })
                parsed.append({"symbol": symbol, "close": canonical_close, "change_pct": change,
                               "volume": volumes[target_index] if len(volumes) == len(stamps) else None,
                               "quote_source_identifier": url,
                               "quote_raw_response_sha256": evidence["raw_response_sha256"],
                               "previous_session_date": days[previous_index].isoformat()})
                continue
            if (meta_day != target or target_index != len(stamps) - 1
                    or not isinstance(regular_price, (int, float))
                    or not math.isfinite(float(regular_price)) or regular_price <= 0):
                continue
            if abs(canonical_close - float(regular_price)) > max(1e-6, float(regular_price) * 1e-6):
                display = re.fullmatch(r"\d+\.(\d{2,6})", str(closes[-1]))
                hint = meta.get("priceHint")
                if (display is None or not isinstance(hint, int) or isinstance(hint, bool)
                        or not 2 <= hint <= 6 or len(display.group(1)) > hint
                        or not close_matches(round_market_display(float(regular_price), len(display.group(1))),
                                             canonical_close)):
                    continue
                evidence["display_rounding_reconciliations"].append({
                    "symbol": symbol, "dated_daily_close": canonical_close,
                    "regular_market_price": float(regular_price),
                    "daily_display_decimal_places": len(display.group(1)),
                    "price_hint": hint,
                    "disposition": "discovery_only_dated_daily_display_rounding",
                })
                canonical_close = float(regular_price)
            change = meta.get("regularMarketChangePercent")
            if not isinstance(change, (int, float)) or not math.isfinite(float(change)):
                continue
            parsed.append({"symbol": symbol, "close": canonical_close, "change_pct": change,
                           "volume": volumes[target_index] if len(volumes) == len(stamps) else None,
                           "quote_source_identifier": url,
                           "quote_raw_response_sha256": evidence["raw_response_sha256"],
                           "previous_session_date": days[previous_index].isoformat()})
        return parsed, evidence

    def _verify_missing(self, symbol: str, name: str, target: date) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        url = f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/info?assetclass=stocks"
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                   "Origin": "https://www.nasdaq.com"})
        payload = _raw_json(raw, self.name)
        if payload.get("status", {}).get("rCode") != 200:
            raise MarketDataError(f"regular-universe {symbol}: official quote unavailable")
        data = payload.get("data") or {}
        if str(data.get("symbol") or "").upper() != symbol:
            raise MarketDataError(f"regular-universe {symbol}: official response symbol mismatch")
        secondary = data.get("secondaryData") or {}
        stamp = str(secondary.get("lastTradeTimestamp") or "")
        observed_day = None
        match = re.fullmatch(r"Closed at ([A-Z][a-z]{2} \d{1,2}, \d{4}) 4:00 PM ET", stamp)
        if match:
            observed_day = datetime.strptime(match.group(1), "%b %d, %Y").date()
        evidence = {"symbol": symbol, "official_source_identifier": url,
                    "official_raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                    "official_regular_timestamp": stamp,
                    "primary_exchange": str(data.get("exchange") or "")}
        if observed_day == target:
            close = _numeric(secondary.get("lastSalePrice"), "official regular last sale")
            pct_text = str(secondary.get("percentageChange") or "").strip()
            if not pct_text and secondary.get("deltaIndicator") == "unch":
                evidence["disposition"] = "official_unchanged_regular_session"
                return None, evidence
            if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", pct_text):
                raise MarketDataError(f"regular-universe {symbol}: official regular change is unavailable")
            pct = float(pct_text[:-1])
            if not math.isfinite(pct) or close is None or close <= 0:
                raise MarketDataError(f"regular-universe {symbol}: invalid official regular quote")
            evidence["disposition"] = "official_dated_regular_quote"
            return {"symbol": symbol, "name": name, "close": close, "change_pct": pct,
                    "volume": None, "quote_source_identifier": url,
                    "quote_raw_response_sha256": evidence["official_raw_response_sha256"]}, evidence
        if observed_day is not None and observed_day > target:
            raise MarketDataError(f"regular-universe {symbol}: official quote moved beyond target session")
        # A second dated provider must also have no target bar before this
        # listing is classified as having no observed target-session trade.
        chart = YahooChartProvider(transport=self.transport, now=self.now,
                                   expected_names={symbol: name})
        series = chart.fetch(symbol, target - timedelta(days=10), target)
        evidence.update({"chart_source_identifier": series.source_identifier,
                         "chart_raw_response_sha256": series.raw_response_sha256})
        target_bar = next((bar for bar in series.bars if bar.session_date == target), None)
        if target_bar is not None:
            primary = data.get("primaryData") or {}
            primary_stamp = str(primary.get("lastTradeTimestamp") or "")
            # A date-only Nasdaq quote can include activity outside the regular
            # session. When it conflicts with a zero-volume daily bar, require
            # the exchange's dated historical no-trade row before omitting the
            # symbol from a pure regular-session ranking.
            if (evidence["primary_exchange"].upper().startswith("NASDAQ")
                    and target_bar.volume == 0
                    and primary_stamp == f"{target.strftime('%b')} {target.day}, {target.year}"):
                primary_close = _numeric(primary.get("lastSalePrice"), "official date-only quote", nullable=True)
                if primary_close is not None and not close_matches(primary_close, target_bar.regular_close):
                    if symbol in self.corporate_action_notices:
                        raise MarketDataError(f"regular-universe {symbol}: zero-volume split basis needs arbitration")
                    history_url = (f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/historical?"
                                   + urlencode({"assetclass": "stocks", "fromdate": (target - timedelta(days=10)).isoformat(),
                                                "todate": (target + timedelta(days=1)).isoformat(), "limit": 5000}))
                    history_raw = self.transport(history_url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                                               "Origin": "https://www.nasdaq.com"})
                    history = _raw_json(history_raw, self.name)
                    history_data = history.get("data") or {}
                    rows = (history_data.get("tradesTable") or {}).get("rows")
                    if (history.get("status", {}).get("rCode") != 200
                            or history_data.get("symbol") != symbol or not isinstance(rows, list)):
                        raise MarketDataError(f"regular-universe {symbol}: official no-trade history unavailable")
                    by_day = {}
                    for row in rows:
                        try:
                            day = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
                        except (TypeError, ValueError):
                            continue
                        if day in by_day:
                            raise MarketDataError(f"regular-universe {symbol}: duplicate official no-trade history")
                        by_day[day] = row
                    previous, _ = completed_session_pair(series, target)
                    current_row = by_day.get(target)
                    previous_row = by_day.get(previous.session_date)
                    official_target = (_numeric(current_row.get("close"), "official no-trade close")
                                       if current_row else None)
                    official_previous = (_numeric(previous_row.get("close"), "official previous close")
                                         if previous_row else None)
                    matching_digits = next((digits for digits in range(2, 5)
                                            if official_target is not None and official_previous is not None
                                            and close_matches(target_bar.regular_close,
                                                              round_market_display(official_target, digits))
                                            and close_matches(previous.regular_close,
                                                              round_market_display(official_previous, digits))), None)
                    prices_match = (official_target is not None and official_previous is not None
                                    and ((close_matches(official_target, target_bar.regular_close)
                                          and close_matches(official_previous, previous.regular_close))
                                         or matching_digits is not None))
                    if (not current_row or not previous_row
                            or str(current_row.get("volume", "")).strip().upper() not in {"N/A", "0"}
                            or not prices_match):
                        raise MarketDataError(f"regular-universe {symbol}: zero-volume close conflicts with official history")
                    evidence.update({"disposition": "zero_regular_volume_confirmed_by_official_history",
                                     "date_only_quote_close": primary_close,
                                     "official_no_trade_source_identifier": history_url,
                                     "official_no_trade_raw_response_sha256": hashlib.sha256(history_raw).hexdigest(),
                                     "official_no_trade_target_close": target_bar.regular_close,
                                     "official_no_trade_exchange_target_close": official_target,
                                     "official_no_trade_previous_session_date": previous.session_date.isoformat(),
                                     "official_no_trade_previous_close": previous.regular_close,
                                     "official_no_trade_exchange_previous_close": official_previous,
                                     "official_no_trade_vendor_display_decimal_places": matching_digits})
                    return None, evidence
            try:
                previous, target_bar = completed_session_pair(series, target)
            except MarketDataError as exc:
                if "previous completed session is missing" not in str(exc):
                    raise
                # A newly trading security has no comparable prior close.  A
                # vendor may still synthesize a change from its former unit or
                # another class, so check the exchange's dated history before
                # treating the gap as a split requiring normalization.
                history_url = (f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/historical?"
                               + urlencode({"assetclass": "stocks", "fromdate": (target - timedelta(days=35)).isoformat(),
                                            "todate": (target + timedelta(days=1)).isoformat(), "limit": 5000}))
                history_raw = self.transport(history_url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                                           "Origin": "https://www.nasdaq.com"})
                history = _raw_json(history_raw, self.name)
                history_data = history.get("data") or {}
                history_rows = (history_data.get("tradesTable") or {}).get("rows")
                if (history.get("status", {}).get("rCode") != 200
                        or history_data.get("symbol") != symbol or not isinstance(history_rows, list)):
                    raise MarketDataError(f"regular-universe {symbol}: official missing-prior history unavailable")
                dated_rows = []
                for history_row in history_rows:
                    try:
                        history_day = datetime.strptime(str(history_row.get("date")), "%m/%d/%Y").date()
                    except ValueError:
                        continue
                    dated_rows.append((history_day, history_row))
                target_rows = [row for day, row in dated_rows if day == target]
                prior_rows = [row for day, row in dated_rows if day < target]
                if not prior_rows and len(target_rows) == 1 and symbol not in self.corporate_action_notices:
                    official_target = _numeric(target_rows[0].get("close"), "official first-session close")
                    if not close_matches(official_target, target_bar.regular_close):
                        raise MarketDataError(f"regular-universe {symbol}: first-session closes disagree")
                    evidence.update({"disposition": "first_trading_session_no_comparable_prior",
                                     "official_history_source_identifier": history_url,
                                     "official_history_raw_response_sha256": hashlib.sha256(history_raw).hexdigest(),
                                     "official_target_close": official_target,
                                     "vendor_target_close": target_bar.regular_close,
                                     "prior_session_rows": 0})
                    return None, evidence
                recovered, recovery_evidence = self._recover_split_missing_previous(symbol, target, target_bar)
                evidence.update(recovery_evidence)
                return {**recovered, "name": name}, evidence
            pct = (target_bar.regular_close / previous.regular_close - 1.0) * 100.0
            evidence["disposition"] = "dated_chart_regular_quote_missing_from_batch"
            return {"symbol": symbol, "name": name, "close": target_bar.regular_close,
                    "change_pct": pct, "volume": target_bar.volume,
                    "quote_source_identifier": series.source_identifier,
                    "quote_raw_response_sha256": series.raw_response_sha256}, evidence
        evidence["disposition"] = "no_target_session_quote_in_two_independent_sources"
        return None, evidence

    def _official_previous(self, symbol: str, target: date,
                           target_close: float | None = None) -> tuple[date, float, dict[str, Any]]:
        if symbol in self.corporate_action_notices:
            pair = NasdaqOfficialCloseProvider(
                transport=self.transport, now=self.now,
                corporate_action_notices=self.corporate_action_notices,
                notice_transport=self.notice_transport,
            ).fetch(symbol, target, target_close or 0.0)
            if target_close is None or not close_matches(float(pair["target_close"]), target_close):
                raise MarketDataError(f"regular-universe {symbol}: official split target differs from dated quote")
            return date.fromisoformat(pair["previous_session_date"]), float(pair["previous_close"]), {
                "source_identifier": pair["source_identifiers"],
                "raw_response_sha256": pair["raw_response_sha256"],
                "previous_price_basis": pair.get("previous_price_basis"),
                "applied_split_factor": pair.get("applied_split_factor"),
                "official_action": pair.get("official_action"),
            }
        url = (f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/historical?"
               + urlencode({"assetclass": "stocks", "fromdate": (target - timedelta(days=35)).isoformat(),
                            "todate": (target + timedelta(days=1)).isoformat(), "limit": 5000}))
        raw = self.transport(url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                   "Origin": "https://www.nasdaq.com"})
        payload = _raw_json(raw, self.name)
        if payload.get("status", {}).get("rCode") != 200:
            raise MarketDataError(f"regular-universe {symbol}: dated official history unavailable")
        rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows"))
        if not isinstance(rows, list):
            raise MarketDataError(f"regular-universe {symbol}: dated official history rows missing")
        previous = []
        for row in rows:
            try:
                day = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
            except ValueError:
                continue
            if day < target:
                previous.append((day, _numeric(row.get("close"), "official prior close")))
        if not previous:
            raise MarketDataError(f"regular-universe {symbol}: official prior close unavailable")
        day, close = max(previous, key=lambda item: item[0])
        return day, close, {"source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest()}

    def fetch(self, target_session_date: date,
              expected_previous_session_date: date | None = None) -> dict[str, Any]:
        listing = self.listing_provider.fetch()
        if listing.get("provider") == "nasdaqtrader_symbol_directory":
            directory_dates = {source["file_creation_date"] for source in listing["directory_files"]}
            if directory_dates != {target_session_date.isoformat()}:
                raise MarketDataError("Nasdaq Trader directory date differs from target session")
        eligible = {str(row["symbol"]).strip().upper(): row for row in listing["rows"]
                    if isinstance(row, dict) and eligible_screener_row(row)}
        if len(eligible) < 100:
            raise MarketDataError("regular-universe listing has too few eligible stocks")
        symbols = sorted(eligible)
        batches = [symbols[index:index + 20] for index in range(0, len(symbols), 20)]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(pool.map(lambda batch: self._spark_batch(batch, target_session_date), batches))
        quotes = [quote for batch_quotes, _ in results for quote in batch_quotes]
        quote_symbols = {quote["symbol"] for quote in quotes}
        if len(quote_symbols) != len(quotes):
            raise MarketDataError("regular-universe has duplicate dated symbol quotes")
        missing = sorted(set(symbols) - quote_symbols)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            verified_missing = list(pool.map(
                lambda symbol: self._verify_missing(symbol, str(eligible[symbol]["name"]), target_session_date),
                missing,
            ))
        quotes.extend(quote for quote, _ in verified_missing if quote is not None)
        if len({quote["symbol"] for quote in quotes}) != len(quotes):
            raise MarketDataError("regular-universe official recovery duplicated a symbol")
        discovery_top = sorted(quotes, key=lambda item: item["change_pct"], reverse=True)[:50]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            top_official = list(pool.map(
                lambda item: self._verify_missing(item["symbol"], str(eligible[item["symbol"]]["name"]),
                                                  target_session_date),
                discovery_top,
            ))
        nasdaq_top = [item for item, (_, evidence) in zip(discovery_top, top_official)
                      if evidence["primary_exchange"].upper().startswith("NASDAQ")]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            official_prior = dict(zip(
                (item["symbol"] for item in nasdaq_top),
                pool.map(lambda item: self._official_previous(item["symbol"], target_session_date,
                                                              item["close"]), nasdaq_top),
            ))
        official_by_symbol = {}
        for discovered, (official, evidence) in zip(discovery_top, top_official):
            if official is None:
                raise MarketDataError(f"regular-universe top candidate {discovered['symbol']} lacks a dated close")
            if discovered["symbol"] in official_prior:
                previous_day, previous_close, prior_evidence = official_prior[discovered["symbol"]]
                if expected_previous_session_date is not None and previous_day != expected_previous_session_date:
                    raise MarketDataError(
                        f"regular-universe {discovered['symbol']} official previous session "
                        f"{previous_day} differs from {expected_previous_session_date}"
                    )
                official["change_pct"] = (official["close"] / previous_close - 1.0) * 100.0
                evidence["official_previous_session_date"] = previous_day.isoformat()
                evidence["official_previous_close"] = previous_close
                evidence["official_previous_source_identifier"] = prior_evidence["source_identifier"]
                evidence["official_previous_raw_response_sha256"] = prior_evidence["raw_response_sha256"]
                if prior_evidence.get("official_action"):
                    evidence["official_action"] = prior_evidence["official_action"]
                    evidence["official_previous_price_basis"] = prior_evidence["previous_price_basis"]
                    evidence["applied_split_factor"] = prior_evidence["applied_split_factor"]
            official_by_symbol[discovered["symbol"]] = {
                **official,
                "discovery_close": discovered["close"],
                "discovery_change_pct": discovered["change_pct"],
                "discovery_source_identifier": discovered["quote_source_identifier"],
                "discovery_raw_response_sha256": discovered["quote_raw_response_sha256"],
            }
            evidence["discovery_change_pct"] = discovered["change_pct"]
            evidence["discovery_raw_response_sha256"] = discovered["quote_raw_response_sha256"]
            evidence["discovery_target_close"] = discovered["close"]
            evidence["official_target_close"] = official["close"]
        rows = []
        for quote_row in quotes:
            quote_row = official_by_symbol.get(quote_row["symbol"], quote_row)
            row = eligible[quote_row["symbol"]]
            rows.append({"symbol": quote_row["symbol"], "name": row["name"],
                         "instrument_type": "common_stock", "lastsale": quote_row["close"],
                         "pctchange": quote_row["change_pct"], "volume": quote_row["volume"],
                         "marketCap": row.get("marketCap"),
                         "quote_source_identifier": quote_row["quote_source_identifier"],
                         "quote_raw_response_sha256": quote_row["quote_raw_response_sha256"],
                         **({"discovery_change_pct": quote_row["discovery_change_pct"],
                             "discovery_raw_response_sha256": quote_row["discovery_raw_response_sha256"]}
                            if "discovery_change_pct" in quote_row else {}),
                         **({"previous_session_date": quote_row["previous_session_date"]}
                            if "previous_session_date" in quote_row else {})})
        return {"provider": self.name, "source_identifier": listing["source_identifier"],
                "retrieved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                "raw_response_sha256": listing["raw_response_sha256"], "rows": rows,
                "universe_evidence": {"eligible_count": len(symbols), "quoted_count": len(quotes),
                                      "listing_directory_files": listing.get("directory_files", []),
                                      "spark_batches": [item for _, item in results],
                                      "missing_quote_audit": [item for _, item in verified_missing],
                                      "top_candidate_official_audit": [item for _, item in top_official]}}


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

        nls_session_date = self.now().astimezone(NY_TZ).date()
        nls_url = (
            f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/realtime-trades?"
            + urlencode({"assetclass": "stocks", "limit": 5000})
        )
        nls_raw = None
        top_rows = None
        if target_session_date == nls_session_date:
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
                vendor_basis = None
                if target_session_date == self.now().astimezone(NY_TZ).date() and nls_previous is not None:
                    if close_matches(nls_previous, previous_close) and provider_marks_adjusted:
                        basis = "already_split_normalized"
                    elif close_matches(nls_previous, normalized) and not provider_marks_adjusted:
                        basis = "unadjusted"
                    else:
                        basis = None
                else:
                    basis = "already_split_normalized" if provider_marks_adjusted else None
                if basis is None:
                    vendor = YahooCorporateActionProvider(transport=self.transport).fetch(symbol, target_session_date)
                    if not vendor.get("events"):
                        # A vendor may omit the action while leaving its daily
                        # history on the old share basis.  Require an exchange
                        # notice and a second publisher's explicitly adjusted,
                        # dated price pair; a price gap alone proves nothing.
                        independent_history = StockAnalysisHistoricalCloseProvider(
                            transport=self.transport,
                        ).fetch_pair(symbol, previous_day, target_session_date)
                        if (independent_history["price_basis"] != "regular_close_normalized_for_split"
                                or independent_history["previous_session_date"] != previous_day.isoformat()
                                or independent_history["target_session_date"] != target_session_date.isoformat()):
                            raise MarketDataError(f"{self.name}:{symbol}: independent split dates or basis disagree")
                        displayed_previous = float(independent_history["previous_split_normalized_close"])
                        digits = int(independent_history["previous_display_decimal_places"])
                        if (provider_marks_adjusted
                                or not close_matches(displayed_previous, round_market_display(normalized, digits))
                                or close_matches(displayed_previous, round_market_display(previous_close, digits))):
                            raise MarketDataError(f"{self.name}:{symbol}: split basis without vendor event is unconfirmed")
                        basis = "unadjusted"
                        vendor_basis = {**vendor, "action_status": "vendor_event_missing_independent_history_used"}
                    else:
                        try:
                            validate_vendor_actions(vendor["events"], notice)
                        except ValueError as exc:
                            raise MarketDataError(f"{self.name}:{symbol}: vendor action disagrees with exchange: {exc}") from exc
                        stamp = vendor.get("regular_market_time")
                        vendor_day = (datetime.fromtimestamp(stamp, timezone.utc).astimezone(NY_TZ).date()
                                      if isinstance(stamp, int) else None)
                        vendor_previous = _numeric(vendor.get("chart_previous_close"), "vendor split-adjusted previous")
                        if (vendor.get("meta_symbol") != symbol
                                or vendor.get("exchange_timezone_name") != "America/New_York"
                                or vendor_day != target_session_date):
                            raise MarketDataError(f"{self.name}:{symbol}: dated vendor split basis unavailable")
                        matches_raw = close_matches(vendor_previous, previous_close)
                        matches_adjusted = close_matches(vendor_previous, normalized)
                        if matches_raw == matches_adjusted:
                            independent_history = StockAnalysisHistoricalCloseProvider(
                                transport=self.transport,
                            ).fetch_pair(symbol, previous_day, target_session_date)
                            if (independent_history["price_basis"] != "regular_close_normalized_for_split"
                                    or independent_history["previous_session_date"] != previous_day.isoformat()
                                    or independent_history["target_session_date"] != target_session_date.isoformat()):
                                raise MarketDataError(f"{self.name}:{symbol}: independent split dates or basis disagree")
                            displayed_previous = float(independent_history["previous_split_normalized_close"])
                            digits = int(independent_history["previous_display_decimal_places"])
                            matches_raw = close_matches(displayed_previous, round_market_display(previous_close, digits))
                            matches_adjusted = close_matches(displayed_previous, round_market_display(normalized, digits))
                            if matches_raw == matches_adjusted:
                                raise MarketDataError(f"{self.name}:{symbol}: independent split basis is ambiguous")
                        else:
                            independent_history = None
                        basis = "already_split_normalized" if matches_raw else "unadjusted"
                        vendor_basis = vendor
                normalization = {"previous_close_raw": previous_close, "previous_price_basis": basis,
                                 "applied_split_factor": notice["old_shares"] / notice["new_shares"] if basis == "unadjusted" else 1.0,
                                 "official_action": notice,
                                 "historical_volume_basis_marker": volume_marker}
                if vendor_basis:
                    normalization["independent_split_basis"] = vendor_basis
                    if independent_history:
                        normalization["independent_adjusted_history_basis"] = independent_history
                if basis == "unadjusted":
                    previous_close = normalized
                normalization["previous_close_on_target_basis"] = True
        # NLS is a live quote. Its previousClose belongs to the current New York
        # trading day, so it cannot corroborate a historical session's prior close.
        nls_previous_mismatch = (
            target_session_date == nls_session_date
            and nls_previous is not None
            and abs(nls_previous - previous_close) > max(0.02, previous_close * 0.002)
        )
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
        info_historical_conflict = None
        if target_rows:
            historical_target_close = _numeric(target_rows[0].get("close"), "official target close")
            if abs(target_close - historical_target_close) > max(1e-7, historical_target_close * 1e-6):
                primary_exchange = str(info.get("exchange") or "").upper().strip()
                if (not primary_exchange.startswith("NASDAQ")
                        and primary_exchange not in {"AMEX", "NYSE", "NYSE AMERICAN", "NYSEAMERICAN", "NYSE ARCA"}):
                    raise MarketDataError(f"{self.name}:{symbol}: official info/historical target close mismatch")
                info_historical_conflict = {
                    "primary_exchange": primary_exchange,
                    "nasdaq_info_close": target_close,
                    "nasdaq_historical_close": historical_target_close,
                    "resolution_required": ("independent_dated_nasdaq_eod" if primary_exchange.startswith("NASDAQ")
                                            else "independent_primary_exchange_eod"),
                }
                if primary_exchange.startswith("NASDAQ"):
                    # A live info response can retain the target day's 4 PM
                    # label while its last-sale price changes the next day.
                    # The dated historical row is immutable evidence, but a
                    # separate dated EOD publisher must still corroborate it.
                    target_close = historical_target_close
                    target_stamp = f"Nasdaq historical close {target_session_date:%m/%d/%Y}"
                    target_close_source = "nasdaq_historical"
        if normalization.get("independent_split_basis") and not close_matches(
            _numeric(normalization["independent_split_basis"].get("regular_market_price"),
                     "vendor split-date regular close"), target_close,
        ):
            raise MarketDataError(f"{self.name}:{symbol}: vendor split-date target differs from exchange close")
        independent_history = normalization.get("independent_adjusted_history_basis")
        if independent_history:
            previous_digits = int(independent_history["previous_display_decimal_places"])
            target_digits = int(independent_history["target_display_decimal_places"])
            reported = str(independent_history.get("reported_change_raw") or "")
            official_change = (target_close / previous_close - 1.0) * 100.0
            displayed_previous = float(independent_history["previous_split_normalized_close"])
            displayed_target = float(independent_history["target_regular_close"])
            previous_half_unit = 0.5 * 10 ** -previous_digits
            target_half_unit = 0.5 * 10 ** -target_digits
            if displayed_previous <= previous_half_unit or displayed_target <= target_half_unit:
                raise MarketDataError(f"{self.name}:{symbol}: independent split display precision is invalid")
            # A publisher's printed closes bound the return of its underlying
            # prices. Compare the reported return with that derived interval,
            # rather than an arbitrary fixed tolerance on rounded closes.
            return_interval = [
                ((displayed_target - target_half_unit) / (displayed_previous + previous_half_unit) - 1) * 100,
                ((displayed_target + target_half_unit) / (displayed_previous - previous_half_unit) - 1) * 100,
            ]
            if (not close_matches(float(independent_history["target_regular_close"]),
                                  round_market_display(target_close, target_digits))
                    or not close_matches(displayed_previous,
                                         round_market_display(previous_close, previous_digits))
                    or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", reported)
                    or not return_interval[0] <= float(reported[:-1]) <= return_interval[1]
                    or not return_interval[0] <= official_change <= return_interval[1]):
                raise MarketDataError(f"{self.name}:{symbol}: independent split target or return disagrees")
            normalization["independent_reported_return_display_interval_pct"] = return_interval
        return {
            "provider": self.name, "provider_family": self.family,
            "target_close_verified": True, "target_timestamp": target_stamp,
            "target_close_source": target_close_source,
            "target_session_date": target_session_date.isoformat(),
            "notifications": info.get("notifications", []),
            "previous_session_date": previous_day.isoformat(),
            "previous_close": previous_close, "target_close": target_close,
            "nls_previous_close": nls_previous,
            "nls_previous_mismatch": nls_previous_mismatch,
            **({"info_historical_conflict": info_historical_conflict} if info_historical_conflict else {}),
            **normalization,
            "previous_volume": _numeric(previous_row.get("volume"), "official previous volume", nullable=True),
            "source_identifiers": [historical_url, *([nls_url] if nls_raw is not None else []), info_url],
            "raw_response_sha256": [hashlib.sha256(historical_raw).hexdigest(),
                                    *([hashlib.sha256(nls_raw).hexdigest()] if nls_raw is not None else []),
                                    hashlib.sha256(info_raw).hexdigest()],
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
        meta = results[0].get("meta") or {}
        found = bool(events)
        return {
            "provider": self.name, "provider_family": self.family,
            "status": "corporate_action_found" if found else "checked_none",
            "events": events or {}, "source_identifier": url,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "meta_symbol": meta.get("symbol"),
            "chart_previous_close": meta.get("chartPreviousClose"),
            "regular_market_price": meta.get("regularMarketPrice"),
            "regular_market_time": meta.get("regularMarketTime"),
            "exchange_timezone_name": meta.get("exchangeTimezoneName"),
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
        if (not re.fullmatch(rf"{re.escape(symbol)} Historical Prices(?: - [^|]+)? \| ChartExchange", title)
                or not canonical or canonical.get("href") != url):
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
        # The page embeds its last regular quote alongside the historical
        # table. Keep its clock and price so an earlier quote is never
        # mistaken for the exchange's final 4 PM close.
        quote_metadata = re.findall(
            r'regDate\\":\\"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\\".{0,120}?initialPrice\\":(\d+(?:\.\d+)?)',
            raw.decode("utf-8"),
        )
        if len(set(quote_metadata)) > 1:
            raise MarketDataError(f"{self.name}:{symbol}: embedded regular quote ambiguous")
        quote_stamp, quote_price = quote_metadata[0] if quote_metadata else (None, None)
        return {
            "provider": self.name, "provider_family": "chartexchange",
            "session": "regular_close", "target_session_date": target_session_date.isoformat(),
            "previous_close": previous_close, "target_close": close,
            "raw_value": close_raw, "parsed_value": close,
            "reported_change_raw": str(close - previous_close), "reported_pct_raw": pct_raw,
            "display_decimal_places": len(close_raw.partition(".")[2]),
            "primary_exchange": urlsplit(url).path.split("/")[2].rsplit("-", 1)[0].upper(),
            "capture_type": "captured_historical_html",
            "last_regular_quote_timestamp": quote_stamp,
            "last_regular_quote_price": float(quote_price) if quote_price is not None else None,
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
    canonical_primary_pair: tuple[float, float] | None = None
    # An exchange-confirmed split can leave the vendor's previous daily bar on
    # the old share basis even when the target bar is already on the new basis.
    # The official provider established the old basis from the dated Nasdaq
    # history row, its volume marker, the live NLS previous close and the notice.
    notice = corporate_action.get("official_action")
    if reason is None and notice and official.get("previous_price_basis") == "unadjusted":
        effective = date.fromisoformat(notice["effective_session_date"])
        previous_session = date.fromisoformat(official["previous_session_date"])
        target_session = date.fromisoformat(official["target_session_date"])
        factor = notice["old_shares"] / notice["new_shares"]
        raw_previous = official.get("previous_close_raw")
        vendor_rounding = next((digits for digits in range(2, 5) if raw_previous is not None
                                and close_matches(historical_previous_close,
                                                  round_market_display(float(raw_previous), digits))), None)
        vendor_matches_raw = raw_previous is not None and close_matches(historical_previous_close, float(raw_previous))
        if (previous_session < effective <= target_session
                and official.get("previous_close_on_target_basis") is True
                and official.get("target_close_verified") is True
                and raw_previous is not None
                and close_matches(float(raw_previous) * factor, official_previous)
                and (vendor_matches_raw or vendor_rounding is not None)
                and close_matches(historical_target_close, official_target)
                and official_supports and independent_support):
            reason = "official_split_unadjusted_vendor_previous"
            basis_evidence = {
                "official_action": notice,
                "official_previous_close_raw": float(raw_previous),
                "official_previous_price_basis": "unadjusted",
                "applied_split_factor": factor,
                "official_previous_close": official_previous,
                "official_target_close": official_target,
                "vendor_daily_previous_close_raw": historical_previous_close,
                "vendor_daily_previous_rounding_decimal_places": vendor_rounding,
                "vendor_daily_target_close": historical_target_close,
                "independent_previous_close_support": independent_support,
                "comparison_basis": "dated_regular_closes_on_target_share_basis",
            }
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

    # Some thinly traded symbols have no 16:00 minute observation. A vendor
    # daily close rounded from the exchange's dated close can still be
    # diagnosed when an independent publisher displays that same rounded
    # previous close and the exchange verifies the target regular close.
    if (reason is None and minute_close is None and action_status == "checked_none"
            and official.get("target_close_verified") is True and official_supports):
        rounded_places = next((digits for digits in range(2, 5)
                               if close_matches(historical_previous_close,
                                                round_market_display(official_previous, digits))
                               and not close_matches(historical_previous_close, official_previous)), None)
        rounded_support = [source for source in independent_support
                           if source.get("previous_close") is not None
                           and close_matches(float(source["previous_close"]), historical_previous_close)]
        if (rounded_places is not None and rounded_support
                and close_matches(historical_target_close, official_target)):
            reason = "provider_rounding_independently_corroborated"
            basis_evidence = {
                "official_previous_close": official_previous,
                "vendor_daily_previous_close": historical_previous_close,
                "vendor_daily_previous_decimal_places": rounded_places,
                "official_target_close": official_target,
                "vendor_daily_target_close": historical_target_close,
                "independent_rounded_previous_close_support": rounded_support,
                "comparison_basis": "dated_regular_closes_with_display_rounding",
            }

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

    if (reason is None or (reason == "stale_daily_bar" and not official_supports)
            or (official.get("info_historical_conflict")
                and reason == "official_closed_eod_with_secondary_boundary_difference")) and action_status == "checked_none":
        non_nasdaq_eod = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("primary_exchange")
            and "NASDAQ" not in str(source.get("primary_exchange")).upper()
            and (abs(float(source["target_close"]) - screener_last)
                 <= max(0.02, screener_last * 0.002)
                 or (official.get("info_historical_conflict")
                     and close_matches(float(source["target_close"]),
                                       float(official["info_historical_conflict"]["nasdaq_historical_close"]))))
            and abs(float(source["previous_close"]) - implied_previous)
            <= max(0.02, implied_previous * 0.002)
            and abs(historical_target_close - screener_last)
            <= max(0.02, screener_last * 0.002)
        )]
        if non_nasdaq_eod:
            prior_classification = reason
            reason = "non_nasdaq_primary_exchange_eod"
            official_supports = True
            primary_previous = float(non_nasdaq_eod[0]["previous_close"])
            primary_target = float(non_nasdaq_eod[0]["target_close"])
            # A two-decimal display may only corroborate a more precise
            # vendor close; never replace that precise value with rounding.
            primary_daily_rounding = next((digits for digits in range(2, 5)
                                           if close_matches(historical_previous_close,
                                                            round_market_display(primary_previous, digits))
                                           and close_matches(historical_target_close,
                                                             round_market_display(primary_target, digits))), None)
            if (close_matches(primary_previous, historical_previous_close)
                    and close_matches(primary_target, historical_target_close)):
                canonical_primary_pair = (primary_previous, primary_target)
            elif (official.get("info_historical_conflict")
                  and primary_daily_rounding is not None
                  and close_matches(primary_previous, official_previous)
                  and close_matches(primary_target,
                                    float(official["info_historical_conflict"]["nasdaq_historical_close"]))):
                canonical_primary_pair = (primary_previous, primary_target)
            basis_evidence = {
                "primary_exchange": non_nasdaq_eod[0]["primary_exchange"],
                "primary_exchange_previous_close": primary_previous,
                "primary_exchange_eod_close": float(non_nasdaq_eod[0]["target_close"]),
                "nasdaq_info_close": official_target,
                **({"nasdaq_historical_close": official["info_historical_conflict"]["nasdaq_historical_close"]}
                   if official.get("info_historical_conflict") else {}),
                "screener_close": screener_last,
                "vendor_daily_close": historical_target_close,
                "vendor_daily_display_rounding_places": primary_daily_rounding,
                "comparison_basis": "primary_exchange_regular_close",
                "nasdaq_info_role": "nasdaq_reported_last_sale_not_primary_exchange_close",
                "previous_discrepancy_classification": prior_classification,
            }

    # A live movers table can keep updating after the regular close while its
    # priceDate still names the finished session. Correct that quote only when
    # the dated vendor daily pair and the exchange's 4 PM close agree exactly,
    # and a separate dated publisher corroborates the regular-close pair.
    # This diagnoses a source conflict; it does not widen the price tolerance.
    vendor_previous_rounding_places = next((digits for digits in range(2, 5)
                                            if close_matches(historical_previous_close,
                                                             round_market_display(official_previous, digits))), None)
    if (reason in {None, "official_closed_eod_with_secondary_boundary_difference"}
            and action_status == "checked_none"
            and official.get("target_close_verified") is True
            and official.get("target_close_source") == "nasdaq_info"
            and re.fullmatch(r"Closed at .* 4:00 PM ET", str(official.get("target_timestamp", "")))
            and vendor_previous_rounding_places is not None
            and close_matches(historical_target_close, official_target)
            and not close_matches(screener_last, official_target)):
        dated_eod = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("target_close") is not None
            and source.get("previous_close") is not None
            and close_matches(float(source["previous_close"]), official_previous)
        )]
        confirmed_dated_eod = [source for source in dated_eod if (
            close_matches(float(source["target_close"]), official_target)
            or abs(float(source["target_close"]) - official_target)
            <= 0.5 * 10 ** -int(source.get("display_decimal_places", 8)) + 1e-12
        )]
        preclose_quotes = [source for source in independent_sources if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and re.fullmatch(rf"{re.escape(str(official['target_session_date']))} "
                             r"(?:09:(?:3\d|[4-5]\d)|1[0-5]:[0-5]\d):[0-5]\d",
                             str(source.get("last_regular_quote_timestamp", "")))
            and source.get("target_close") is not None
            and source.get("last_regular_quote_price") is not None
            and close_matches(float(source["target_close"]), float(source["last_regular_quote_price"]))
            and source.get("previous_close") is not None
            and close_matches(float(source["previous_close"]), official_previous)
        )]
        nls_conflict_verified = (not official.get("nls_previous_mismatch")
                                 or any(close_matches(float(source["target_close"]), official_target)
                                        for source in dated_eod))
        if ((reason is None and (dated_eod or preclose_quotes))
                or (reason == "official_closed_eod_with_secondary_boundary_difference" and confirmed_dated_eod)) and nls_conflict_verified:
            prior_classification = reason
            known_hashes = {source.get("raw_response_sha256") for source in independent_support}
            independent_support.extend({**source, "support_role": "independent_preclose_quote"}
                                       for source in preclose_quotes
                                       if source.get("raw_response_sha256") not in known_hashes)
            reason = "movers_quote_differs_from_verified_regular_close"
            official_supports = True
            basis_evidence = {
                "official_previous_close": official_previous,
                "nls_previous_close": official.get("nls_previous_close"),
                "nls_previous_mismatch": official.get("nls_previous_mismatch", False),
                "official_target_close": official_target,
                "official_target_timestamp": official["target_timestamp"],
                "vendor_daily_previous_close": historical_previous_close,
                "vendor_daily_previous_rounding_places": vendor_previous_rounding_places,
                "vendor_daily_target_close": historical_target_close,
                "movers_quote_close": screener_last,
                "movers_quote_change_pct": screener_change,
                "independent_dated_eod": dated_eod,
                "independent_preclose_quotes": preclose_quotes,
                "previous_discrepancy_classification": prior_classification,
                "comparison_basis": "dated_regular_close_with_conflicting_movers_quote",
            }

    # A dated exchange discovery quote can use a precise prior close while
    # the daily vendor rounds that same prior close. Require independent
    # corroboration of the precise prior and the exchange's dated 4 PM close.
    if (reason is None and action_status == "checked_none" and official_supports
            and official.get("target_close_verified") is True
            and official.get("target_close_source") == "nasdaq_info"
            and re.fullmatch(r"Closed at .* 4:00 PM ET", str(official.get("target_timestamp", "")))
            and vendor_previous_rounding_places is not None
            and not close_matches(historical_previous_close, official_previous)
            and close_matches(historical_target_close, official_target)
            and independent_support):
        reason = "official_regular_pair_over_vendor_previous_rounding"
        basis_evidence = {
            "official_previous_close": official_previous,
            "official_target_close": official_target,
            "vendor_daily_previous_close": historical_previous_close,
            "vendor_daily_previous_rounding_places": vendor_previous_rounding_places,
            "vendor_daily_target_close": historical_target_close,
            "independent_previous_close_support": independent_support,
            "comparison_basis": "dated_official_regular_close_and_independent_precise_previous",
        }

    # The exchange's live info close can disagree with its subsequently
    # published, date-specific historical close. Use the historical pair only
    # when the vendor daily target is identical and an independent publisher
    # confirms the precise previous close. A publisher's last pre-close quote
    # may support identity, but is never substituted for the final close.
    quote_source = str(candidate.get("quote_source_identifier") or "")
    expected_info = f"https://api.nasdaq.com/api/quote/{quote(str(candidate.get('symbol') or ''), safe='')}/info?assetclass=stocks"
    quote_parts = urlsplit(quote_source)
    yahoo_dated_quote = (quote_parts.hostname in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"}
                         and quote_parts.path == f"/v8/finance/chart/{quote(str(candidate.get('symbol') or ''), safe='')}")
    yahoo_target_rounding_places = next((digits for digits in range(2, 5)
                                         if close_matches(historical_target_close,
                                                          round_market_display(official_target, digits))), None)
    yahoo_previous_rounding = (yahoo_dated_quote
                               and vendor_previous_rounding_places is not None
                               and yahoo_target_rounding_places is not None
                               and close_matches(screener_last, historical_target_close)
                               and (abs(screener_change - historical_change_pct) <= tolerance_pct
                                    or abs(screener_change - (screener_last / official_previous - 1.0) * 100.0)
                                    <= tolerance_pct))
    if (not official_supports and action_status == "checked_none"
            and official.get("target_close_verified") is True
            and official.get("target_close_source") == "nasdaq_historical"
            and str(official.get("target_timestamp", "")).startswith("Nasdaq historical close ")
            and (quote_source == expected_info or yahoo_previous_rounding)
            and (close_matches(historical_target_close, official_target)
                 or yahoo_previous_rounding)):
        dated_previous = [source for source in independent_sources if (
            provider_family(str(source.get("provider_family") or source.get("provider") or ""))
            not in {"nasdaq", provider_family(historical_provider)}
            and source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("previous_close") is not None
            and abs(float(source["previous_close"]) - official_previous)
            <= 0.5 * 10 ** -int(source.get("display_decimal_places", 8)) + 1e-12
            and source.get("target_close") is not None
            and (close_matches(float(source["target_close"]), official_target)
                 or (source.get("last_regular_quote_price") is not None
                     and close_matches(float(source["target_close"]),
                                       float(source["last_regular_quote_price"]))
                     and re.fullmatch(
                         rf"{re.escape(str(official['target_session_date']))} "
                         r"(?:09:(?:3\d|[4-5]\d)|1[0-5]:[0-5]\d):[0-5]\d",
                         str(source.get("last_regular_quote_timestamp", "")),
                     ))
                 or (source.get("price_basis") == "regular_close_normalized_for_split"
                     and re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", str(source.get("reported_change_raw") or ""))
                     and 2 <= int(source.get("display_decimal_places", 0)) <= 6
                     and abs(float(str(source["reported_change_raw"]).rstrip("%")) - official_change) <= 0.01
                     and close_matches(float(source["target_close"]),
                                       round_market_display(official_target,
                                                            int(source.get("display_decimal_places", 0))))))
        )]
        if dated_previous:
            known_hashes = {source.get("raw_response_sha256") for source in independent_support}
            dated_roles = {
                source.get("raw_response_sha256"): (
                    "independent_precise_previous_preclose_quote" if source.get("last_regular_quote_timestamp")
                    else "independent_reported_change_and_rounded_pair" if source.get("reported_change_raw")
                    else "independent_dated_regular_pair")
                for source in dated_previous
            }
            independent_support = [
                {**source, "support_role": dated_roles.get(source.get("raw_response_sha256"),
                                                       source.get("support_role", "independent_support"))}
                for source in independent_support
            ]
            independent_support.extend({**source, "support_role": (
                "independent_precise_previous_preclose_quote" if source.get("last_regular_quote_timestamp")
                else "independent_reported_change_and_rounded_pair")}
                                       for source in dated_previous
                                       if source.get("raw_response_sha256") not in known_hashes)
            prior_classification = reason
            reason = ("dated_exchange_history_over_vendor_previous_rounding" if yahoo_previous_rounding
                      else "dated_exchange_history_over_conflicting_info_quote")
            official_supports = True
            canonical_primary_pair = (official_previous, official_target)
            basis_evidence = {
                "official_previous_close": official_previous,
                "official_historical_target_close": official_target,
                "official_historical_target_timestamp": official["target_timestamp"],
                "conflicting_info_quote_close": screener_last,
                "conflicting_info_quote_change_pct": screener_change,
                "conflicting_info_quote_source_identifier": quote_source,
                "vendor_daily_previous_close": historical_previous_close,
                "vendor_daily_target_close": historical_target_close,
                "vendor_previous_rounding_places": vendor_previous_rounding_places,
                "vendor_target_rounding_places": yahoo_target_rounding_places,
                "previous_discrepancy_classification": prior_classification,
                "independent_previous_close_support": [
                    {**source, "support_role": ("independent_precise_previous_preclose_quote"
                                               if source.get("last_regular_quote_timestamp")
                                               else "independent_reported_change_and_rounded_pair"
                                               if source.get("reported_change_raw")
                                               else "independent_dated_regular_pair")}
                    for source in dated_previous
                ],
                "comparison_basis": "dated_exchange_historical_regular_closes",
            }

    conflict = official.get("info_historical_conflict")
    if conflict and str(conflict["primary_exchange"]).startswith("NASDAQ"):
        dated_nasdaq_eod = [source for source in independent_sources if (
            provider_family(str(source.get("provider_family") or source.get("provider") or ""))
            not in {"nasdaq", provider_family(historical_provider)}
            and source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and "NASDAQ" in str(source.get("primary_exchange") or "").upper()
            and source.get("previous_close") is not None
            and source.get("target_close") is not None
            and close_matches(float(source["previous_close"]), official_previous)
            and close_matches(float(source["target_close"]), float(conflict["nasdaq_historical_close"]))
        )]
        vendor_rounding_places = next((digits for digits in range(2, 5)
                                       if close_matches(historical_previous_close,
                                                        round_market_display(official_previous, digits))
                                       and close_matches(historical_target_close,
                                                         round_market_display(official_target, digits))), None)
        if (action_status != "checked_none" or not dated_nasdaq_eod
                or vendor_rounding_places is None):
            raise MarketDataError("unresolved Top20 discrepancy: Nasdaq info/history conflict lacks independent dated EOD pair")
        prior_classification = reason
        reason = "dated_exchange_history_over_conflicting_info_quote"
        official_supports = True
        canonical_primary_pair = (official_previous, official_target)
        basis_evidence = {
            "primary_exchange": conflict["primary_exchange"],
            "official_previous_close": official_previous,
            "nasdaq_info_close": conflict["nasdaq_info_close"],
            "nasdaq_historical_close": conflict["nasdaq_historical_close"],
            "vendor_daily_previous_close": historical_previous_close,
            "vendor_daily_target_close": historical_target_close,
            "vendor_daily_display_rounding_places": vendor_rounding_places,
            "independent_dated_eod": dated_nasdaq_eod,
            "previous_discrepancy_classification": prior_classification,
            "comparison_basis": "dated_nasdaq_historical_regular_close",
        }
    elif conflict and (reason != "non_nasdaq_primary_exchange_eod"
                       or canonical_primary_pair is None
                       or not close_matches(canonical_primary_pair[0], official_previous)
                       or not close_matches(canonical_primary_pair[1], float(conflict["nasdaq_historical_close"]))):
        raise MarketDataError("unresolved Top20 discrepancy: non-Nasdaq info/history conflict lacks exact independent primary EOD pair")

    # A large move is still a dated regular-close move when the exchange,
    # daily history, and a separate publisher's rounded pair and reported
    # return agree. The publisher's precision is evidence, not a replacement
    # for the exchange's exact price.
    if (reason is None and action_status == "checked_none" and official_supports
            and official.get("target_close_verified") is True
            and official.get("info_historical_conflict") is None
            and (minute_close is None or (
                close_matches(float(minute_close["previous_close"]), official_previous)
                and close_matches(float(minute_close["target_close"]), official_target)))
            and close_matches(historical_previous_close, official_previous)
            and close_matches(historical_target_close, official_target)):
        dated_pair = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("price_basis") == "regular_close_normalized_for_split"
            and 2 <= int(source.get("display_decimal_places", 0)) <= 6
            and source.get("previous_close") is not None
            and source.get("target_close") is not None
            and close_matches(float(source["previous_close"]), round_market_display(
                official_previous, int(source["display_decimal_places"])))
            and close_matches(float(source["target_close"]), round_market_display(
                official_target, int(source["display_decimal_places"])))
            and re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", str(source.get("reported_change_raw") or ""))
            and abs(float(str(source["reported_change_raw"]).rstrip("%")) - official_change) <= 0.01
        )]
        if dated_pair:
            reason = "dated_official_regular_pair_independently_confirmed"
            basis_evidence = {
                "official_previous_close": official_previous,
                "official_target_close": official_target,
                "vendor_daily_previous_close": historical_previous_close,
                "vendor_daily_target_close": historical_target_close,
                "independent_dated_regular_pair": dated_pair,
                "comparison_basis": "dated_regular_close_with_independent_reported_return",
            }

    # The full-universe Yahoo Spark feed and its daily chart can both print
    # only cents. Keep the exchange's precise regular-close pair when a dated
    # independent publisher prints the same rounded pair and independently
    # reports the return calculated from the precise pair.
    quote_source = str(candidate.get("quote_source_identifier") or "")
    quote_parts = urlsplit(quote_source)
    if (reason in {None, "official_closed_eod_with_secondary_boundary_difference"}
            and action_status == "checked_none" and not official_supports
            and official.get("target_close_verified") is True
            and official.get("info_historical_conflict") is None
            and not official.get("nls_previous_mismatch")
            and quote_parts.hostname in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"}
            and quote_parts.path == "/v7/finance/spark"
            and close_matches(screener_last, round_market_display(official_target, 2))
            and close_matches(historical_previous_close, round_market_display(official_previous, 2))
            and close_matches(historical_target_close, round_market_display(official_target, 2))
            and abs(screener_change - historical_change_pct) <= tolerance_pct):
        dated_pair = [source for source in independent_support if (
            source.get("session") == "regular_close"
            and source.get("target_session_date") == official.get("target_session_date")
            and source.get("price_basis") == "regular_close_normalized_for_split"
            and int(source.get("display_decimal_places", 0)) == 2
            and source.get("previous_close") is not None
            and source.get("target_close") is not None
            and close_matches(float(source["previous_close"]), round_market_display(official_previous, 2))
            and close_matches(float(source["target_close"]), round_market_display(official_target, 2))
            and re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", str(source.get("reported_change_raw") or ""))
            and abs(float(str(source["reported_change_raw"]).rstrip("%")) - official_change) <= 0.01
        )]
        if dated_pair:
            prior_classification = reason
            reason = "dated_official_regular_pair_over_rounded_discovery"
            official_supports = True
            canonical_primary_pair = (official_previous, official_target)
            basis_evidence = {
                **basis_evidence,
                "official_previous_close": official_previous,
                "official_target_close": official_target,
                "rounded_discovery_close": screener_last,
                "rounded_discovery_change_pct": screener_change,
                "rounded_vendor_daily_previous_close": historical_previous_close,
                "rounded_vendor_daily_target_close": historical_target_close,
                "discovery_source_identifier": quote_source,
                "independent_dated_regular_pair": dated_pair,
                "previous_discrepancy_classification": prior_classification,
                "comparison_basis": "dated_official_regular_close_over_cent_rounded_discovery",
            }

    if (not official_supports or not action_checked or not independent_support or reason is None
            or (official.get("nls_previous_mismatch") and reason != "movers_quote_differs_from_verified_regular_close")):
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
        if official.get("nls_previous_mismatch") and reason != "movers_quote_differs_from_verified_regular_close":
            problems.append("official historical/NLS previous close mismatch is unresolved")
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
        and (abs(float(source["target_close"]) - official_target) > max(0.02, official_target * 0.002)
             or (re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", str(source.get("reported_pct_raw") or ""))
                 and abs(float(str(source["reported_pct_raw"]).rstrip("%")) - official_change)
                 > tolerance_pct))
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
        "raw_response_sha256": item.get("raw_response_sha256"),
        "role": item.get("support_role", "independent_support"),
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
        **({"canonical_previous_close": canonical_primary_pair[0],
            "canonical_target_close": canonical_primary_pair[1]} if canonical_primary_pair else {}),
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
                    and abs(float(official["target_close"]) - target.regular_close) <= max(0.02, target.regular_close * 0.002)):
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
                dated_pair = next((source for source in independent_previous if (
                    source.get("session") == "regular_close"
                    and source.get("target_session_date") == target_session_date.isoformat()
                    and source.get("target_close") is not None
                    and close_matches(float(source["target_close"]), float(official["target_close"]))
                )), None)
                quote_near_official = (abs(float(official["target_close"]) - float(candidate["_close"]))
                                       <= max(0.02, float(candidate["_close"]) * 0.002))
                if (independent_previous and (quote_near_official or dated_pair)
                        and (not official.get("nls_previous_mismatch") or dated_pair)):
                    independent_evidence = dated_pair if official.get("nls_previous_mismatch") else independent_previous[0]
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
                            {**independent_evidence, "role": "independent_dated_regular_close_pair"
                             if dated_pair else "independent_previous_close"},
                        ],
                        "basis_evidence": {
                            "previous_session_date": expected_previous_session_date.isoformat(),
                            "target_session_date": target_session_date.isoformat(),
                            "official_previous_close": official["previous_close"],
                            "official_target_close": official["target_close"],
                            "nls_previous_close": official.get("nls_previous_close"),
                            "nls_previous_mismatch": official.get("nls_previous_mismatch", False),
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
        minute_error: str | None = None
        try:
            minute = self.minute_provider.fetch(ticker, minute_previous_session, target_session_date)
        except MarketDataError as exc:
            if "16:00 close evidence missing" not in str(exc):
                raise
            minute = None
            minute_error = str(exc)
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
                # A 16:00 minute bar may begin before the exchange's final
                # close. Even when all current prices agree, a large move
                # still needs an independent dated pair and reported return.
                if not (
                    action.get("status") in {"checked_none", "corporate_action_adjusted"}
                    and official.get("target_close_verified") is True
                    and official.get("target_close_source") in {"nasdaq_info", "nasdaq_historical"}
                    and "no independent supporting source" in str(initial_error)
                    and ("provider discrepancy reason is unresolved" in str(initial_error)
                         or (minute is not None and not close_matches(
                             float(minute["target_close"]), float(official["target_close"]))))
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
            if minute_error:
                resolved["basis_evidence"] = {
                    **resolved.get("basis_evidence", {}), "minute_evidence_error": minute_error,
                }
            if resolved.get("discrepancy_reason") == "movers_quote_differs_from_verified_regular_close":
                resolved["supporting_sources"].append({
                    "provider": historical_series.provider,
                    "provider_family": provider_family(historical_series.provider),
                    "source_identifier": historical_series.source_identifier,
                    "raw_response_sha256": historical_series.raw_response_sha256,
                    "role": "dated_vendor_daily_regular_closes",
                })
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
        if action["event_type"] in {"reverse_split", "forward_split"} and prior_symbol == ticker:
            return self._resolve_same_symbol_split_history_gap(
                ticker=ticker, candidate=candidate, historical_series=historical_series,
                target=target, target_session_date=target_session_date,
                expected_previous_session_date=expected_previous_session_date,
                source_top20_floor_pct=source_top20_floor_pct, tolerance_pct=tolerance_pct,
                action=action, action_source=action_source,
            )
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

    def _resolve_same_symbol_split_history_gap(
        self, *, ticker: str, candidate: dict[str, Any], historical_series: DailySeries,
        target: DailyBar, target_session_date: date, expected_previous_session_date: date,
        source_top20_floor_pct: float, tolerance_pct: float, action: dict[str, Any],
        action_source: dict[str, Any],
    ) -> dict[str, Any]:
        """Use the exchange split basis and dated vendor metadata when prior bars vanish."""
        notice_url = self.corporate_action_notices.get(ticker)
        if not notice_url:
            raise MarketDataError(f"Top20 {ticker}: same-symbol split requires exchange notice")
        try:
            validate_notice_url(notice_url)
            notice = parse_exchange_split_notice(
                self.notice_transport(notice_url, {"User-Agent": "Mozilla/5.0"}),
                notice_url, ticker,
            )
        except ValueError as exc:
            raise MarketDataError(f"Top20 {ticker}: invalid exchange split notice: {exc}") from exc
        if (notice["effective_session_date"] != action["effective_session_date"]
                or notice["new_shares"] != action["new_shares"]
                or notice["old_shares"] != action["old_shares"]
                or notice["action_type"] != action["event_type"]):
            raise MarketDataError(f"Top20 {ticker}: exchange and SEC split evidence disagree")
        official = self.official_provider.fetch(ticker, target_session_date, float(candidate["_close"]))
        if (official.get("previous_session_date") != expected_previous_session_date.isoformat()
                or official.get("target_session_date") != target_session_date.isoformat()
                or not official.get("target_close_verified")
                or official.get("previous_price_basis") not in {"unadjusted", "already_split_normalized"}):
            raise MarketDataError(f"Top20 {ticker}: dated official split basis is incomplete")
        factor = action["old_shares"] / action["new_shares"]
        applied_factor = factor if official["previous_price_basis"] == "unadjusted" else 1.0
        official_previous = float(official["previous_close"])
        official_target = float(official["target_close"])
        if (not close_matches(float(official["previous_close_raw"]) * applied_factor, official_previous)
                or not close_matches(float(official["applied_split_factor"]), applied_factor)
                or not close_matches(target.regular_close, official_target)
                or not close_matches(float(candidate["_close"]), official_target)):
            raise MarketDataError(f"Top20 {ticker}: official split-normalized prices disagree")
        vendor = self.action_provider.fetch(ticker, target_session_date)
        if not vendor.get("events"):
            raise MarketDataError(f"Top20 {ticker}: vendor split event missing")
        try:
            validate_vendor_actions(vendor.get("events") or {}, notice)
        except ValueError as exc:
            raise MarketDataError(f"Top20 {ticker}: vendor split conflicts with official action: {exc}") from exc
        vendor_stamp = vendor.get("regular_market_time")
        vendor_day = (
            datetime.fromtimestamp(vendor_stamp, timezone.utc).astimezone(NY_TZ).date()
            if isinstance(vendor_stamp, int) else None
        )
        vendor_previous = _numeric(vendor.get("chart_previous_close"), "vendor split-adjusted previous")
        independent_history = official.get("independent_adjusted_history_basis")
        independent_previous_support = (
            official["previous_price_basis"] == "already_split_normalized"
            and isinstance(independent_history, dict)
            and independent_history.get("price_basis") == "regular_close_normalized_for_split"
            and independent_history.get("previous_session_date") == expected_previous_session_date.isoformat()
            and independent_history.get("target_session_date") == target_session_date.isoformat()
            and close_matches(
                float(independent_history["previous_split_normalized_close"]),
                round_market_display(official_previous, int(independent_history["previous_display_decimal_places"])),
            )
        )
        if (vendor.get("meta_symbol") != ticker
                or vendor.get("exchange_timezone_name") != "America/New_York"
                or vendor_day != target_session_date
                or not (close_matches(vendor_previous, official_previous) or independent_previous_support)
                or not close_matches(_numeric(vendor.get("regular_market_price"), "vendor regular close"), official_target)):
            raise MarketDataError(f"Top20 {ticker}: vendor dated split-adjusted prices disagree")
        independent_history = independent_history or self.split_history_provider.fetch_pair(
            ticker, expected_previous_session_date, target_session_date,
        )
        if (independent_history.get("provider_family") in {"nasdaq", "yahoo"}
                or independent_history.get("price_basis") != "regular_close_normalized_for_split"
                or independent_history.get("previous_session_date") != expected_previous_session_date.isoformat()
                or independent_history.get("target_session_date") != target_session_date.isoformat()
                or not close_matches(float(independent_history["previous_split_normalized_close"]),
                                     round_market_display(official_previous, int(independent_history["previous_display_decimal_places"])))
                or not close_matches(float(independent_history["target_regular_close"]),
                                     round_market_display(official_target, int(independent_history["target_display_decimal_places"])))):
            raise MarketDataError(f"Top20 {ticker}: independent dated split-adjusted pair disagrees")
        corrected_change = (official_target / official_previous - 1.0) * 100.0
        if (abs(corrected_change - float(candidate["_change_pct"])) > tolerance_pct
                or corrected_change < source_top20_floor_pct - tolerance_pct):
            raise MarketDataError(f"Top20 {ticker}: official split return disagrees with ranking")
        screener_net = _numeric(candidate.get("netchange"), "screener net change", nullable=True)
        if screener_net is not None and abs(screener_net - (official_target - official_previous)) > 0.02:
            raise MarketDataError(f"Top20 {ticker}: screener net change disagrees with split basis")
        volume = _numeric(candidate.get("volume"), "screener volume", nullable=True)
        return {
            "discrepancy_status": "resolved",
            "discrepancy_reason": "same_symbol_split_missing_vendor_previous_bar",
            "historical_change_pct": corrected_change,
            "official_previous_close": official_previous,
            "official_target_close": official_target,
            "corporate_action_status": "official_action_verified",
            "liquidity_flag": "low_liquidity" if volume is not None and volume < 250_000 else "normal_liquidity",
            "compared_providers": ["nasdaq", "sec", "yahoo", str(independent_history["provider_family"])],
            "basis_evidence": {
                "previous_session_date": expected_previous_session_date.isoformat(),
                "target_session_date": target_session_date.isoformat(),
                "previous_close_raw": float(official["previous_close_raw"]),
                "previous_price_basis": official["previous_price_basis"],
                "applied_split_factor": applied_factor,
                "previous_close_on_target_basis": official_previous,
                "target_regular_close": official_target,
                "vendor_chart_previous_close": vendor["chart_previous_close"],
                "vendor_chart_previous_agrees_with_exchange": close_matches(vendor_previous, official_previous),
                **({"independent_adjusted_history_basis": independent_history}
                   if independent_previous_support else {}),
                "vendor_regular_market_price": vendor["regular_market_price"],
                "official_action": notice,
            },
            "supporting_sources": [
                {**action_source, "role": "corporate_action_check"},
                {**notice, "provider": "nasdaq_corporate_action_notice", "provider_family": "nasdaq",
                 "role": "official_corporate_action"},
                {**official, "role": "official_market_source"},
                {**vendor, "role": "dated_vendor_split_metadata"},
                {"provider": historical_series.provider,
                 "provider_family": provider_family(historical_series.provider),
                 "source_identifier": historical_series.source_identifier,
                 "raw_response_sha256": historical_series.raw_response_sha256,
                 "role": "target_regular_close_and_missing_previous_bar"},
                {**independent_history, "role": "independent_support"},
            ],
            "resolved_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
        }

def eligible_screener_row(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    name = str(row.get("name") or "").strip()
    if not symbol or not name or EXCLUDED_INSTRUMENT_RE.search(name):
        return False
    if row.get("instrument_type") == "common_stock":
        return True
    # Nasdaq ordinary/common/class shares and foreign ordinary shares/ADS/ADR are eligible.
    allowed = re.search(
        r"(?i)(common|ordinary|\bord(?:inary)? shares?\b|american deposit(?:ary|ory)|"
        r"ADSs?\b|ADR\b|registered shares|\bclass [A-Z](?: shares?)?\b)", name,
    )
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
        series = None
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
                    if discrepancy.get("discrepancy_reason") in {
                        "official_split_normalized_eod", "official_split_unadjusted_vendor_previous",
                        "movers_quote_differs_from_verified_regular_close",
                        "official_regular_pair_over_vendor_previous_rounding",
                        "dated_exchange_history_over_conflicting_info_quote",
                        "dated_exchange_history_over_vendor_previous_rounding",
                        "dated_official_regular_pair_over_rounded_discovery",
                    } or (discrepancy.get("discrepancy_reason") == "non_nasdaq_primary_exchange_eod"
                          and "canonical_target_close" in discrepancy):
                        canonical_target = float(discrepancy.get("canonical_target_close", discrepancy["official_target_close"]))
                        canonical_previous = float(discrepancy.get("canonical_previous_close", discrepancy["official_previous_close"]))
                        corrected_change = (canonical_target / canonical_previous - 1.0) * 100.0
                        if corrected_change < source_top20_floor_pct:
                            raise MarketDataError(f"Top20 {ticker}: corrected move falls below candidate floor")
                        candidate = {**candidate, "_change_pct": corrected_change,
                                     "_close": canonical_target}
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
            "source_identifier": f"{candidate.get('quote_source_identifier', screener['source_identifier'])}#{ticker}",
            "retrieved_at": screener["retrieved_at"],
            "raw_response_sha256": candidate.get("quote_raw_response_sha256", screener["raw_response_sha256"]),
            "session_date": target_session_date.isoformat(),
            "price_basis": "regular_close",
            "adjusted": False,
            "review_flags": review_flags,
            **({"symbol_mapping_evidence": series.symbol_mapping_evidence}
               if series is not None and series.symbol_mapping_evidence else {}),
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


def audit_unranked_movers_against_regular_closes(
    screener: dict[str, Any], historical: dict[str, DailySeries],
    ranked: list[dict[str, Any]], target_session_date: date,
    excluded_actions: list[dict[str, Any]], excluded_no_trade: list[dict[str, Any]],
    symbols: list[str],
) -> list[dict[str, Any]]:
    """Fail closed when a moving quote table hides a regular-session Top20 name."""
    if screener.get("provider") not in {
        StockAnalysisGainersProvider.name, NasdaqYahooRegularUniverseProvider.name,
    }:
        return []
    if len(symbols) < 50:
        raise MarketDataError("moving gainers source has fewer than 50 eligible candidates")
    floor = min(float(item["change_pct"]) for item in ranked)
    reviewed = {item["ticker"] for item in ranked}
    reviewed.update(item["ticker"] for item in excluded_actions)
    reviewed.update(item["ticker"] for item in excluded_no_trade)
    screener_rows = {str(item["symbol"]).upper(): item for item in screener.get("rows", [])}
    audit: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for symbol in symbols:
        if symbol in reviewed:
            continue
        series = historical.get(symbol)
        if series is None:
            raise MarketDataError(f"unranked mover {symbol} lacks dated daily evidence")
        previous, target = completed_session_pair(series, target_session_date)
        historical_pct = (target.regular_close / previous.regular_close - 1.0) * 100.0
        official_source = (screener.get("provider") == NasdaqYahooRegularUniverseProvider.name
                           and str(screener_rows[symbol].get("quote_source_identifier", "")).startswith(
                               "https://api.nasdaq.com/api/quote/"))
        change_pct = float(screener_rows[symbol]["pctchange"]) if official_source else historical_pct
        record = {"symbol": symbol, "regular_change_pct": change_pct,
                  "historical_change_pct": historical_pct,
                  "ranking_basis": "dated_exchange_regular_quote" if official_source else "dated_daily_close_pair",
                  "previous_close": previous.regular_close, "target_close": target.regular_close,
                  "source_identifier": series.source_identifier,
                  "raw_response_sha256": series.raw_response_sha256}
        audit.append(record)
        if target.volume and change_pct > floor:
            hidden.append(record)
    if screener.get("provider") == NasdaqYahooRegularUniverseProvider.name:
        outside = set(str(row["symbol"]).upper() for row in screener["rows"]) - set(symbols)
        for row in screener["rows"]:
            if str(row["symbol"]).upper() not in outside:
                continue
            if float(row["pctchange"]) > floor:
                hidden.append({"symbol": row["symbol"], "regular_change_pct": float(row["pctchange"])})
    if hidden:
        summary = ", ".join(f"{item['symbol']} {item['regular_change_pct']:.3f}%" for item in hidden)
        raise MarketDataError(f"moving gainers source omitted higher regular-session candidates: {summary}; "
                              f"verified Top20 floor {floor:.3f}%")
    return audit


def build_canonical_market_data_packet(
    target_session_date: date,
    *,
    historical_providers: Iterable[HistoricalProvider] | None = None,
    issuer_components: dict[str, Any] | None = None,
    screener_provider: NasdaqScreenerProvider | None = None,
    discrepancy_arbitrator: DiscrepancyArbitrator | None = None,
    candidate_limit: int = 50,
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
    arbitrator = discrepancy_arbitrator or LiveDiscrepancyArbitrator()
    if screener_provider is not None:
        screener = screener_provider.fetch()
    else:
        screener = NasdaqYahooRegularUniverseProvider(
            corporate_action_notices=getattr(arbitrator, "corporate_action_notices", {}),
            notice_transport=getattr(arbitrator, "notice_transport", default_transport),
            action_provider=getattr(arbitrator, "action_provider", None),
            split_history_provider=getattr(arbitrator, "split_history_provider", None),
        ).fetch(
            target_session_date,
            expected_previous_session_date=date.fromisoformat(snapshot["indexes"][0]["previous_session_date"]),
        )
    symbols = screener_candidate_symbols(screener, limit=candidate_limit)
    expected_names = {str(row.get("symbol", "")).upper(): str(row.get("name", ""))
                      for row in screener.get("rows", [])}
    for provider in providers:
        if isinstance(provider, YahooChartProvider):
            provider.expected_names = expected_names
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
        discrepancy_arbitrator=arbitrator,
        excluded_candidates=excluded_candidates,
        excluded_action_candidates=excluded_action_candidates,
        expected_previous_session_date=date.fromisoformat(snapshot["indexes"][0]["previous_session_date"]),
    )
    candidate_floor_audit = audit_unranked_movers_against_regular_closes(
        screener, historical.series, top20, target_session_date,
        excluded_action_candidates, excluded_candidates, symbols,
    )
    provider_names = {
        snapshot["source"]["name"], screener["provider"], historical.provider,
    }
    raw_hashes = {screener["raw_response_sha256"]}
    for batch in screener.get("universe_evidence", {}).get("spark_batches", []):
        raw_hashes.add(batch["raw_response_sha256"])
    for item in screener.get("universe_evidence", {}).get("missing_quote_audit", []):
        for key in ("official_raw_response_sha256", "chart_raw_response_sha256",
                    "official_no_trade_raw_response_sha256"):
            if key in item:
                raw_hashes.add(item[key])
    for item in screener.get("universe_evidence", {}).get("top_candidate_official_audit", []):
        for key in ("official_raw_response_sha256", "chart_raw_response_sha256",
                    "official_previous_raw_response_sha256"):
            if key in item:
                hashes = item[key] if isinstance(item[key], list) else [item[key]]
                raw_hashes.update(value for value in hashes if isinstance(value, str))
    for item in (*snapshot["indexes"], *snapshot["sectors"], *top20,
                 *excluded_action_candidates, *candidate_floor_audit):
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
        mapping = item.get("symbol_mapping_evidence")
        if isinstance(mapping, dict) and isinstance(mapping.get("raw_response_sha256"), str):
            raw_hashes.add(mapping["raw_response_sha256"])
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
        "candidate_floor_audit": candidate_floor_audit,
        **({"universe_evidence": screener["universe_evidence"]}
           if "universe_evidence" in screener else {}),
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
