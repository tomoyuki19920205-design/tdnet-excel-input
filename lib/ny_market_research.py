"""Deterministic research contract for NY market daily reports.

This module deliberately contains no publishing or database code.  It validates
the machine-derived market snapshot and the single per-ticker research cache
that every rendered report section must reuse.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit


QUALITY_CONTRACT_VERSION = "ny_market_quality_v2"
MARKET_DATA_CONTRACT_VERSION = "ny_market_data_v1"
INDEX_ORDER = ("SOX", "S&P500", "Dow", "Nasdaq", "Russell 2000")
SECTOR_ETFS = frozenset({"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"})
SEARCH_STATUSES = frozenset({"verified_catalyst", "searched_not_found"})
MARKET_CAP_METHODS = frozenset({
    "issuer_total_single_class", "issuer_total_dual_class", "issuer_total_ads",
    "vendor_market_cap", "unavailable",
})
SOURCE_TYPES = frozenset({
    "sec", "company_ir", "company_press_release", "exchange", "market_data",
    "reuters", "ap", "high_quality_media", "government", "central_bank",
})
NOT_FOUND_TEXT = "当日材料を検索したが確認できず"


class NYMarketResearchError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedResearch:
    indexes: dict[str, dict[str, Any]]
    sectors: dict[str, dict[str, Any]]
    top_gainers: list[dict[str, Any]]
    tickers: dict[str, dict[str, Any]]
    source_urls: frozenset[str]


def market_data_packet_sha256(packet: dict[str, Any]) -> str:
    """Hash the complete canonical packet with stable JSON serialization."""
    encoded = json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def attach_market_data_packet_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Project immutable packet identity into a payload before rendering/publish."""
    packet = payload["canonical_market_data"]
    payload["market_data_contract_version"] = packet["market_data_contract_version"]
    payload["market_data_generated_at"] = packet["market_data_generated_at"]
    payload["providers"] = list(packet["providers"])
    payload["raw_response_hashes"] = list(packet["raw_response_hashes"])
    payload["discrepancy_count"] = packet["discrepancy_count"]
    payload["market_data_packet_sha256"] = market_data_packet_sha256(packet)
    return payload


def verify_market_data_packet_projection(payload: dict[str, Any], packet: dict[str, Any]) -> None:
    """Fail closed unless the persisted acquisition packet is the exact payload source."""
    if payload.get("canonical_market_data") != packet:
        raise NYMarketResearchError("payload canonical_market_data differs from supplied market-data packet")
    expected_hash = market_data_packet_sha256(packet)
    if payload.get("market_data_packet_sha256") != expected_hash:
        raise NYMarketResearchError("payload market_data_packet_sha256 differs from supplied packet")
    for key in (
        "market_data_contract_version", "market_data_generated_at", "providers",
        "raw_response_hashes", "discrepancy_count",
    ):
        if payload.get(key) != packet.get(key):
            raise NYMarketResearchError(f"payload {key} differs from supplied market-data packet")


def build_catalyst_search_plan(ticker: str, company_name: str, target_date: date | str) -> dict[str, list[str]]:
    """Return a provider-agnostic two-pass plan for same-day catalyst research."""
    symbol = _string(ticker, "ticker").upper()
    company = _string(company_name, "company_name")
    day = target_date.isoformat() if isinstance(target_date, date) else _string(target_date, "target_date")
    return {
        "initial": [
            f'"{symbol}" "{company}" {day} news catalyst',
            f'"{company}" {day} press release',
        ],
        "second_pass": [
            f'site:sec.gov "{symbol}" "{company}" {day}',
            f'"{company}" investor relations {day} press release',
            f'"{symbol}" {day} patent contract financing earnings acquisition',
        ],
    }


