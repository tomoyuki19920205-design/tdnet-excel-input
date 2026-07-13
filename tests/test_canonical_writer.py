"""tests/test_canonical_writer.py — canonical_writer のユニットテスト"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.pipeline.canonical_writer import (
    _make_financials_row_key,
    _make_segments_row_key,
    has_segment_display_aliases,
    normalize_segment_display_key,
    normalize_segment_key,
    write_financials_canonical,
    write_segments_canonical,
    expand_financials_rows,
    expand_segments_rows,
)


# ================================================================
# source_row_key テスト
# ================================================================


class TestFinancialsRowKey:
    def test_deterministic(self):
        k1 = _make_financials_row_key("6750", "2026-03-31", "FY", "sales", "tdnet", "F001")
        k2 = _make_financials_row_key("6750", "2026-03-31", "FY", "sales", "tdnet", "F001")
        assert k1 == k2

    def test_different_metric(self):
        k1 = _make_financials_row_key("6750", "2026-03-31", "FY", "sales", "tdnet", "F001")
        k2 = _make_financials_row_key("6750", "2026-03-31", "FY", "operating_profit", "tdnet", "F001")
        assert k1 != k2

    def test_none_filing_id(self):
        k = _make_financials_row_key("6750", "2026-03-31", "FY", "sales", "jquants", None)
        assert "||" not in k  # None → ""
        assert k.endswith("|")

    def test_format(self):
        k = _make_financials_row_key("6750", "2026-03-31", "FY", "sales", "tdnet", "F001")
        assert k == "cf|6750|2026-03-31|FY|sales|tdnet|F001"


class TestSegmentsRowKey:
    def test_deterministic(self):
        k1 = _make_segments_row_key("6750", "2026-03-31", "FY", "自動車事業", "sales", "xbrl", "F001")
        k2 = _make_segments_row_key("6750", "2026-03-31", "FY", "自動車事業", "sales", "xbrl", "F001")
        assert k1 == k2

    def test_format(self):
        k = _make_segments_row_key("6750", "2026-03-31", "FY", "自動車事業", "sales", "xbrl", None)
        assert k == "cs|6750|2026-03-31|FY|自動車事業|sales|xbrl|"


# ================================================================
# segment_key 正規化テスト
# ================================================================


class TestNormalizeSegmentKey:
    def test_nfkc(self):
        # 全角 → 半角
        assert normalize_segment_key("ＡＢＣ") == "abc"

    def test_trim(self):
        assert normalize_segment_key("  foo  ") == "foo"

    def test_collapse_whitespace(self):
        assert normalize_segment_key("a  b\t c") == "a b c"

    def test_lower(self):
        assert normalize_segment_key("AbCdE") == "abcde"

    def test_japanese(self):
        assert normalize_segment_key("  自動車 事業  ") == "自動車 事業"


class TestNormalizeSegmentDisplayKey:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("CloudCommercePlatform", "cloud commerce platform"),
            ("Cloud Commerce Platform", "cloud commerce platform"),
            ("クラウドコマースプラットフォーム事業", "cloud commerce platform"),
            ("EcBusinessGrowth", "ec business growth"),
            ("Ec Business Growth", "ec business growth"),
            ("EC Business Growth", "ec business growth"),
            ("ＥＣビジネス成長支援事業", "ec business growth"),
            ("ECビジネス成長支援事業", "ec business growth"),
            ("Datautillization", "datautillization"),
            ("datautillization", "datautillization"),
            ("データ利活用プラットフォーム事業", "datautillization"),
        ],
    )
    def test_4057_taxonomy_aliases_share_display_keys(self, name, expected):
        assert normalize_segment_display_key("4057", name) == expected

    def test_4057_aliases_are_ticker_scoped(self):
        assert normalize_segment_display_key("9999", "CloudCommercePlatform") == "cloudcommerceplatform"
        assert normalize_segment_display_key("9999", "ＥＣビジネス成長支援事業") == "ecビジネス成長支援事業"
        assert has_segment_display_aliases("4057") is True
        assert has_segment_display_aliases("9999") is False

    def test_4057_expand_preserves_source_names(self):
        rows, skipped = expand_segments_rows(
            ticker="4057",
            period="2026-05-31",
            quarter="FY",
            segments=[
                {"segment_name": "Cloud Commerce Platform", "sales": 1, "profit": 2},
                {"segment_name": "ＥＣビジネス成長支援事業", "sales": 3, "profit": 4},
                {"segment_name": "Datautillization", "sales": 5, "profit": 6},
            ],
            source="backfill_xbrl",
        )
        assert skipped == 0
        assert {row["segment_name"] for row in rows} == {
            "Cloud Commerce Platform", "ECビジネス成長支援事業", "Datautillization",
        }
        assert {row["segment_key"] for row in rows} == {
            "cloud commerce platform", "ec business growth", "datautillization",
        }

    def test_8908_aliases_share_two_display_keys(self):
        assert normalize_segment_display_key("8908", "Real Estate Solution") == "real estate solution"
        assert normalize_segment_display_key("8908", "不動産ソリューション事業") == "real estate solution"
        assert normalize_segment_display_key("8908", "School Life Support") == "school life support"
        assert normalize_segment_display_key("8908", "School Life Solution") == "school life support"
        assert normalize_segment_display_key("8908", "学生生活ソリューション事業") == "school life support"

    def test_non_8908_keeps_existing_key(self):
        assert normalize_segment_display_key("9999", "School Life Solution") == "school life solution"

    def test_expand_preserves_source_segment_name(self):
        rows, skipped = expand_segments_rows(
            ticker="8908",
            period="2026-05-31",
            quarter="FY",
            segments=[{"segment_name": "School Life Solution", "sales": 4653, "profit": 393}],
            source="backfill_xbrl",
        )
        assert skipped == 0
        assert {row["segment_name"] for row in rows} == {"School Life Solution"}
        assert {row["segment_key"] for row in rows} == {"school life support"}

    @pytest.mark.parametrize("quarter", ["1Q", "3Q"])
    def test_8908_non_alias_display_name_is_unchanged_in_each_quarter(self, quarter):
        rows, skipped = expand_segments_rows(
            ticker="8908",
            period="2026-05-31",
            quarter=quarter,
            segments=[{"segment_name": "Existing Segment", "sales": 1, "profit": 2}],
            source="backfill_xbrl",
        )
        assert skipped == 0
        assert {row["segment_name"] for row in rows} == {"Existing Segment"}
        assert {row["segment_key"] for row in rows} == {"existing segment"}


# ================================================================
# expand_financials_rows テスト
# ================================================================


class TestExpandFinancialsRows:
    """expand_financials_rows のテスト (HTTP なし)。"""

    def test_expansion(self):
        """3 metrics → 3 rows"""
        rows, skipped = expand_financials_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100, "gross_profit": 50, "operating_profit": 30},
            source="tdnet",
        )
        assert len(rows) == 3
        assert skipped == 0
        metrics = {r["metric"] for r in rows}
        assert metrics == {"sales", "gross_profit", "operating_profit"}

    def test_none_skipped(self):
        rows, skipped = expand_financials_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100, "gross_profit": None},
            source="tdnet",
        )
        assert len(rows) == 1
        assert skipped == 1

    def test_all_none_empty(self):
        rows, skipped = expand_financials_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": None, "gross_profit": None},
            source="tdnet",
        )
        assert len(rows) == 0
        assert skipped == 2

    def test_ticker_normalized(self):
        rows, skipped = expand_financials_rows(
            ticker="72030", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100},
            source="jquants",
        )
        assert len(rows) == 1
        assert rows[0]["ticker"] == "7203"

    def test_invalid_ticker_empty(self):
        rows, skipped = expand_financials_rows(
            ticker="", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100},
            source="tdnet",
        )
        assert len(rows) == 0
        assert skipped == 1

    def test_source_priority_auto(self):
        rows, _ = expand_financials_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100},
            source="jquants",
        )
        assert rows[0]["source_priority"] == 2

    def test_recency_key_present(self):
        rows, _ = expand_financials_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100},
            source="tdnet",
        )
        assert rows[0]["recency_key"]
        assert len(rows[0]["recency_key"]) > 0

    def test_source_row_key_uses_normalized_ticker(self):
        rows, _ = expand_financials_rows(
            ticker="72030", period="2026-03-31", quarter="FY",
            metrics_dict={"sales": 100},
            source="jquants",
        )
        assert "|7203|" in rows[0]["source_row_key"]


# ================================================================
# expand_segments_rows テスト
# ================================================================


class TestExpandSegmentsRows:
    """expand_segments_rows のテスト (HTTP なし)。"""

    def test_expansion(self):
        """2 segments × 2 metrics = 4 rows"""
        rows, skipped = expand_segments_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            segments=[
                {"segment_name": "自動車事業", "sales": 100, "profit": 50},
                {"segment_name": "金融事業", "sales": 200, "profit": 80},
            ],
            source="xbrl",
        )
        assert len(rows) == 4
        assert skipped == 0

    def test_empty_name_skipped(self):
        rows, skipped = expand_segments_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            segments=[
                {"segment_name": "", "sales": 100, "profit": 50},
                {"segment_name": "有効", "sales": 200, "profit": 80},
            ],
            source="xbrl",
        )
        assert skipped >= 1
        assert len(rows) == 2

    def test_ticker_normalized(self):
        rows, _ = expand_segments_rows(
            ticker="72030", period="2026-03-31", quarter="FY",
            segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
            source="xbrl",
        )
        assert all(r["ticker"] == "7203" for r in rows)

    def test_dedupe(self):
        """同一 source_row_key が重複する場合、後勝ちで dedupe される"""
        rows, _ = expand_segments_rows(
            ticker="6750", period="2026-03-31", quarter="FY",
            segments=[
                {"segment_name": "テスト", "sales": 100, "profit": 50},
                {"segment_name": "テスト", "sales": 200, "profit": 80},
            ],
            source="xbrl",
        )
        # 後勝ちで 2 rows (sales + profit)
        assert len(rows) == 2
        sales_row = [r for r in rows if r["metric"] == "sales"][0]
        assert sales_row["value"] == 200


# ================================================================
# write_financials_canonical テスト (後方互換)
# ================================================================


class TestWriteFinancialsCanonical:
    def test_wide_to_long_expansion(self):
        """wide → long: 3 metrics → 3 rows (None skipped)"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 3, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100, "gross_profit": 50, "operating_profit": 30},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 3
        assert result["skipped"] == 0
        assert result["errors"] == 0

        # upsert が呼ばれること
        assert mock_upsert.called
        call_args = mock_upsert.call_args
        rows = call_args[0][1]
        assert len(rows) == 3
        metrics = {r["metric"] for r in rows}
        assert metrics == {"sales", "gross_profit", "operating_profit"}

    def test_none_values_skipped(self):
        """None metric は skipped"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100, "gross_profit": None, "operating_profit": None},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 1
        assert result["skipped"] == 2

    def test_all_none_no_upsert(self):
        """全 None → upsert 呼ばれない"""
        mock_upsert = MagicMock()
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": None, "gross_profit": None},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert mock_upsert.call_count == 0


    def test_source_priority_auto_set(self):
        """source_priority が自動設定される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="jquants",
                config={"url": "x", "key": "y"},
            )
        rows = mock_upsert.call_args[0][1]
        assert rows[0]["source_priority"] == 2  # SOURCE_PRIORITY["jquants"] = 2

    def test_recency_key_auto_generated(self):
        """recency_key が自動生成される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        rows = mock_upsert.call_args[0][1]
        assert rows[0]["recency_key"] is not None
        assert len(rows[0]["recency_key"]) > 0

    def test_upsert_failure_best_effort(self):
        """upsert 失敗 → errors > 0, written == 0"""
        mock_upsert = MagicMock(return_value={"ok": False, "count": 0, "error": "RLS violation"})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["errors"] == 1


# ================================================================
# write_segments_canonical テスト (後方互換)
# ================================================================


class TestWriteSegmentsCanonical:
    def test_segment_to_long(self):
        """segment list → long rows: 2 segments × 2 metrics = 4 rows"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 4, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                segments=[
                    {"segment_name": "自動車事業", "sales": 100, "profit": 50},
                    {"segment_name": "金融事業", "sales": 200, "profit": 80},
                ],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 4
        assert result["errors"] == 0

    def test_segment_key_normalized(self):
        """segment_key が正規化される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 2, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_segments_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                segments=[{"segment_name": " 自動車 事業 ", "sales": 100, "profit": 50}],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        rows = mock_upsert.call_args[0][1]
        assert all(r["segment_key"] == "自動車 事業" for r in rows)

    def test_empty_segment_name_skipped(self):
        """空 segment_name はスキップ"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 2, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="6750",
                period="2026-03-31",
                quarter="FY",
                segments=[
                    {"segment_name": "", "sales": 100, "profit": 50},
                    {"segment_name": "有効", "sales": 200, "profit": 80},
                ],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["skipped"] >= 1
        rows = mock_upsert.call_args[0][1]
        assert len(rows) == 2  # 有効セグメントの sales+profit


