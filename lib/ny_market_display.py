"""Deterministic display contract for NY market report Markdown."""
from __future__ import annotations

import re
from copy import deepcopy
from hashlib import sha256
from typing import Any


DISPLAY_CONTRACT_VERSION = "ny_market_display_v1"
LEGACY_MIGRATION_VERSION = "ny_market_legacy_projection_description_v1"
LEGACY_MIGRATION_MAX_REPORT_DATE = "2026-09-03"
IDEOGRAPHIC_SPACE = "\u3000"
INDEX_LABELS = {
    "SOX": "SOX",
    "S&P500": "S&P 500",
    "Dow": "Dow",
    "Nasdaq": "Nasdaq",
    "Russell 2000": "Russell 2000",
}
SECTOR_LABELS = {
    "XLB": "素材",
    "XLC": "コミュニケーション・サービス",
    "XLF": "金融",
    "XLV": "ヘルスケア",
    "XLE": "エネルギー",
    "XLP": "生活必需品",
    "XLU": "公益",
    "XLY": "一般消費財",
    "XLI": "資本財",
    "XLK": "情報技術",
    "XLRE": "不動産",
}
SECTOR_ENGLISH_LABELS = frozenset({
    "Materials", "Communication Services", "Financials", "Health Care", "Energy",
    "Consumer Staples", "Utilities", "Consumer Discretionary", "Industrials",
    "Information Technology", "Real Estate",
})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SECTIONS = (
    ("5指数", frozenset({"5指数"})),
    ("11業種別騰落", frozenset({"11業種別騰落", "11 Sector SPDR（騰落率降順）"})),
    ("話題の値上がり10社", frozenset({"話題の値上がり10社"})),
    ("純粋上昇率ランキング", frozenset({"純粋上昇率ランキング", "純粋上昇率Top20"})),
)


class NYMarketDisplayError(ValueError):
    pass


