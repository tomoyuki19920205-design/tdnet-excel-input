#!/usr/bin/env python3
"""Prepare the reviewed NY 2026-09-03 v3 payload without touching Production."""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.ny_market import validate_payload
from lib.ny_market_20260903_after_hours_v3 import MIGRATION_PAYLOAD
from lib.ny_market_display import apply_after_hours_only_contract
from lib.production_environment import bootstrap_production_write_environment
from tools.company_news_atomic import atomic_write_json


class HistoricalMigrationError(RuntimeError):
    """The historical payload is not safe to migrate."""


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _section_span(markdown: str, heading: str) -> tuple[int, int]:
    pattern = re.compile(rf"(?m)^(#{{1,6}})\s+{re.escape(heading)}\s*(?:\r?\n|$)")
    matches = list(pattern.finditer(markdown))
    if len(matches) != 1:
        raise HistoricalMigrationError(f"expected exactly one {heading} section")
    match = matches[0]
    level = len(match.group(1))
    end = len(markdown)
    for candidate in re.finditer(r"(?m)^(#{1,6})\s+.+?\s*(?:\r?\n|$)", markdown[match.end():]):
        if len(candidate.group(1)) <= level:
            end = match.end() + candidate.start()
            break
    return match.start(), end


def _without_after_hours(markdown: str) -> str:
    start, end = _section_span(markdown, "引け後・アフター決算の注目株")
    return f"{markdown[:start]}<AFTER_HOURS_SECTION>{markdown[end:]}"


def _append_source(
    payload: dict[str, Any], *, title: str, publisher: str, url: str, published_at: str
) -> None:
    sources = payload.setdefault("sources", [])
    if not isinstance(sources, list):
        raise HistoricalMigrationError("sources must be an array")
    if any(isinstance(source, dict) and source.get("url") == url for source in sources):
        return
    sources.append({
        "title": title,
        "publisher": publisher,
        "url": url,
        "published_at": published_at,
    })


def _add_migration_sources(payload: dict[str, Any], items: list[dict[str, Any]]) -> None:
    for item in items:
        company = str(item["display_company_name"])
        announced_by_url: dict[str, str] = {}
        for field in ("background_context", "same_day_developments"):
            for record in item.get(field, []):
                announced_by_url[str(record["source_url"])] = str(record["announced_at"])
        for source in item["fact_sources"]:
            _append_source(
                payload,
                title=str(source["label"]),
                publisher=company,
                url=str(source["url"]),
                published_at=announced_by_url.get(str(source["url"]), "2026-09-02"),
            )
        for source in item["market_context_sources"]:
            _append_source(
                payload,
                title=str(source["label"]),
                publisher="Market Context",
                url=str(source["url"]),
                published_at="2026-09-02T20:28:00+00:00",
            )
    for run in MIGRATION_PAYLOAD["after_hours_candidate_review"]["discovery_runs"]:
        _append_source(
            payload,
            title=f"After-hours discovery: {run['scope']}",
            publisher="Discovery",
            url=str(run["source_url"]),
            published_at="2026-09-02",
        )


def migrate_payload(
    payload: dict[str, Any], *, expected_input_sha256: str, applied_commit: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", applied_commit):
        raise HistoricalMigrationError("applied_commit must be a full lowercase Git SHA")
    actual_input_sha256 = _payload_sha256(payload)
    if actual_input_sha256 != expected_input_sha256:
        raise HistoricalMigrationError("input payload SHA-256 does not match the approved backup")
    checks = {
        "stable_key": MIGRATION_PAYLOAD["stable_key"],
        "market_session_date": MIGRATION_PAYLOAD["market_session_date"],
        "report_display_contract_version": MIGRATION_PAYLOAD["source_display_contract"],
    }
    for field, expected in checks.items():
        if payload.get(field) != expected:
            raise HistoricalMigrationError(f"unexpected source {field}: {payload.get(field)!r}")
    if payload.get("report_date_jst") != "2026-09-03":
        raise HistoricalMigrationError("migration only permits the 2026-09-03 report")

    identity_before = tuple(
        payload.get(field)
        for field in ("stable_key", "schema_version", "report_type", "report_date_jst", "headline")
    )
    markdown_before = payload.get("report_markdown")
    if not isinstance(markdown_before, str):
        raise HistoricalMigrationError("report_markdown must be a string")

    result = deepcopy(payload)
    items = deepcopy(MIGRATION_PAYLOAD["after_hours_research"])
    result["after_hours_research"] = deepcopy(items)
    result["after_hours_earnings"] = deepcopy(items)
    result["after_hours_candidate_review"] = deepcopy(
        MIGRATION_PAYLOAD["after_hours_candidate_review"]
    )
    result["after_hours_migration"] = {
        "schema_version": MIGRATION_PAYLOAD["schema_version"],
        "migration_id": MIGRATION_PAYLOAD["migration_id"],
        "migration_source": MIGRATION_PAYLOAD["migration_source"],
        "verified_at": MIGRATION_PAYLOAD["verified_at"],
        "applied_commit": applied_commit,
        "source_payload_sha256": actual_input_sha256,
        "source_display_contract": MIGRATION_PAYLOAD["source_display_contract"],
        "target_display_contract": MIGRATION_PAYLOAD["target_display_contract"],
        "target_research_contract": MIGRATION_PAYLOAD["target_research_contract"],
        "migrated_tickers": [item["ticker"] for item in items],
    }
    _add_migration_sources(result, items)
    result = apply_after_hours_only_contract(result)

    identity_after = tuple(
        result.get(field)
        for field in ("stable_key", "schema_version", "report_type", "report_date_jst", "headline")
    )
    if identity_before != identity_after:
        raise HistoricalMigrationError("report identity changed during migration")
    if _without_after_hours(markdown_before) != _without_after_hours(result["report_markdown"]):
        raise HistoricalMigrationError("a non-target Markdown section changed")
    if result.get("report_display_contract_version") != MIGRATION_PAYLOAD["target_display_contract"]:
        raise HistoricalMigrationError("target display contract was not applied")
    if result["after_hours_candidate_review"].get("contract_version") != MIGRATION_PAYLOAD["target_research_contract"]:
        raise HistoricalMigrationError("target research contract was not applied")

    validated = validate_payload(result).payload
    if validated["after_hours_earnings"] != validated["after_hours_research"]:
        raise HistoricalMigrationError("canonical and projection after-hours data diverged")
    return validated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--applied-commit", required=True)
    parser.add_argument("--production-root", required=True, type=Path)
    args = parser.parse_args()
    environment = bootstrap_production_write_environment(args.production_root)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HistoricalMigrationError("input payload must be an object")
    migrated = migrate_payload(
        payload,
        expected_input_sha256=args.expected_input_sha256,
        applied_commit=args.applied_commit,
    )
    atomic_write_json(args.output, migrated)
    print(json.dumps({
        "status": "prepared",
        "stable_key": migrated["stable_key"],
        "display_contract": migrated["report_display_contract_version"],
        "research_contract": migrated["after_hours_candidate_review"]["contract_version"],
        "after_hours_count": len(migrated["after_hours_research"]),
        "payload_sha256": _payload_sha256(migrated),
        "markdown_sha256": migrated["report_delivery"]["sha256"],
        "output": str(args.output.resolve()),
        "environment": environment.safe_metadata(),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
