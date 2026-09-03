from __future__ import annotations

from copy import deepcopy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lib.ny_market import NYMarketValidationError, validate_payload
from lib.ny_market_display import (
    DISPLAY_CONTRACT_VERSION,
    INDEX_LABELS,
    NYMarketDisplayError,
    SECTOR_LABELS,
    apply_display_contract,
    migrate_legacy_projection_descriptions,
    render_display_sections,
)
from tests.ny_market_quality_fixture import payload


ROOT = Path(__file__).resolve().parents[1]


def _section(markdown: str, heading: str) -> str:
    marker = f"## {heading}\n\n"
    body = markdown.split(marker, 1)[1]
    return body.split("\n## ", 1)[0].strip()


def _replace_section(markdown: str, heading: str, body: str) -> str:
    marker = f"## {heading}\n\n"
    prefix, remainder = markdown.split(marker, 1)
    _, suffix = remainder.split("\n## ", 1)
    return f"{prefix}{marker}{body}\n\n## {suffix}"


def _legacy_payload() -> dict:
    data = payload()
    legacy_lines = []
    for rank, item in enumerate(data["notable_gainers"], start=1):
        legacy_lines.append(
            f"{rank}. **{item['company_name']} ({item['ticker']})** — "
            f"{item['company_description']} 終値 ${item['close']:.4f}、"
            f"{item['change_pct']:+.3f}%、issuer-total時価総額 "
            f"${item['market_cap']}（{item['market_cap_method']}）。"
        )
    data["report_markdown"] = _replace_section(
        data["report_markdown"], "話題の値上がり10社", "\n".join(legacy_lines)
    )
    data["summary_bullets"][0] = "セクター首位はXLB Materials、次いでXLC。"
    data.pop("report_display_contract_version")
    for item in data["ticker_research"]:
        item.pop("company_description", None)
    data["report_delivery"]["sha256"] = __import__("hashlib").sha256(
        data["report_markdown"].encode("utf-8")
    ).hexdigest()
    return data


def test_five_indexes_render_name_and_signed_percentage_only():
    data = payload()
    body = _section(data["report_markdown"], "5指数")
    assert body.splitlines() == [
        "SOX　-2.14%",
        "S&P 500　-0.71%",
        "Dow　-0.79%",
        "Nasdaq　-1.03%",
        "Russell 2000　-1.23%",
    ]
    assert "|" not in body
    for item in data["index_moves"].values():
        assert str(item["close"]) not in body
        assert str(item["previous_close"]) not in body


def test_sample_index_and_sector_output_matches_requested_20260903_shape():
    data = payload()
    changes = [0.45, 0.46, 0.56, 0.45, 1.13]
    for item, change in zip(data["index_moves"].values(), changes):
        item["change_pct"] = change
    sector_symbols = ["XLB", "XLC", "XLF", "XLV", "XLE", "XLP", "XLU", "XLY", "XLI", "XLK", "XLRE"]
    sector_changes = [1.69, 1.39, 0.80, 0.75, 0.51, 0.33, 0.26, 0.24, 0.03, -0.02, -0.70]
    data["sector_moves"] = [
        {"symbol": symbol, "change_pct": change}
        for symbol, change in zip(sector_symbols, sector_changes)
    ]
    sections = render_display_sections(data)
    assert sections["5指数"] == "\n".join([
        "SOX　+0.45%", "S&P 500　+0.46%", "Dow　+0.56%",
        "Nasdaq　+0.45%", "Russell 2000　+1.13%",
    ])
    assert sections["11業種別騰落"] == "\n".join([
        "素材　+1.69%", "コミュニケーション・サービス　+1.39%", "金融　+0.80%",
        "ヘルスケア　+0.75%", "エネルギー　+0.51%", "生活必需品　+0.33%",
        "公益　+0.26%", "一般消費財　+0.24%", "資本財　+0.03%",
        "情報技術　-0.02%", "不動産　-0.70%",
    ])


def test_all_sectors_use_japanese_names_without_rank_etf_english_or_absolute_values():
    data = payload()
    body = _section(data["report_markdown"], "11業種別騰落")
    assert [line.split("　", 1)[0] for line in body.splitlines()] == [
        SECTOR_LABELS[item["symbol"]] for item in data["sector_moves"]
    ]
    assert len(body.splitlines()) == 11
    assert "|" not in body
    for item in data["sector_moves"]:
        assert item["symbol"] not in body
        assert item["sector"] not in body
        assert str(item["close"]) not in body
        assert str(item["previous_close"]) not in body


def test_notable_gainers_have_compact_first_paragraph_and_separate_evidence():
    data = payload()
    body = _section(data["report_markdown"], "話題の値上がり10社")
    assert len(re.findall(r"(?m)^\d+\. ", body)) == 10
    first_item = data["notable_gainers"][0]
    first_block = body.split("\n\n2. ", 1)[0]
    paragraphs = first_block.split("\n\n")
    assert paragraphs[0] == (
        f"1. {first_item['ticker']}（{first_item['company_name']}） +77.42%"
        f" — {first_item['company_description']}"
    )
    assert "上昇理由・材料：" in paragraphs[1]
    assert "材料確認結果：" in paragraphs[2]
    assert first_item["catalyst"] not in paragraphs[0]
    for item in data["notable_gainers"]:
        assert str(item["close"]) not in body
        assert str(item["market_cap"]) not in body
        assert item["market_cap_method"] not in body


