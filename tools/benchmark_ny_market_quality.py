#!/usr/bin/env python3
"""Frozen 2026-09-02 NY quality-contract benchmark (never publishes)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.ny_market import validate_payload
from tests.ny_market_quality_fixture import TOP20, payload
from tools.write_ny_market_payload import validate_only


def run() -> dict:
    data = payload()
    validated = validate_payload(data)
    canonical = data["canonical_market_data"]
    top = data["top_gainers_20"]
    expected_top = [row[0] for row in TOP20]
    verified_subset = sum(item["search_status"] == "verified_catalyst" for item in top[:10])
    caps = sum(item["market_cap"] is not None for item in top)

    # Content-depth scores are deterministic field/coverage rubrics, not a model's
    # subjective score: required depth fields, primary sources, independent news
    # clusters, and cross-domain causal themes in the frozen regenerated markdown.
    earnings_fields = {
        "company_description", "revenue", "eps", "guidance", "key_kpis", "one_offs",
        "price_reaction", "why_stock_moved", "forward_implication", "source_url", "source_type",
    }
    earnings_rows = data["earnings"] + data["after_hours_earnings"]
    earnings_quality = round(100 * sum(len(earnings_fields.intersection(row)) / len(earnings_fields) for row in earnings_rows) / len(earnings_rows))
    news_source = sum(bool(item.get("source_url")) for item in data["major_news"])
    news_clusters = len({item["event_cluster"] for item in data["major_news"]})
    news_impact = sum(bool(item.get("market_impact")) for item in data["major_news"])
    news10 = round(news_source * 4 + news_clusters * 3 + news_impact * 3)
    themes = ("原油", "金利", "Fed", "AI", "半導体", "ネットワーク", "メモリ", "電力", "冷却", "消費", "信用", "コモディティ", "日本株")
    theme_hits = sum(theme in data["report_markdown"] for theme in themes)
    final_analysis = min(100, round(70 + 30 * theme_hits / len(themes)))

    with tempfile.TemporaryDirectory(prefix="ny_market_quality_") as tmp:
        path = Path(tmp) / "payload.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        dry_run = validate_only(path)

    metrics = {
        "indices": f"{len(canonical['indexes'])}/5",
        "sectors": f"{len(canonical['sectors'])}/11",
        "top20_composition": f"{sum(item['ticker'] in expected_top for item in top)}/20",
        "top20_rank_change": f"{sum(item['ticker'] == expected_top[i] and item['rank'] == i + 1 and item['change_pct'] == canonical['top_gainers_20'][i]['change_pct'] for i, item in enumerate(top))}/20",
        "catalyst_verified_subset": f"{verified_subset}/10",
        "market_cap_reasonableness": f"{caps}/20",
        "earnings_quality": earnings_quality,
        "news10": news10,
        "final_analysis": final_analysis,
    }
    passes = {
        "indices": metrics["indices"] == "5/5",
        "sectors": metrics["sectors"] == "11/11",
        "top20_composition": metrics["top20_composition"] == "20/20",
        "top20_rank_change": metrics["top20_rank_change"] == "20/20",
        "catalyst_verified_subset": verified_subset >= 9,
        "market_cap_reasonableness": caps >= 19,
        "earnings_quality": earnings_quality >= 88,
        "news10": news10 >= 85,
        "final_analysis": final_analysis >= 90,
    }
    return {
        "benchmark": "frozen_2026-09-02_quality_v2",
        "stable_key": validated.report["stable_key"],
        "metrics": metrics,
        "thresholds_pass": passes,
        "overall": "PASS" if all(passes.values()) else "FAIL",
        "dry_run_payload_validation": dry_run,
        "writes": {"inbox": 0, "sqlite": 0, "supabase": 0, "frontend": 0},
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
