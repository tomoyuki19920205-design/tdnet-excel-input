"""Validation and canonical SQLite storage for daily NY market reports."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lib.ny_market_research import (
    INDEX_ORDER,
    NOT_FOUND_TEXT,
    QUALITY_CONTRACT_VERSION,
    SOURCE_TYPES,
    NYMarketResearchError,
    ValidatedResearch,
    validate_research_packet,
)
from lib.ny_market_display import (
    AFTER_HOURS_CONTRACT_VERSION,
    NYMarketDisplayError,
    validate_display_contract,
)


SCHEMA_VERSION = "ny_market_daily_v1"
REPORT_TYPE = "ny_market_daily"
MARKET_STATUSES = frozenset({"open", "holiday_or_weekend"})
JSON_FIELDS = (
    "summary_bullets", "index_moves", "sector_moves", "notable_gainers",
    "notable_losers", "top_gainers_20", "earnings", "after_hours_earnings",
    "major_news", "commodities", "sources",
)
PLATFORM_CITATION_RE = re.compile(r"(?:cite|turn\d+(?:search|view|fetch)\d+|\u3010\d+\u2020[^\u3011]+\u3011)", re.IGNORECASE)


class NYMarketValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedNYMarketReport:
    report: dict[str, Any]
    run: dict[str, Any]
    payload: dict[str, Any]


def _text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise NYMarketValidationError(f"{field} must be a string")
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise NYMarketValidationError(f"{field} length must be {minimum}..{maximum}")
    return result


def _date(value: Any, field: str) -> str:
    text = _text(value, field, 10, 10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise NYMarketValidationError(f"{field} must be YYYY-MM-DD") from exc


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NYMarketValidationError(f"{field} must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise NYMarketValidationError(f"{field} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field, 2048)
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise NYMarketValidationError(f"{field} must be an absolute http/https URL")
    return url


def _list(value: Any, field: str, exact: int | None = None, maximum: int = 200) -> list[Any]:
    if not isinstance(value, list):
        raise NYMarketValidationError(f"{field} must be an array")
    if exact is not None and len(value) != exact:
        raise NYMarketValidationError(f"{field} must contain exactly {exact} items")
    if len(value) > maximum:
        raise NYMarketValidationError(f"{field} must contain at most {maximum} items")
    return value


def stable_report_id(stable_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-viewer:{stable_key}"))


def report_markdown_sha256(report_markdown: str) -> str:
    return hashlib.sha256(report_markdown.encode("utf-8")).hexdigest()


def _same_number(left: Any, right: Any, field: str, tolerance: float = 0.015) -> None:
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        raise NYMarketValidationError(f"{field} must be numeric")
    if isinstance(right, bool) or not isinstance(right, (int, float)):
        raise NYMarketValidationError(f"canonical value for {field} must be numeric")
    if abs(float(left) - float(right)) > tolerance:
        raise NYMarketValidationError(f"{field} differs from canonical research")


def _validate_projection(
    item: Any,
    field: str,
    research: ValidatedResearch,
    *,
    require_company_description: bool = False,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise NYMarketValidationError(f"{field} must be an object")
    ticker = _text(item.get("ticker"), f"{field}.ticker", 32).upper()
    canonical = research.tickers.get(ticker)
    if canonical is None:
        raise NYMarketValidationError(f"{field} ticker {ticker} has no canonical research entry")
    for key in (
        "company_name", "close", "change_pct", "market_cap", "market_cap_method",
        "catalyst", "catalyst_type", "source_url", "source_type", "search_status",
        "searched_at", "share_class_components", "search_attempt_count", "search_queries",
        "searched_sources",
    ):
        if key not in item:
            raise NYMarketValidationError(f"{field}.{key} is required")
        if item[key] != canonical[key]:
            raise NYMarketValidationError(f"{field}.{key} differs from canonical research")
    if require_company_description:
        key = "company_description"
        if key not in item:
            raise NYMarketValidationError(f"{field}.{key} is required")
        if key not in canonical:
            raise NYMarketValidationError(f"canonical research for {ticker} requires {key}")
        if item[key] != canonical[key]:
            raise NYMarketValidationError(f"{field}.{key} differs from canonical research")
    if item.get("search_status") == "searched_not_found" and NOT_FOUND_TEXT not in str(item.get("catalyst")):
        raise NYMarketValidationError(f"{field} must use the explicit searched-not-found wording")
    return item


def _validate_index_moves(value: Any, research: ValidatedResearch) -> dict[str, Any]:
    if not isinstance(value, dict) or tuple(value) != INDEX_ORDER:
        raise NYMarketValidationError(f"index_moves must contain the canonical order {INDEX_ORDER}")
    for symbol, item in value.items():
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"index_moves.{symbol} must be an object")
        canonical = research.indexes[symbol]
        for key in ("close", "previous_close", "change_pct"):
            _same_number(item.get(key), canonical[key], f"index_moves.{symbol}.{key}")
        if _http_url(item.get("source_url"), f"index_moves.{symbol}.source_url") not in research.source_urls:
            raise NYMarketValidationError(f"index_moves.{symbol}.source_url is not canonical")
    return value


def _validate_sector_moves(value: Any, research: ValidatedResearch) -> list[Any]:
    items = _list(value, "sector_moves", exact=11)
    if [item.get("symbol") if isinstance(item, dict) else None for item in items] != list(research.sectors):
        raise NYMarketValidationError("sector_moves must preserve canonical rank order")
    for index, item in enumerate(items):
        canonical = research.sectors[item["symbol"]]
        if item.get("rank") != index + 1:
            raise NYMarketValidationError("sector_moves ranks must be contiguous")
        for key in ("close", "previous_close", "change_pct"):
            _same_number(item.get(key), canonical[key], f"sector_moves[{index}].{key}")
        if _http_url(item.get("source_url"), f"sector_moves[{index}].source_url") not in research.source_urls:
            raise NYMarketValidationError(f"sector_moves[{index}].source_url is not canonical")
    return items


def _validate_earnings(items: list[Any], field: str, source_urls: set[str], research: ValidatedResearch) -> None:
    required = (
        "ticker", "company_name", "company_description", "revenue", "eps", "guidance", "key_kpis",
        "one_offs", "price_reaction", "why_stock_moved", "forward_implication", "source_url", "source_type",
    )
    if field == "after_hours_earnings":
        required += ("after_hours_change_pct", "as_of_utc", "as_of_jst", "session")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"{field}[{index}] must be an object")
        for key in required:
            if key not in item:
                raise NYMarketValidationError(f"{field}[{index}].{key} is required")
        if field == "after_hours_earnings":
            _text(item.get("ticker"), f"{field}[{index}].ticker", 32)
            _text(item.get("company_name"), f"{field}[{index}].company_name", 500)
        else:
            _validate_projection(item, f"{field}[{index}]", research)
        if item["source_type"] not in {"company_ir", "sec"}:
            raise NYMarketValidationError(f"{field}[{index}] must use company IR or SEC as primary source")
        source_urls.add(_http_url(item["source_url"], f"{field}[{index}].source_url"))
        for key in ("ticker", "company_name", "company_description", "why_stock_moved", "forward_implication"):
            _text(item[key], f"{field}[{index}].{key}", 5000)
        for key in ("revenue", "eps", "guidance", "price_reaction"):
            value = item[key]
            if value is None or value == "" or value == {} or value == []:
                raise NYMarketValidationError(f"{field}[{index}].{key} must contain researched detail")
        for key in ("key_kpis", "one_offs"):
            if not isinstance(item[key], list):
                raise NYMarketValidationError(f"{field}[{index}].{key} must be an array")
        if field == "after_hours_earnings":
            if item["session"] != "post_market":
                raise NYMarketValidationError(f"{field}[{index}].session must be post_market")
            _same_number(item["after_hours_change_pct"], item["after_hours_change_pct"], f"{field}[{index}].after_hours_change_pct", 0)
            utc_text = _timestamp(item["as_of_utc"], f"{field}[{index}].as_of_utc")
            jst_text = _timestamp(item["as_of_jst"], f"{field}[{index}].as_of_jst")
            if datetime.fromisoformat(utc_text).astimezone(timezone.utc) != datetime.fromisoformat(jst_text).astimezone(timezone.utc):
                raise NYMarketValidationError(f"{field}[{index}] UTC/JST as-of timestamps identify different instants")


def _validate_after_hours_contract(
    payload: dict[str, Any],
    after_hours: list[Any],
    source_urls: set[str],
) -> None:
    canonical = _list(payload.get("after_hours_research"), "after_hours_research", maximum=10)
    if after_hours != canonical:
        raise NYMarketValidationError(
            "after_hours_earnings differs from canonical after_hours_research"
        )
    review = payload.get("after_hours_candidate_review")
    if not isinstance(review, dict):
        raise NYMarketValidationError("after_hours_candidate_review must be an object")
    if review.get("contract_version") != AFTER_HOURS_CONTRACT_VERSION:
        raise NYMarketValidationError(
            f"after_hours_candidate_review.contract_version must be {AFTER_HOURS_CONTRACT_VERSION}"
        )
    if review.get("discovery_method") != "broad_discovery_then_primary_verification":
        raise NYMarketValidationError(
            "after_hours_candidate_review.discovery_method must use two-stage discovery"
        )
    review_session_date = _date(
        review.get("market_session_date"),
        "after_hours_candidate_review.market_session_date",
    )
    if review_session_date != payload.get("market_session_date"):
        raise NYMarketValidationError(
            "after_hours_candidate_review.market_session_date differs from report"
        )
    discovery_started = datetime.fromisoformat(_timestamp(
        review.get("discovery_started_at"),
        "after_hours_candidate_review.discovery_started_at",
    ))
    discovery_completed = datetime.fromisoformat(_timestamp(
        review.get("discovery_completed_at"),
        "after_hours_candidate_review.discovery_completed_at",
    ))
    if discovery_started > discovery_completed:
        raise NYMarketValidationError(
            "after_hours_candidate_review discovery timestamps are reversed"
        )
    expected_scopes = (
        "earnings_calendar", "after_hours_movers", "regulatory_filings", "material_events",
    )
    discovery_runs = _list(
        review.get("discovery_runs"),
        "after_hours_candidate_review.discovery_runs",
        exact=len(expected_scopes),
    )
    actual_scopes: list[str] = []
    for index, run in enumerate(discovery_runs):
        field = f"after_hours_candidate_review.discovery_runs[{index}]"
        if not isinstance(run, dict):
            raise NYMarketValidationError(f"{field} must be an object")
        scope = _text(run.get("scope"), f"{field}.scope", 64)
        actual_scopes.append(scope)
        _text(run.get("query"), f"{field}.query", 1000)
        if run.get("status") != "completed":
            raise NYMarketValidationError(f"{field}.status must be completed")
        if run.get("source_kind") not in {"secondary_discovery", "official_registry"}:
            raise NYMarketValidationError(
                f"{field}.source_kind must distinguish discovery from primary verification"
            )
        source_urls.add(_http_url(run.get("source_url"), f"{field}.source_url"))
    if tuple(actual_scopes) != expected_scopes:
        raise NYMarketValidationError(
            "after_hours_candidate_review.discovery_runs must cover every broad discovery scope"
        )
    coverage = review.get("coverage_status")
    if coverage not in {"normal", "quiet_day"}:
        raise NYMarketValidationError(
            "after_hours_candidate_review.coverage_status must be normal or quiet_day"
        )
    expected_coverage = "normal" if len(after_hours) >= 5 else "quiet_day"
    if coverage != expected_coverage:
        raise NYMarketValidationError(
            "after_hours_candidate_review.coverage_status must be derived from selected count"
        )
    candidates = _list(review.get("candidates"), "after_hours_candidate_review.candidates")
    if review.get("discovered_candidate_count") != len(candidates):
        raise NYMarketValidationError(
            "after_hours_candidate_review.discovered_candidate_count differs from candidates"
        )
    if len(candidates) < len(after_hours):
        raise NYMarketValidationError("candidate review cannot be smaller than selected coverage")
    included: list[str] = []
    included_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        field = f"after_hours_candidate_review.candidates[{index}]"
        if not isinstance(candidate, dict):
            raise NYMarketValidationError(f"{field} must be an object")
        if candidate.get("importance_rank") != index + 1:
            raise NYMarketValidationError(f"{field}.importance_rank must be contiguous")
        ticker = _text(candidate.get("ticker"), f"{field}.ticker", 32).upper()
        if ticker in seen:
            raise NYMarketValidationError(f"duplicate after-hours candidate ticker: {ticker}")
        seen.add(ticker)
        discovered_by = _list(candidate.get("discovered_by"), f"{field}.discovered_by")
        if not discovered_by or any(scope not in expected_scopes for scope in discovered_by):
            raise NYMarketValidationError(f"{field}.discovered_by must name completed scopes")
        if candidate.get("discovery_source_kind") not in {
            "secondary_discovery", "official_registry",
        }:
            raise NYMarketValidationError(f"{field}.discovery_source_kind is unsupported")
        discovery_source_url = _http_url(
            candidate.get("discovery_source_url"), f"{field}.discovery_source_url"
        )
        source_urls.add(discovery_source_url)
        _text(candidate.get("reason"), f"{field}.reason", 1000)
        status = candidate.get("status")
        if status == "included":
            if candidate.get("reason_code") not in {
                "verified_earnings", "verified_material_event",
            }:
                raise NYMarketValidationError(f"{field}.reason_code is unsupported")
            primary_source_url = _http_url(
                candidate.get("primary_source_url"), f"{field}.primary_source_url"
            )
            price_source_url = _http_url(
                candidate.get("price_source_url"), f"{field}.price_source_url"
            )
            if primary_source_url == discovery_source_url:
                raise NYMarketValidationError(
                    f"{field} must separate discovery evidence from primary verification"
                )
            source_urls.update((primary_source_url, price_source_url))
            included.append(ticker)
            included_records.append(candidate)
        elif status == "excluded":
            if candidate.get("reason_code") not in {
                "no_primary_source", "no_trusted_after_hours_price", "not_material",
                "duplicate_event", "outside_session",
            }:
                raise NYMarketValidationError(f"{field}.reason_code is unsupported")
        else:
            raise NYMarketValidationError(f"{field}.status must be included or excluded")
    selected = [_text(item.get("ticker"), "after_hours_earnings[].ticker", 32).upper()
                for item in after_hours if isinstance(item, dict)]
    if included != selected:
        raise NYMarketValidationError(
            "included after-hours candidates must exactly match selected canonical order"
        )
    for index, (item, candidate) in enumerate(zip(after_hours, included_records)):
        if candidate["primary_source_url"] != item.get("source_url"):
            raise NYMarketValidationError(
                f"after_hours_candidate_review primary source differs for {selected[index]}"
            )
        if candidate["price_source_url"] != item.get("after_hours_price_source_url"):
            raise NYMarketValidationError(
                f"after_hours_candidate_review price source differs for {selected[index]}"
            )
    required = (
        "display_company_name", "results_summary", "investment_takeaway",
        "after_hours_price_source_url", "after_hours_price_provider", "display_numbers",
    )
    forbidden_display_tokens = (
        "session=post_market", "searched_at", "search_status", "provider=", "as_of_utc",
        "as_of_jst", "通常取引終値",
    )
    for index, item in enumerate(canonical):
        field = f"after_hours_research[{index}]"
        for key in required:
            if key not in item:
                raise NYMarketValidationError(f"{field}.{key} is required")
        for key in required[:-1]:
            _text(item[key], f"{field}.{key}", 5000)
        numbers = item["display_numbers"]
        if not isinstance(numbers, list) or not numbers:
            raise NYMarketValidationError(f"{field}.display_numbers must be a non-empty array")
        results = item["results_summary"]
        for number in numbers:
            token = _text(number, f"{field}.display_numbers[]", 100)
            if token not in results:
                raise NYMarketValidationError(
                    f"{field}.results_summary is missing canonical display number {token}"
                )
        source_urls.add(_http_url(
            item["after_hours_price_source_url"],
            f"{field}.after_hours_price_source_url",
        ))
        observed_utc = datetime.fromisoformat(_timestamp(
            item.get("as_of_utc"), f"{field}.as_of_utc"
        )).astimezone(timezone.utc)
        if observed_utc.date().isoformat() != review_session_date:
            raise NYMarketValidationError(
                f"{field}.as_of_utc must identify the reviewed NY market session date"
            )
        visible = "\n".join((
            item["display_company_name"], item["company_description"], results,
            item["investment_takeaway"],
        ))
        if any(token in visible for token in forbidden_display_tokens):
            raise NYMarketValidationError(f"{field} exposes internal or regular-session data")


def _validate_news(items: list[Any], source_urls: set[str]) -> None:
    cluster_counts: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"major_news[{index}] must be an object")
        for key in ("title", "summary", "market_impact", "event_cluster", "source_url", "source_type", "covered_elsewhere", "market_wide_exception"):
            if key not in item:
                raise NYMarketValidationError(f"major_news[{index}].{key} is required")
        cluster = _text(item["event_cluster"], f"major_news[{index}].event_cluster", 200)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        if cluster_counts[cluster] > 2:
            raise NYMarketValidationError(f"major_news cluster {cluster} exceeds two items")
        if item["covered_elsewhere"] is True and item["market_wide_exception"] is not True:
            raise NYMarketValidationError(f"major_news[{index}] duplicates another section without a market-wide exception")
        if not isinstance(item["covered_elsewhere"], bool) or not isinstance(item["market_wide_exception"], bool):
            raise NYMarketValidationError(f"major_news[{index}] duplicate flags must be boolean")
        if item["source_type"] not in SOURCE_TYPES:
            raise NYMarketValidationError(f"major_news[{index}].source_type is unsupported")
        source_urls.add(_http_url(item["source_url"], f"major_news[{index}].source_url"))


def _validate_commodities(items: list[Any], source_urls: set[str]) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"commodities[{index}] must be an object")
        for key in ("name", "price", "change_pct", "reason", "source_url", "source_type"):
            if key not in item:
                raise NYMarketValidationError(f"commodities[{index}].{key} is required")
        _same_number(item["price"], item["price"], f"commodities[{index}].price", 0)
        _same_number(item["change_pct"], item["change_pct"], f"commodities[{index}].change_pct", 0)
        _text(item["reason"], f"commodities[{index}].reason", 2000)
        if item["source_type"] not in SOURCE_TYPES:
            raise NYMarketValidationError(f"commodities[{index}].source_type is unsupported")
        source_urls.add(_http_url(item["source_url"], f"commodities[{index}].source_url"))


def validate_payload(payload: Any) -> ValidatedNYMarketReport:
    if not isinstance(payload, dict):
        raise NYMarketValidationError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("report_type") != REPORT_TYPE:
        raise NYMarketValidationError("unsupported NY market report schema")

    report_date = _date(payload.get("report_date_jst"), "report_date_jst")
    session_date = _date(payload.get("market_session_date"), "market_session_date")
    if session_date > report_date:
        raise NYMarketValidationError("market_session_date cannot follow report_date_jst")
    stable_key = _text(payload.get("stable_key"), "stable_key", 64)
    expected_key = f"ny_market_daily:{report_date}"
    if stable_key != expected_key:
        raise NYMarketValidationError(f"stable_key must be {expected_key}")
    generated_at = _timestamp(payload.get("generated_at"), "generated_at")
    market_status = payload.get("market_status")
    if market_status not in MARKET_STATUSES:
        raise NYMarketValidationError("market_status must be open or holiday_or_weekend")

    try:
        research = validate_research_packet(payload)
    except NYMarketResearchError as exc:
        raise NYMarketValidationError(str(exc)) from exc

    headline = _text(payload.get("headline"), "headline", 500)
    bullets = _list(payload.get("summary_bullets"), "summary_bullets")
    if not 5 <= len(bullets) <= 8:
        raise NYMarketValidationError("summary_bullets must contain 5..8 items")
    bullets = [_text(item, "summary_bullets[]", 500) for item in bullets]

    index_moves = _validate_index_moves(payload.get("index_moves"), research)
    sector_moves = _validate_sector_moves(payload.get("sector_moves"), research)
    notable_gainers = _list(payload.get("notable_gainers"), "notable_gainers", exact=10)
    notable_losers = _list(payload.get("notable_losers"), "notable_losers", exact=10)
    for index, item in enumerate(notable_gainers):
        _validate_projection(
            item,
            f"notable_gainers[{index}]",
            research,
            require_company_description=True,
        )
    for index, item in enumerate(notable_losers):
        _validate_projection(item, f"notable_losers[{index}]", research)
    top_gainers = _list(payload.get("top_gainers_20"), "top_gainers_20", exact=20)
    for index, item in enumerate(top_gainers):
        _validate_projection(item, f"top_gainers_20[{index}]", research)
        canonical = research.top_gainers[index]
        if item["ticker"].upper() != canonical["ticker"] or item.get("rank") != index + 1:
            raise NYMarketValidationError("top_gainers_20 must preserve canonical order and ranks")

    earnings = _list(payload.get("earnings"), "earnings", maximum=100)
    after_hours = _list(payload.get("after_hours_earnings"), "after_hours_earnings", maximum=100)
    major_news = _list(payload.get("major_news"), "major_news", exact=10)
    commodities = _list(payload.get("commodities"), "commodities", maximum=100)
    report_markdown = _text(payload.get("report_markdown"), "report_markdown", 300_000, 500)
    try:
        validate_display_contract(payload)
    except NYMarketDisplayError as exc:
        raise NYMarketValidationError(str(exc)) from exc
    section_source_urls = set(research.source_urls)
    _validate_earnings(earnings, "earnings", section_source_urls, research)
    _validate_earnings(after_hours, "after_hours_earnings", section_source_urls, research)
    _validate_after_hours_contract(payload, after_hours, section_source_urls)
    _validate_news(major_news, section_source_urls)
    _validate_commodities(commodities, section_source_urls)
    final_refs = _list(payload.get("final_analysis_references"), "final_analysis_references", maximum=100)
    if not final_refs:
        raise NYMarketValidationError("final_analysis_references must contain at least one ticker research projection")
    for index, item in enumerate(final_refs):
        _validate_projection(item, f"final_analysis_references[{index}]", research)

    delivery = payload.get("report_delivery")
    if not isinstance(delivery, dict) or delivery.get("source_field") != "report_markdown":
        raise NYMarketValidationError("report_delivery.source_field must be report_markdown")
    expected_sha = report_markdown_sha256(report_markdown)
    if delivery.get("sha256") != expected_sha:
        raise NYMarketValidationError("report_delivery.sha256 does not match report_markdown")

    raw_sources = _list(payload.get("sources"), "sources", maximum=300)
    if not raw_sources:
        raise NYMarketValidationError("sources must contain at least one item")
    sources: list[dict[str, Any]] = []
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise NYMarketValidationError(f"sources[{index}] must be an object")
        published_at = item.get("published_at")
        if published_at is not None:
            if isinstance(published_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
                published_at = _date(published_at, f"sources[{index}].published_at")
            else:
                published_at = _timestamp(published_at, f"sources[{index}].published_at")
        sources.append({
            "title": _text(item.get("title"), f"sources[{index}].title", 500),
            "publisher": _text(item.get("publisher"), f"sources[{index}].publisher", 200),
            "url": _http_url(item.get("url"), f"sources[{index}].url"),
            "published_at": published_at,
        })
    source_catalog_urls = {item["url"] for item in sources}
    missing_sources = section_source_urls - source_catalog_urls
    if missing_sources:
        raise NYMarketValidationError(f"sources catalog is missing {len(missing_sources)} section source URL(s)")

    clean_payload = {
        **payload,
        "quality_contract_version": QUALITY_CONTRACT_VERSION,
        "sources": sources,
        "report_markdown": report_markdown,
        "report_delivery": {"source_field": "report_markdown", "sha256": expected_sha},
    }
    if PLATFORM_CITATION_RE.search(json.dumps(clean_payload, ensure_ascii=False)):
        raise NYMarketValidationError("Company Viewer payload contains a platform-specific citation token")

    report: dict[str, Any] = {
        "id": stable_report_id(stable_key),
        "stable_key": stable_key,
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "report_date_jst": report_date,
        "market_session_date": session_date,
        "market_status": market_status,
        "generated_at": generated_at,
        "headline": headline,
        "summary_bullets": json.dumps(bullets, ensure_ascii=False),
        "index_moves": json.dumps(index_moves, ensure_ascii=False),
        "sector_moves": json.dumps(sector_moves, ensure_ascii=False),
        "notable_gainers": json.dumps(notable_gainers, ensure_ascii=False),
        "notable_losers": json.dumps(notable_losers, ensure_ascii=False),
        "top_gainers_20": json.dumps(top_gainers, ensure_ascii=False),
        "earnings": json.dumps(earnings, ensure_ascii=False),
        "after_hours_earnings": json.dumps(after_hours, ensure_ascii=False),
        "major_news": json.dumps(major_news, ensure_ascii=False),
        "commodities": json.dumps(commodities, ensure_ascii=False),
        "report_markdown": report_markdown,
        "sources": json.dumps(sources, ensure_ascii=False),
    }
    run = {
        "run_id": stable_report_id(f"run:{stable_key}"),
        "stable_key": stable_key,
        "report_date_jst": report_date,
    }
    return ValidatedNYMarketReport(report=report, run=run, payload=clean_payload)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_ny_market_reports (
 id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL, report_type TEXT NOT NULL,
 report_date_jst TEXT NOT NULL, market_session_date TEXT NOT NULL, market_status TEXT NOT NULL,
 generated_at TEXT NOT NULL, headline TEXT NOT NULL, summary_bullets TEXT NOT NULL, index_moves TEXT NOT NULL,
 sector_moves TEXT NOT NULL, notable_gainers TEXT NOT NULL, notable_losers TEXT NOT NULL,
 top_gainers_20 TEXT NOT NULL, earnings TEXT NOT NULL, after_hours_earnings TEXT NOT NULL,
 major_news TEXT NOT NULL, commodities TEXT NOT NULL, report_markdown TEXT NOT NULL, sources TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_ny_market_report_runs (
 run_id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, report_date_jst TEXT NOT NULL, status TEXT NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT, error_type TEXT, error_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ny_market_reports_generated ON canonical_ny_market_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_ny_market_runs_status ON canonical_ny_market_report_runs(status, report_date_jst DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SQLITE_SCHEMA)
    return conn


def mark_run(
    conn: sqlite3.Connection,
    run: dict[str, Any],
    status: str,
    *,
    error: Exception | None = None,
    increment: bool = False,
) -> None:
    if status not in {"pending", "running", "success", "failed", "retry_pending"}:
        raise ValueError("invalid NY market run status")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO canonical_ny_market_report_runs "
            "(run_id,stable_key,report_date_jst,status,attempt,started_at,completed_at,error_type,error_message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stable_key) DO UPDATE SET "
            "status=excluded.status,attempt=canonical_ny_market_report_runs.attempt+?,"
            "started_at=COALESCE(excluded.started_at,canonical_ny_market_report_runs.started_at),"
            "completed_at=excluded.completed_at,error_type=excluded.error_type,error_message=excluded.error_message,updated_at=excluded.updated_at",
            (
                run["run_id"], run["stable_key"], run["report_date_jst"], status, 1 if increment else 0,
                now if status == "running" else None, now if status in {"success", "failed"} else None,
                type(error).__name__ if error else None, str(error)[:2000] if error else None, now, now,
                1 if increment else 0,
            ),
        )


def upsert_report(conn: sqlite3.Connection, validated: ValidatedNYMarketReport) -> None:
    now = _now()
    report = validated.report
    columns = list(report) + ["created_at", "updated_at"]
    updates = ",".join(
        f"{name}=excluded.{name}" for name in report if name not in {"id", "stable_key"}
    ) + ",updated_at=excluded.updated_at"
    with conn:
        conn.execute(
            f"INSERT INTO canonical_ny_market_reports ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(stable_key) DO UPDATE SET {updates} "
            "WHERE excluded.generated_at >= canonical_ny_market_reports.generated_at",
            [report[name] for name in report] + [now, now],
        )


def rows_for_sync(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"canonical_ny_market_reports", "canonical_ny_market_report_runs"}:
        raise ValueError("unsupported NY market table")
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    if table == "canonical_ny_market_reports":
        for row in rows:
            for field in JSON_FIELDS:
                row[field] = json.loads(row[field])
    return rows