def test_top20_contains_only_rank_company_ticker_and_signed_change():
    data = payload()
    body = _section(data["report_markdown"], "純粋上昇率ランキング")
    assert len(re.findall(r"(?m)^\d+\. ", body)) == 20
    for rank, item in enumerate(data["top_gainers_20"], start=1):
        assert body.splitlines()[rank - 1] == (
            f"{rank}. {item['company_name']}（{item['ticker']}） "
            f"{item['change_pct']:+.2f}%"
        )
        assert item["company_description"] not in body
        assert item["catalyst"] not in body
        assert str(item["close"]) not in body
        assert str(item["market_cap"]) not in body
        assert item["market_cap_method"] not in body


def test_validator_rejects_manual_display_drift_and_non_japanese_sector_summary():
    data = payload()
    validate_payload(data)
    changed = deepcopy(data)
    changed["report_markdown"] = changed["report_markdown"].replace("SOX　-2.14%", "SOX | 11288.61 | -2.14%")
    changed["report_delivery"]["sha256"] = __import__("hashlib").sha256(
        changed["report_markdown"].encode("utf-8")
    ).hexdigest()
    with pytest.raises(NYMarketValidationError, match="deterministic"):
        validate_payload(changed)

    summary_changed = deepcopy(data)
    summary_changed["summary_bullets"][0] = "XLB Materialsが上昇"
    with pytest.raises(NYMarketValidationError, match="Japanese sector names"):
        validate_payload(summary_changed)


def test_notable_gainer_missing_or_noncanonical_company_description_fails_closed():
    missing = payload()
    del missing["ticker_research"][0]["company_description"]
    with pytest.raises(NYMarketValidationError, match="company_description"):
        validate_payload(missing)

    invented = payload()
    invented["notable_gainers"][0]["company_description"] = "根拠のない会社説明。"
    invented = apply_display_contract(invented)
    with pytest.raises(NYMarketValidationError, match="company_description differs from canonical research"):
        validate_payload(invented)


def test_top20_only_company_descriptions_are_optional():
    data = payload()
    top_only = {item["ticker"] for item in data["top_gainers_20"][10:]}
    for item in data["ticker_research"]:
        if item["ticker"] in top_only:
            item.pop("company_description", None)
    for item in data["top_gainers_20"][10:]:
        item.pop("company_description", None)
    data = apply_display_contract(data)
    validate_payload(data)


def test_legacy_projection_descriptions_migrate_ten_and_enable_safe_render():
    legacy = _legacy_payload()
    migrated = migrate_legacy_projection_descriptions(legacy)
    metadata = migrated["report_display_migration"]
    assert metadata["migrated_count"] == 10
    assert metadata["migrated_tickers"] == [
        item["ticker"] for item in legacy["notable_gainers"]
    ]
    assert all(
        item.get("company_description")
        for item in migrated["ticker_research"]
        if item["ticker"] in metadata["migrated_tickers"]
    )
    assert all(
        "company_description" not in item
        for item in migrated["ticker_research"]
        if item["ticker"] in {row["ticker"] for row in legacy["top_gainers_20"][10:]}
    )
    rendered = apply_display_contract(migrated)
    validate_payload(rendered)


def test_legacy_projection_description_mismatch_or_absence_fails_closed():
    mismatched = _legacy_payload()
    description = mismatched["notable_gainers"][0]["company_description"]
    mismatched["report_markdown"] = mismatched["report_markdown"].replace(
        description, "一致しない説明。", 1
    )
    mismatched["report_delivery"]["sha256"] = __import__("hashlib").sha256(
        mismatched["report_markdown"].encode("utf-8")
    ).hexdigest()
    with pytest.raises(NYMarketDisplayError, match="differs from report_markdown"):
        migrate_legacy_projection_descriptions(mismatched)

    missing = _legacy_payload()
    del missing["notable_gainers"][0]["company_description"]
    with pytest.raises(NYMarketDisplayError, match="company_description"):
        migrate_legacy_projection_descriptions(missing)


def test_renderer_changes_only_ny_display_fields_and_preserves_other_sections():
    draft = payload()
    draft["report_markdown"] = draft["report_markdown"].replace(
        "## 話題の値下がり10社\n\n下落銘柄の説明。",
        "## 話題の値下がり10社\n\nNY以外も含め、この対象外本文は不変。",
    )
    draft.pop("report_display_contract_version")
    rendered = apply_display_contract(draft)
    assert rendered["report_display_contract_version"] == DISPLAY_CONTRACT_VERSION
    assert "NY以外も含め、この対象外本文は不変。" in rendered["report_markdown"]
    for key, value in draft.items():
        if key not in {"report_markdown", "report_delivery"}:
            assert rendered[key] == value


def test_positive_negative_and_zero_signs_are_stable():
    data = payload()
    values = [1.0, -0.7, 0.0, 0.004, -0.004]
    for item, value in zip(data["index_moves"].values(), values):
        item["change_pct"] = value
    assert render_display_sections(data)["5指数"].splitlines() == [
        "SOX　+1.00%", "S&P 500　-0.70%", "Dow　+0.00%",
        "Nasdaq　+0.00%", "Russell 2000　+0.00%",
    ]


def test_canonical_index_labels_are_complete():
    assert tuple(INDEX_LABELS) == ("SOX", "S&P500", "Dow", "Nasdaq", "Russell 2000")


def test_renderer_cli_replaces_draft_sections_and_produces_valid_payload(tmp_path: Path):
    draft = payload()
    draft["report_markdown"] = draft["report_markdown"].replace(
        "SOX　-2.14%", "| SOX | 11288.61 | 11535.00 | -2.14% |",
    )
    draft.pop("report_display_contract_version")
    source = tmp_path / "draft.json"
    output = tmp_path / "rendered.json"
    source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "render_ny_market_report.py"), str(source), "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(output.read_text(encoding="utf-8"))
    assert "11288.61" not in _section(rendered["report_markdown"], "5指数")
    validate_payload(rendered)