def format_change_pct(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NYMarketDisplayError("change_pct must be numeric")
    number = float(value)
    if round(number, 2) == 0:
        number = 0.0
    return f"{number:+.2f}%"


def _text(item: dict[str, Any], key: str, field: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NYMarketDisplayError(f"{field}.{key} must be a non-empty string")
    return value.strip()


def _source_status(item: dict[str, Any]) -> str:
    status = _text(item, "search_status", "ticker item")
    if status == "verified_catalyst":
        source_type = _text(item, "source_type", "ticker item")
        source_url = _text(item, "source_url", "ticker item")
        return f"確認済み（[{source_type}]({source_url})）"
    if status == "searched_not_found":
        return "当日材料を検索したが確認できず"
    raise NYMarketDisplayError(f"unsupported search_status: {status}")


def _render_notable_gainers(items: Any) -> str:
    field = "notable_gainers"
    exact = 10
    if not isinstance(items, list) or len(items) != exact:
        raise NYMarketDisplayError(f"{field} must contain exactly {exact} items")
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise NYMarketDisplayError(f"{field}[{index - 1}] must be an object")
        ticker = _text(item, "ticker", field)
        company = _text(item, "company_name", field)
        description = _text(item, "company_description", field)
        catalyst = _text(item, "catalyst", field)
        first = (
            f"{index}. {ticker}（{company}） {format_change_pct(item.get('change_pct'))}"
            f" — {description}"
        )
        second = f"上昇理由・材料：{catalyst}"
        third = f"材料確認結果：{_source_status(item)}。"
        blocks.append(f"{first}\n\n{second}\n\n{third}")
    return "\n\n".join(blocks)


def _render_top_gainers(items: Any) -> str:
    field = "top_gainers_20"
    if not isinstance(items, list) or len(items) != 20:
        raise NYMarketDisplayError(f"{field} must contain exactly 20 items")
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise NYMarketDisplayError(f"{field}[{index - 1}] must be an object")
        ticker = _text(item, "ticker", field)
        company = _text(item, "company_name", field)
        lines.append(
            f"{index}. {company}（{ticker}） {format_change_pct(item.get('change_pct'))}"
        )
    return "\n".join(lines)


def _legacy_notable_descriptions(markdown: Any) -> dict[str, str]:
    if not isinstance(markdown, str) or not markdown.strip():
        raise NYMarketDisplayError("legacy report_markdown must be a non-empty string")
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(2) == "話題の値上がり10社":
            matches.append((index, len(match.group(1))))
    if len(matches) != 1:
        raise NYMarketDisplayError(
            "legacy report_markdown must contain exactly one 話題の値上がり10社 section"
        )
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    item_re = re.compile(
        r"^(\d+)\.\s+\*\*(.+?)\s+\(([^()]+)\)\*\*\s+—\s+(.+?)\s+終値\s+"
    )
    descriptions: dict[str, str] = {}
    expected_rank = 1
    for line in lines[start + 1:end]:
        match = item_re.match(line.strip())
        if not match:
            continue
        rank = int(match.group(1))
        if rank != expected_rank:
            raise NYMarketDisplayError("legacy notable_gainers ranks must be contiguous")
        ticker = match.group(3).strip().upper()
        if ticker in descriptions:
            raise NYMarketDisplayError(f"duplicate legacy notable_gainers ticker: {ticker}")
        descriptions[ticker] = match.group(4).strip()
        expected_rank += 1
    if len(descriptions) != 10:
        raise NYMarketDisplayError(
            "legacy report_markdown must contain ten extractable notable-gainer descriptions"
        )
    return descriptions


def migrate_legacy_projection_descriptions(payload: dict[str, Any]) -> dict[str, Any]:
    """Move only verified legacy notable-gainer descriptions into research data."""
    if payload.get("report_display_contract_version") is not None:
        raise NYMarketDisplayError("legacy migration requires a pre-display-contract payload")
    result = deepcopy(payload)
    report_date = result.get("report_date_jst")
    if not isinstance(report_date, str) or report_date > LEGACY_MIGRATION_MAX_REPORT_DATE:
        raise NYMarketDisplayError(
            f"legacy migration is limited to reports through {LEGACY_MIGRATION_MAX_REPORT_DATE}"
        )
    original_markdown = result.get("report_markdown")
    if not isinstance(original_markdown, str):
        raise NYMarketDisplayError("legacy report_markdown must be a string")
    original_digest = sha256(original_markdown.encode("utf-8")).hexdigest()
    delivery = result.get("report_delivery")
    if (
        not isinstance(delivery, dict)
        or delivery.get("source_field") != "report_markdown"
        or delivery.get("sha256") != original_digest
    ):
        raise NYMarketDisplayError("legacy report_delivery does not match report_markdown")
    notable = result.get("notable_gainers")
    research = result.get("ticker_research")
    if not isinstance(notable, list) or len(notable) != 10:
        raise NYMarketDisplayError("legacy notable_gainers must contain exactly ten items")
    if not isinstance(research, list):
        raise NYMarketDisplayError("legacy ticker_research must be an array")
    markdown_descriptions = _legacy_notable_descriptions(result.get("report_markdown"))
    research_by_ticker: dict[str, dict[str, Any]] = {}
    for item in research:
        if not isinstance(item, dict):
            raise NYMarketDisplayError("legacy ticker_research items must be objects")
        ticker = _text(item, "ticker", "ticker_research").upper()
        if ticker in research_by_ticker:
            raise NYMarketDisplayError(f"duplicate legacy ticker_research ticker: {ticker}")
        research_by_ticker[ticker] = item

    migrated_tickers: list[str] = []
    source_tickers: list[str] = []
    for index, item in enumerate(notable):
        if not isinstance(item, dict):
            raise NYMarketDisplayError(f"notable_gainers[{index}] must be an object")
        ticker = _text(item, "ticker", "notable_gainers").upper()
        description = _text(item, "company_description", "notable_gainers")
        if markdown_descriptions.get(ticker) != description:
            raise NYMarketDisplayError(
                f"legacy notable_gainers description differs from report_markdown for {ticker}"
            )
        target = research_by_ticker.get(ticker)
        if target is None:
            raise NYMarketDisplayError(f"legacy ticker_research is missing {ticker}")
        existing = target.get("company_description")
        if existing is None:
            target["company_description"] = description
            migrated_tickers.append(ticker)
        elif not isinstance(existing, str) or existing.strip() != description:
            raise NYMarketDisplayError(
                f"legacy ticker_research company_description differs for {ticker}"
            )
        source_tickers.append(ticker)

    result["report_display_migration"] = {
        "migration_version": LEGACY_MIGRATION_VERSION,
        "source_contract_version": "legacy_pre_ny_market_display_v1",
        "source_field": "notable_gainers[].company_description",
        "target_field": "ticker_research[].company_description",
        "source_stable_key": result.get("stable_key"),
        "source_report_date_jst": report_date,
        "source_report_markdown_sha256": original_digest,
        "source_tickers": source_tickers,
        "migrated_tickers": migrated_tickers,
        "migrated_count": len(migrated_tickers),
    }
    return result


def render_display_sections(payload: dict[str, Any]) -> dict[str, str]:
    index_moves = payload.get("index_moves")
    if not isinstance(index_moves, dict) or tuple(index_moves) != tuple(INDEX_LABELS):
        raise NYMarketDisplayError("index_moves must contain the canonical five-index order")
    indexes = "\n".join(
        f"{INDEX_LABELS[symbol]}{IDEOGRAPHIC_SPACE}{format_change_pct(index_moves[symbol].get('change_pct'))}"
        for symbol in INDEX_LABELS
    )

    sectors = payload.get("sector_moves")
    if not isinstance(sectors, list) or len(sectors) != 11:
        raise NYMarketDisplayError("sector_moves must contain exactly eleven items")
    sector_lines: list[str] = []
    for item in sectors:
        if not isinstance(item, dict) or item.get("symbol") not in SECTOR_LABELS:
            raise NYMarketDisplayError("sector_moves contains an unsupported sector")
        sector_lines.append(
            f"{SECTOR_LABELS[item['symbol']]}{IDEOGRAPHIC_SPACE}{format_change_pct(item.get('change_pct'))}"
        )

    return {
        "5指数": indexes,
        "11業種別騰落": "\n".join(sector_lines),
        "話題の値上がり10社": _render_notable_gainers(payload.get("notable_gainers")),
        "純粋上昇率ランキング": _render_top_gainers(payload.get("top_gainers_20")),
    }


def render_report_markdown(payload: dict[str, Any]) -> str:
    markdown = payload.get("report_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise NYMarketDisplayError("report_markdown must be a non-empty string")
    sections = render_display_sections(payload)
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    replacements: list[tuple[int, int, str, str]] = []
    for canonical, aliases in _SECTIONS:
        matches: list[tuple[int, int]] = []
        for index, line in enumerate(lines):
            match = _HEADING_RE.match(line)
            if match and match.group(2) in aliases:
                matches.append((index, len(match.group(1))))
        if len(matches) != 1:
            raise NYMarketDisplayError(f"report_markdown must contain exactly one {canonical} section")
        start, level = matches[0]
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = _HEADING_RE.match(lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        replacements.append((start, end, canonical, sections[canonical]))

    for start, end, canonical, body in sorted(replacements, reverse=True):
        lines[start:end] = [f"## {canonical}", "", body, ""]
    return "\n".join(lines).strip()


def apply_display_contract(payload: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(payload)
    result["report_markdown"] = render_report_markdown(result)
    result["report_display_contract_version"] = DISPLAY_CONTRACT_VERSION
    digest = sha256(result["report_markdown"].encode("utf-8")).hexdigest()
    result["report_delivery"] = {"source_field": "report_markdown", "sha256": digest}
    return result


def validate_display_contract(payload: dict[str, Any]) -> None:
    if payload.get("report_display_contract_version") != DISPLAY_CONTRACT_VERSION:
        raise NYMarketDisplayError(
            f"report_display_contract_version must be {DISPLAY_CONTRACT_VERSION}"
        )
    expected = render_report_markdown(payload)
    if payload.get("report_markdown") != expected:
        raise NYMarketDisplayError(
            "report_markdown display sections differ from deterministic NY market rendering"
        )
    migration = payload.get("report_display_migration")
    if migration is not None:
        if not isinstance(migration, dict):
            raise NYMarketDisplayError("report_display_migration must be an object")
        expected_tickers = [
            _text(item, "ticker", "notable_gainers").upper()
            for item in payload.get("notable_gainers", [])
            if isinstance(item, dict)
        ]
        required_metadata = {
            "migration_version": LEGACY_MIGRATION_VERSION,
            "source_contract_version": "legacy_pre_ny_market_display_v1",
            "source_field": "notable_gainers[].company_description",
            "target_field": "ticker_research[].company_description",
            "source_stable_key": payload.get("stable_key"),
            "source_report_date_jst": payload.get("report_date_jst"),
            "source_tickers": expected_tickers,
        }
        for key, value in required_metadata.items():
            if migration.get(key) != value:
                raise NYMarketDisplayError(f"invalid report_display_migration.{key}")
        if str(migration.get("source_report_date_jst")) > LEGACY_MIGRATION_MAX_REPORT_DATE:
            raise NYMarketDisplayError("report_display_migration is outside the legacy date range")
        digest = migration.get("source_report_markdown_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise NYMarketDisplayError(
                "report_display_migration.source_report_markdown_sha256 must be SHA-256"
            )
        migrated = migration.get("migrated_tickers")
        if not isinstance(migrated, list) or any(ticker not in expected_tickers for ticker in migrated):
            raise NYMarketDisplayError("invalid report_display_migration.migrated_tickers")
        if migration.get("migrated_count") != len(migrated):
            raise NYMarketDisplayError("invalid report_display_migration.migrated_count")

    summary = payload.get("summary_bullets")
    if not isinstance(summary, list):
        raise NYMarketDisplayError("summary_bullets must be an array")
    summary_text = "\n".join(str(value) for value in summary)
    forbidden = tuple(SECTOR_LABELS) + tuple(SECTOR_ENGLISH_LABELS)
    if migration is None:
        for label in forbidden:
            if re.search(rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])", summary_text, re.IGNORECASE):
                raise NYMarketDisplayError(f"summary_bullets must use Japanese sector names, found {label}")