def _number(value: Any, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NYMarketResearchError(f"{field} must be numeric")
    return float(value)


def _string(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NYMarketResearchError(f"{field} must be a non-empty string")
    return value.strip()


def _url(value: Any, field: str, *, nullable: bool = False) -> str | None:
    result = _string(value, field, nullable=nullable)
    if result is None:
        return None
    parsed = urlsplit(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NYMarketResearchError(f"{field} must be an absolute http/https URL")
    return result


def _timestamp(value: Any, field: str) -> str:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NYMarketResearchError(f"{field} must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise NYMarketResearchError(f"{field} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _formula_change(close: float, previous_close: float) -> float:
    if previous_close == 0:
        raise NYMarketResearchError("previous_close cannot be zero")
    return (close / previous_close - 1.0) * 100.0


def _validate_price_point(item: Any, field: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise NYMarketResearchError(f"{field} must be an object")
    close = _number(item.get("close"), f"{field}.close")
    previous_close = _number(item.get("previous_close"), f"{field}.previous_close")
    change_pct = _number(item.get("change_pct"), f"{field}.change_pct")
    calculated = _formula_change(close, previous_close)
    if abs(calculated - change_pct) > 0.015:
        raise NYMarketResearchError(
            f"{field}.change_pct must equal regular close / prior regular close - 1"
        )
    return {**item, "close": close, "previous_close": previous_close, "change_pct": change_pct}


def _validate_discrepancy(item: dict[str, Any], field: str) -> dict[str, Any]:
    status = item.get("discrepancy_status", "not_applicable")
    if status == "not_applicable":
        return {**item, "discrepancy_status": status}
    if status != "resolved":
        raise NYMarketResearchError(f"{field}.discrepancy_status must be resolved or not_applicable")
    required = (
        "discrepancy_reason", "compared_providers", "official_previous_close",
        "official_target_close", "supporting_sources", "resolved_at",
        "corporate_action_status", "liquidity_flag",
    )
    missing = [key for key in required if key not in item]
    if missing:
        raise NYMarketResearchError(f"{field} resolved discrepancy missing: {', '.join(missing)}")
    _string(item["discrepancy_reason"], f"{field}.discrepancy_reason")
    _number(item["official_previous_close"], f"{field}.official_previous_close")
    _number(item["official_target_close"], f"{field}.official_target_close")
    _timestamp(item["resolved_at"], f"{field}.resolved_at")
    compared = item["compared_providers"]
    if not isinstance(compared, list) or "nasdaq" not in compared:
        raise NYMarketResearchError(f"{field}.compared_providers must include nasdaq")
    independent = {str(value) for value in compared} - {"nasdaq", "yahoo"}
    if not independent:
        raise NYMarketResearchError(f"{field} resolved discrepancy needs an independent provider family")
    sources = item["supporting_sources"]
    if not isinstance(sources, list) or not sources:
        raise NYMarketResearchError(f"{field}.supporting_sources must be non-empty")
    roles = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise NYMarketResearchError(f"{field}.supporting_sources[{index}] must be an object")
        _string(source.get("provider"), f"{field}.supporting_sources[{index}].provider")
        roles.add(_string(source.get("role"), f"{field}.supporting_sources[{index}].role"))
        raw_hash = source.get("raw_response_sha256")
        hashes = raw_hash if isinstance(raw_hash, list) else [raw_hash]
        if not hashes or any(not isinstance(value, str) or len(value) != 64 for value in hashes):
            raise NYMarketResearchError(f"{field}.supporting_sources[{index}] needs raw SHA-256 provenance")
    if not {"official_market_source", "corporate_action_check", "independent_support"}.issubset(roles):
        raise NYMarketResearchError(f"{field}.supporting_sources lacks required evidence roles")
    return item


def issuer_total_market_cap(components: list[dict[str, Any]]) -> float:
    """Return issuer-total market cap from all economically relevant classes."""
    if not isinstance(components, list) or not components:
        raise NYMarketResearchError("share_class_components must be a non-empty array")
    total = 0.0
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise NYMarketResearchError(f"share_class_components[{index}] must be an object")
        _string(component.get("class"), f"share_class_components[{index}].class")
        price = _number(component.get("price"), f"share_class_components[{index}].price")
        shares = _number(component.get("shares_outstanding"), f"share_class_components[{index}].shares_outstanding")
        if price < 0 or shares < 0:
            raise NYMarketResearchError("share-class price and shares must be non-negative")
        total += price * shares
    return total


def _validate_ticker(item: Any, index: int) -> dict[str, Any]:
    field = f"ticker_research[{index}]"
    if not isinstance(item, dict):
        raise NYMarketResearchError(f"{field} must be an object")
    required = (
        "ticker", "company_name", "close", "change_pct", "market_cap", "market_cap_method",
        "catalyst", "catalyst_type", "source_url", "source_type", "search_status", "searched_at",
        "share_class_components",
        "search_attempt_count", "search_queries", "searched_sources",
    )
    missing = [key for key in required if key not in item]
    if missing:
        raise NYMarketResearchError(f"{field} missing fields: {', '.join(missing)}")
    ticker = _string(item["ticker"], f"{field}.ticker").upper()
    company_name = _string(item["company_name"], f"{field}.company_name")
    company_description = item.get("company_description")
    if company_description is not None:
        company_description = _string(company_description, f"{field}.company_description")
    close = _number(item["close"], f"{field}.close")
    change_pct = _number(item["change_pct"], f"{field}.change_pct")
    market_cap = _number(item["market_cap"], f"{field}.market_cap", nullable=True)
    method = item["market_cap_method"]
    if method not in MARKET_CAP_METHODS:
        raise NYMarketResearchError(f"{field}.market_cap_method is unsupported")
    components = item["share_class_components"]
    if not isinstance(components, list):
        raise NYMarketResearchError(f"{field}.share_class_components must be an array")
    if method == "unavailable":
        if market_cap is not None or components:
            raise NYMarketResearchError(f"{field} unavailable market cap must be null with no components")
    elif method == "vendor_market_cap":
        if market_cap is None or market_cap <= 0 or components:
            raise NYMarketResearchError(
                f"{field} vendor market cap must be positive with no share-class components"
            )
    else:
        calculated = issuer_total_market_cap(components)
        if market_cap is None or abs(calculated - market_cap) > max(1.0, calculated * 0.01):
            raise NYMarketResearchError(f"{field}.market_cap must equal issuer-total component value")
        if method == "issuer_total_dual_class" and len(components) < 2:
            raise NYMarketResearchError(f"{field} dual-class market cap requires at least two classes")

    status = item["search_status"]
    if status not in SEARCH_STATUSES:
        raise NYMarketResearchError(f"{field}.search_status is unsupported")
    catalyst = _string(item["catalyst"], f"{field}.catalyst")
    catalyst_type = _string(item["catalyst_type"], f"{field}.catalyst_type")
    attempt_count = item["search_attempt_count"]
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count < 1:
        raise NYMarketResearchError(f"{field}.search_attempt_count must be a positive integer")
    queries = item["search_queries"]
    if not isinstance(queries, list) or len(queries) != attempt_count:
        raise NYMarketResearchError(f"{field}.search_queries must match search_attempt_count")
    if any(not isinstance(query, str) or not query.strip() for query in queries):
        raise NYMarketResearchError(f"{field}.search_queries must contain non-empty strings")
    searched_sources = item["searched_sources"]
    if not isinstance(searched_sources, list) or not searched_sources:
        raise NYMarketResearchError(f"{field}.searched_sources must be a non-empty array")
    for source_index, searched_source in enumerate(searched_sources):
        _url(searched_source, f"{field}.searched_sources[{source_index}]")
    if status == "verified_catalyst":
        source_url = _url(item["source_url"], f"{field}.source_url")
        source_type = item["source_type"]
        if source_type not in SOURCE_TYPES:
            raise NYMarketResearchError(f"{field}.source_type is unsupported")
        if catalyst_type in {"none", "not_found"}:
            raise NYMarketResearchError(f"{field} verified catalyst needs a concrete catalyst_type")
    else:
        if NOT_FOUND_TEXT not in catalyst:
            raise NYMarketResearchError(f"{field}.catalyst must explicitly say the search found no same-day catalyst")
        if item["source_url"] is not None or item["source_type"] is not None:
            raise NYMarketResearchError(f"{field} searched_not_found must not claim a catalyst source")
        source_url = None
        source_type = None
        if catalyst_type != "not_found":
            raise NYMarketResearchError(f"{field}.catalyst_type must be not_found")
    searched_at = _timestamp(item["searched_at"], f"{field}.searched_at")
    result = {
        **item, "ticker": ticker, "company_name": company_name,
        "close": close, "change_pct": change_pct, "market_cap": market_cap, "source_url": source_url,
        "source_type": source_type, "searched_at": searched_at,
    }
    if company_description is not None:
        result["company_description"] = company_description
    return result


def validate_research_packet(payload: dict[str, Any]) -> ValidatedResearch:
    if payload.get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        raise NYMarketResearchError(f"quality_contract_version must be {QUALITY_CONTRACT_VERSION}")
    snapshot = payload.get("canonical_market_data")
    if not isinstance(snapshot, dict):
        raise NYMarketResearchError("canonical_market_data must be an object")
    if snapshot.get("price_basis") != "regular_close" or snapshot.get("adjusted") is not False:
        raise NYMarketResearchError("market data must use unadjusted regular_close")
    if snapshot.get("market_data_contract_version") != MARKET_DATA_CONTRACT_VERSION:
        raise NYMarketResearchError(f"canonical market data must use {MARKET_DATA_CONTRACT_VERSION}")
    generated_at = _timestamp(snapshot.get("market_data_generated_at"), "canonical_market_data.market_data_generated_at")
    providers = snapshot.get("providers")
    if not isinstance(providers, list) or not providers or any(not isinstance(value, str) or not value for value in providers):
        raise NYMarketResearchError("canonical_market_data.providers must be a non-empty string array")
    raw_hashes = snapshot.get("raw_response_hashes")
    if not isinstance(raw_hashes, list) or not raw_hashes:
        raise NYMarketResearchError("canonical_market_data.raw_response_hashes must be non-empty")
    if any(not isinstance(value, str) or len(value) != 64 for value in raw_hashes):
        raise NYMarketResearchError("canonical_market_data.raw_response_hashes must contain SHA-256 values")
    discrepancy_count = snapshot.get("discrepancy_count")
    if isinstance(discrepancy_count, bool) or not isinstance(discrepancy_count, int) or discrepancy_count < 0:
        raise NYMarketResearchError("canonical_market_data.discrepancy_count must be a non-negative integer")
    expected_hash = market_data_packet_sha256(snapshot)
    if payload.get("market_data_packet_sha256") != expected_hash:
        raise NYMarketResearchError("market_data_packet_sha256 is missing or differs from canonical packet")
    projections = {
        "market_data_contract_version": MARKET_DATA_CONTRACT_VERSION,
        "market_data_generated_at": generated_at,
        "providers": providers,
        "raw_response_hashes": raw_hashes,
        "discrepancy_count": discrepancy_count,
    }
    for key, expected in projections.items():
        if payload.get(key) != expected:
            raise NYMarketResearchError(f"payload {key} differs from canonical market packet")
    if snapshot.get("market_session_date") != payload.get("market_session_date"):
        raise NYMarketResearchError("canonical market session date mismatch")
    source = snapshot.get("source")
    if not isinstance(source, dict):
        raise NYMarketResearchError("canonical_market_data.source must be an object")
    market_source_url = _url(source.get("url"), "canonical_market_data.source.url")
    _string(source.get("name"), "canonical_market_data.source.name")
    _timestamp(source.get("retrieved_at"), "canonical_market_data.source.retrieved_at")

    raw_indexes = snapshot.get("indexes")
    if not isinstance(raw_indexes, list) or len(raw_indexes) != len(INDEX_ORDER):
        raise NYMarketResearchError("canonical indexes must contain exactly five entries")
    indexes: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_indexes):
        item = _validate_price_point(raw, f"canonical_market_data.indexes[{index}]")
        symbol = _string(item.get("symbol"), f"canonical_market_data.indexes[{index}].symbol")
        if symbol in indexes:
            raise NYMarketResearchError(f"duplicate canonical index {symbol}")
        indexes[symbol] = item
    if tuple(indexes) != INDEX_ORDER:
        raise NYMarketResearchError(f"canonical index order must be {INDEX_ORDER}")

    raw_sectors = snapshot.get("sectors")
    if not isinstance(raw_sectors, list) or len(raw_sectors) != 11:
        raise NYMarketResearchError("canonical sectors must contain exactly eleven entries")
    sectors: dict[str, dict[str, Any]] = {}
    previous_change: float | None = None
    for index, raw in enumerate(raw_sectors, start=1):
        item = _validate_price_point(raw, f"canonical_market_data.sectors[{index - 1}]")
        symbol = _string(item.get("symbol"), f"canonical_market_data.sectors[{index - 1}].symbol").upper()
        if item.get("rank") != index:
            raise NYMarketResearchError("canonical sector ranks must be contiguous")
        if previous_change is not None and item["change_pct"] > previous_change + 1e-9:
            raise NYMarketResearchError("canonical sectors must be sorted by change_pct descending")
        previous_change = item["change_pct"]
        sectors[symbol] = {**item, "symbol": symbol}
    if frozenset(sectors) != SECTOR_ETFS:
        raise NYMarketResearchError("canonical sectors must be the eleven Sector SPDR ETFs")

    raw_top = snapshot.get("top_gainers_20")
    if not isinstance(raw_top, list) or len(raw_top) != 20:
        raise NYMarketResearchError("canonical top_gainers_20 must contain exactly twenty entries")
    top: list[dict[str, Any]] = []
    prior_change = float("inf")
    for index, raw in enumerate(raw_top, start=1):
        if not isinstance(raw, dict) or raw.get("rank") != index:
            raise NYMarketResearchError("canonical Top20 ranks must be contiguous")
        ticker = _string(raw.get("ticker"), f"canonical top[{index}].ticker").upper()
        change = _number(raw.get("change_pct"), f"canonical top[{index}].change_pct")
        if change > prior_change + 1e-9:
            raise NYMarketResearchError("canonical Top20 must be sorted by change_pct descending")
        prior_change = change
        top.append(_validate_discrepancy(
            {**raw, "ticker": ticker, "change_pct": change},
            f"canonical top[{index}]",
        ))
    actual_discrepancies = sum(item["discrepancy_status"] != "not_applicable" for item in top)
    if discrepancy_count != actual_discrepancies:
        raise NYMarketResearchError("canonical_market_data.discrepancy_count does not match Top20")

    raw_tickers = payload.get("ticker_research")
    if not isinstance(raw_tickers, list):
        raise NYMarketResearchError("ticker_research must be an array")
    tickers: dict[str, dict[str, Any]] = {}
    source_urls = {market_source_url}
    for index, raw in enumerate(raw_tickers):
        item = _validate_ticker(raw, index)
        if item["ticker"] in tickers:
            raise NYMarketResearchError(f"duplicate ticker_research entry {item['ticker']}")
        tickers[item["ticker"]] = item
        if item["source_url"]:
            source_urls.add(item["source_url"])

    for index, canonical in enumerate(top, start=1):
        ticker = canonical["ticker"]
        if ticker not in tickers:
            raise NYMarketResearchError(f"canonical Top20 ticker {ticker} has no research cache entry")
        research = tickers[ticker]
        for key in ("company_name", "close", "change_pct", "market_cap", "market_cap_method"):
            if canonical.get(key) != research.get(key):
                raise NYMarketResearchError(f"canonical Top20 {ticker}.{key} differs from ticker research")
        if canonical.get("discrepancy_status") == "resolved":
            for key in (
                "discrepancy_status", "discrepancy_reason", "compared_providers",
                "official_previous_close", "official_target_close", "supporting_sources", "resolved_at",
                "corporate_action_status", "liquidity_flag", "screener_change_pct",
                "screener_last_sale", "screener_net_change", "screener_implied_previous_close",
                "historical_previous_close", "historical_target_close", "historical_change_pct",
            ):
                if canonical.get(key) != research.get(key):
                    raise NYMarketResearchError(
                        f"canonical Top20 {ticker}.{key} differs from ticker research provenance"
                    )

    return ValidatedResearch(
        indexes=indexes, sectors=sectors, top_gainers=top, tickers=tickers,
        source_urls=frozenset(source_urls),
    )