class TestWriteSegmentsRowKeyDeterministic:
    """source_row_key が同一入力で常に同じ値を返すこと。"""

    def test_same_input_same_key(self):
        calls: list[list[dict]] = []
        mock_upsert = MagicMock(side_effect=lambda *a, **kw: (calls.append(a[1]), {"ok": True, "count": 2, "error": None})[1])
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            for _ in range(2):
                write_segments_canonical(
                    ticker="6750",
                    period="2026-03-31",
                    quarter="FY",
                    segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
                    source="xbrl",
                    config={"url": "x", "key": "y"},
                )
        keys1 = {r["source_row_key"] for r in calls[0]}
        keys2 = {r["source_row_key"] for r in calls[1]}
        assert keys1 == keys2


# ================================================================
# ticker 正規化ガード テスト
# ================================================================


class TestTickerNormalizationGuard:
    """canonical_writer の最終防衛線としての ticker 正規化テスト。"""

    def test_5digit_to_4digit(self):
        """72030 → 7203 に正規化されて upsert される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="72030",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="jquants",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 1
        rows = mock_upsert.call_args[0][1]
        assert rows[0]["ticker"] == "7203"

    def test_alpha_5digit_to_4digit(self):
        """130A0 → 130A に正規化される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="130A0",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="jquants",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 1
        rows = mock_upsert.call_args[0][1]
        assert rows[0]["ticker"] == "130A"

    def test_4digit_passthrough(self):
        """7203 はそのまま通過"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="7203",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 1
        rows = mock_upsert.call_args[0][1]
        assert rows[0]["ticker"] == "7203"

    def test_invalid_ticker_skipped(self):
        """空文字 ticker は invalid → skipped"""
        mock_upsert = MagicMock()
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_financials_canonical(
                ticker="",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="tdnet",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert result["skipped"] == 1
        assert mock_upsert.call_count == 0

    def test_source_row_key_uses_normalized_ticker(self):
        """source_row_key に正規化後の 4桁 ticker が使われる"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 1, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            write_financials_canonical(
                ticker="72030",
                period="2026-03-31",
                quarter="FY",
                metrics_dict={"sales": 100},
                source="jquants",
                config={"url": "x", "key": "y"},
            )
        rows = mock_upsert.call_args[0][1]
        key = rows[0]["source_row_key"]
        assert "|7203|" in key
        assert "|72030|" not in key

    def test_segments_5digit_normalized(self):
        """segments writer でも 72030 → 7203 に正規化される"""
        mock_upsert = MagicMock(return_value={"ok": True, "count": 2, "error": None})
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="72030",
                period="2026-03-31",
                quarter="FY",
                segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 2
        rows = mock_upsert.call_args[0][1]
        assert all(r["ticker"] == "7203" for r in rows)

    def test_segments_invalid_ticker_skipped(self):
        """segments writer でも invalid ticker はスキップ"""
        mock_upsert = MagicMock()
        with patch("lib.pipeline.canonical_writer.supabase_upsert", mock_upsert):
            result = write_segments_canonical(
                ticker="",
                period="2026-03-31",
                quarter="FY",
                segments=[{"segment_name": "テスト", "sales": 100, "profit": 50}],
                source="xbrl",
                config={"url": "x", "key": "y"},
            )
        assert result["written"] == 0
        assert mock_upsert.call_count == 0
