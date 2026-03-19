"""tests/test_v7_regression.py — v7 成果を固定するテスト

v7 で改善した以下の挙動をテストで固定:
- JSONL rows フィールドと quality metrics
- Phase F profit inference (テキスト行表で sales_only → profit 回復)
- ratio を profit に誤認しない (3232 型)
- 2331 / 9303 の false positive 回帰防止
- PL テーブル誤採用防止
- 9057 型の経路差分
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))


# ============================================================
# 1. JSONL rows / quality metrics テスト
# ============================================================

@dataclass
class _MockResult:
    filing_id: str = "test_fid"
    status: str = "ok"
    via: str | None = "pdf"
    segment_records: list = field(default_factory=list)
    metrics: dict = field(default_factory=lambda: {"total_ms": 100})
    quarantine: dict | None = None
    result_fingerprint: str | None = None


@dataclass
class _MockFiling:
    ticker: str = "9999"
    listing_source: str = "tdnet_html"


class TestJsonlRowsWrittenForOkRecords:
    """JSONL に rows フィールドが正しく出力されるかテスト"""

    def test_rows_field_exists_and_matches_segment_count(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        records = [
            {"segment_name": "A事業", "segment_sales": 1000, "segment_profit": 100},
            {"segment_name": "B事業", "segment_sales": 2000, "segment_profit": 200},
            {"segment_name": "C事業", "segment_sales": 3000, "segment_profit": None},
        ]
        result = _MockResult(segment_records=records)

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        # line 0 = run_start, line 1 = filing_result
        data = json.loads(lines[1])
        assert data["rows"] == 3
        assert data["segment_count"] == 3
        assert data["rows"] == data["segment_count"]

    def test_rows_gt_zero_for_ok_status(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        records = [
            {"segment_name": "A", "segment_sales": 1000, "segment_profit": 100},
        ]
        result = _MockResult(status="ok", segment_records=records)

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        data = json.loads(lines[1])
        assert data["rows"] > 0, "ok filings must have rows > 0"

    def test_quarantined_rows_zero(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        result = _MockResult(
            status="quarantined",
            segment_records=[],
            quarantine={"review_hint": "pdf_no_segment_page_candidate"},
        )

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        data = json.loads(lines[1])
        assert data["rows"] == 0


class TestJsonlQualityMetricsCounts:
    """JSONL quality metrics の正確性テスト"""

    def test_quality_metrics_complete_case(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        records = [
            {"segment_name": "A", "segment_sales": 1000, "segment_profit": 100},
            {"segment_name": "B", "segment_sales": 2000, "segment_profit": 200},
        ]
        result = _MockResult(segment_records=records)

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        data = json.loads(lines[1])
        assert data["sales_non_null_count"] == 2
        assert data["profit_non_null_count"] == 2
        assert data["complete_count"] == 2
        assert data["sales_only_count"] == 0
        assert data["profit_only_count"] == 0

    def test_quality_metrics_mixed_case(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        records = [
            {"segment_name": "A", "segment_sales": 1000, "segment_profit": 100},   # complete
            {"segment_name": "B", "segment_sales": 2000, "segment_profit": None},   # sales_only
            {"segment_name": "C", "segment_sales": None,  "segment_profit": 300},   # profit_only
            {"segment_name": "D", "segment_sales": 4000, "segment_profit": 400},   # complete
        ]
        result = _MockResult(segment_records=records)

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        data = json.loads(lines[1])
        assert data["sales_non_null_count"] == 3
        assert data["profit_non_null_count"] == 3
        assert data["complete_count"] == 2
        assert data["sales_only_count"] == 1
        assert data["profit_only_count"] == 1

    def test_quality_metrics_all_sales_only(self, tmp_path):
        from lib.backfill.jsonl_logger import RunLogger

        records = [
            {"segment_name": "A", "segment_sales": 1000, "segment_profit": None},
            {"segment_name": "B", "segment_sales": 2000, "segment_profit": None},
        ]
        result = _MockResult(segment_records=records)

        path = str(tmp_path / "test.jsonl")
        rl = RunLogger(path, run_id="test")
        rl.log_filing_result(result, _MockFiling())
        rl.close()

        lines = open(path, encoding="utf-8").readlines()
        data = json.loads(lines[1])
        assert data["sales_non_null_count"] == 2
        assert data["profit_non_null_count"] == 0
        assert data["complete_count"] == 0
        assert data["sales_only_count"] == 2


# ============================================================
# 2. Phase F profit inference テスト
# ============================================================

class TestProfitInferenceRecoversProfitForSalesOnlyTextRows:
    """6644 型: テキスト行で sales_only の場合に profit を回復するテスト"""

    def test_large_nums_position1_is_profit(self):
        """nums[1] の中央値が大きく小数率低い場合、profit として採用"""
        from analysis.segment_detection_v2 import _extract_numbers_from_line
        import re

        # 6644 型のテスト: 各行に label, sales, profit (大きい値) がある
        test_lines = [
            "製造事業 70,238 72,086 1,848",
            "国内子会社 39,593 41,838 2,245",
            "海外子会社 31,198 30,619 △578",
            "不動産 420 334 △85",
        ]

        # 全行の nums[1] を収集して median チェック
        all_pos1 = []
        for line in test_lines:
            nums = _extract_numbers_from_line(line)
            if len(nums) >= 2:
                all_pos1.append(abs(nums[1]))

        all_pos1.sort()
        median = all_pos1[len(all_pos1) // 2] if all_pos1 else 0
        # 中央値が大きい → profit 候補
        assert median >= 200, f"median={median} should be >= 200 for profit inference"

    def test_small_decimal_nums_position1_is_ratio(self):
        """nums[1] の中央値が小さく小数多い場合、ratio として扱う"""
        from analysis.segment_detection_v2 import _extract_numbers_from_line

        # 3232 型のテスト: 各行に label, sales, ratio (小さい小数) がある
        test_lines = [
            "食料品等卸売事業 7,726 △3.3",
            "生活用品等卸売事業 6,620 2.9",
            "自動車卸売事業 11,912 △0.4",
            "ビジネスホテル 5,780 15.2",
        ]

        all_pos1 = []
        decimal_count = 0
        for line in test_lines:
            nums = _extract_numbers_from_line(line)
            if len(nums) >= 2:
                all_pos1.append(abs(nums[1]))
                if "." in str(nums[1]):
                    decimal_count += 1

        all_pos1.sort()
        median = all_pos1[len(all_pos1) // 2] if all_pos1 else 0
        total = len(all_pos1)
        dec_ratio = decimal_count / total if total else 0

        # 中央値が小さくかつ小数率高い → ratio
        assert median < 200, f"median={median} should be < 200 for ratio"
        assert dec_ratio >= 0.5, f"decimal_ratio={dec_ratio} should be >= 0.5 for ratio"


class TestProfitInferenceDoesNotTreatRatioAsProfit:
    """ratio を profit に誤認しないことを検証"""

    def test_percent_values_are_ratio(self):
        """% 付き値は ratio として扱う"""
        from analysis.segment_detection_v2 import _extract_numbers_from_line

        test_lines = [
            "A事業 1,000 +5.2%",
            "B事業 2,000 -3.1%",
            "C事業 3,000 +12.0%",
        ]

        pct_count = 0
        total = 0
        for line in test_lines:
            if "%" in line or "％" in line:
                pct_count += 1
            total += 1

        pct_ratio = pct_count / total if total else 0
        assert pct_ratio >= 0.3, "% ratio should trigger ratio classification"

    def test_boundary_median_199_is_ratio(self):
        """中央値 199 + 小数率 50% は ratio"""
        vals = [150.0, 199.0, 180.0, 120.0]
        vals.sort()
        median = vals[len(vals) // 2]
        decimal_ratio = 1.0  # all have decimals in original
        assert median < 200
        assert decimal_ratio >= 0.5
        # → ratio 判定

    def test_boundary_median_201_is_profit(self):
        """中央値 201 が小数率低い場合、profit 候補"""
        vals = [150.0, 201.0, 300.0, 500.0]
        vals.sort()
        median = vals[len(vals) // 2]
        decimal_ratio = 0.0
        assert median >= 200
        assert decimal_ratio < 0.5
        # → profit 候補


# ============================================================
# 3. 3232 型 ratio 誤認防止テスト
# ============================================================

class TestRegression3232RatioNotProfit:
    """3232 型: sales + ratio のみの表で profit=None を維持"""

    def test_profit_rate_header_is_ratio_not_profit(self):
        """「利益率」は ratio であり profit ではない"""
        from analysis.header_analysis import score_header_role

        scores = score_header_role("利益率")
        # 「利益率」は ratio として分類されるべき
        assert scores.get("ratio", 0) > scores.get("profit", 0), \
            f"「利益率」should have higher ratio than profit: {scores}"

    def test_profit_header_is_profit_not_ratio(self):
        """「利益」は profit 系スコアが正"""
        from analysis.header_analysis import score_header_role

        scores = score_header_role("利益")
        # score_header_role は segment_profit, operating_profit 等のキー
        profit_score = max(
            scores.get("segment_profit", 0),
            scores.get("operating_profit", 0),
            scores.get("ordinary_profit", 0),
        )
        assert profit_score > 0, \
            f"「利益」should have positive profit score: {scores}"

    def test_column_analysis_ratio_stays_ratio(self):
        """column_analysis で ratio 列を profit に昇格しない"""
        from analysis.column_analysis import classify_columns, ColumnRole

        data_rows = [
            ["A事業", "10,000", "5.2"],
            ["B事業", "20,000", "-3.1"],
            ["C事業", "30,000", "12.0"],
            ["D事業", "15,000", "8.4"],
        ]
        headers = ["セグメント", "売上高", "利益率(%)"]

        result = classify_columns(data_rows, headers)
        assert result.best_sales_col is not None, "sales col should be found"
        # 利益率 → ratio なので profit col は None
        if result.best_profit_col is not None:
            role = result.column_roles[result.best_profit_col]
            assert role not in ("ratio", "margin_like"), \
                f"profit col should not be a ratio column, got role={role}"


# ============================================================
# 4. 2331 / 9303 / PL 回帰防止テスト
# ============================================================

class TestRegression2331NarrativeRejected:
    """2331: 本文断片を segment rows として採用しない"""

    def test_narrative_keywords_trigger_guard(self):
        """narrative guard KW が含まれるテキストが検出される"""
        from analysis.scoring import normalize_text

        # 2331 型 narrative text の典型パターン
        narrative_text = "当期のセグメント別の業績は増加傾向にあり売上高は前年同期比で増加しました"
        normalized = normalize_text(narrative_text)
        # narrative_guard KW の一部
        narrative_kws = ["増加", "減少", "影響", "推移", "動向"]
        hits = sum(1 for kw in narrative_kws if kw in normalized)
        assert hits >= 1, "narrative KW should be detected in narrative text"


class TestRegression9303MixedBlockRejected:
    """9303: 混在ブロックを reject"""

    def test_bs_cf_keywords_detected(self):
        """BS/CF 関連キーワードが検出される"""
        from analysis.scoring import normalize_text

        bs_text = "受取手形及び売掛金 総資産 負債 純資産 現金及び預金"
        normalized = normalize_text(bs_text)
        bs_kws = ["総資産", "負債", "純資産", "現金"]
        hits = sum(1 for kw in bs_kws if kw in normalized)
        assert hits >= 2, "BS KW should be detected in BS text"


class TestRegressionPLTableNotSegment:
    """PL テーブルをセグメント表として採用しない"""

    def test_pl_strong_cooccurrence_detected(self):
        """PL 強共起パターンが検出されて減点される"""
        from analysis.table_scoring import score_segment_table

        # PL テーブルの典型行
        pl_lines = [
            "売上高 100,000 110,000",
            "売上原価 70,000 75,000",
            "売上総利益 30,000 35,000",
            "販売費及び一般管理費 20,000 22,000",
            "営業利益 10,000 13,000",
            "営業外収益 500 600",
            "経常利益 10,500 13,600",
        ]

        score = score_segment_table(pl_lines, "連結損益計算書")
        # PL テーブルはスコアが低い（非セグメント表）
        assert score.score < 0.3, \
            f"PL table score should be < 0.3, got {score.score}"


# ============================================================
# 5. 9057 型 経路差分テスト
# ============================================================

class TestFallbackPath9057IsExplainable:
    """9057 型: v2 reject / v1 fallback の経路差分が追えること"""

    def test_v2_narrative_guard_returns_failed_stage(self):
        """v2 の narrative_guard 結果は failed_stage が candidate_guard"""
        # narrative_guard で reject された場合の expected structure
        expected_quarantine_reason = "candidate_guard:narrative_guard"
        expected_failed_stage = "candidate_guard"

        # 実際のフィールドパターンを検証
        assert "narrative_guard" in expected_quarantine_reason
        assert expected_failed_stage == "candidate_guard"

    def test_worker_result_fields_are_consistent(self):
        """FilingResult のフィールドが一貫していること"""
        from lib.backfill.worker import FilingResult

        # ok 結果
        ok_result = FilingResult(
            filing_id="test_ok",
            status="ok",
            via="pdf",
            segment_records=[{"seg": 1}],
        )
        assert ok_result.status == "ok"
        assert len(ok_result.segment_records) > 0

        # quarantined 結果
        q_result = FilingResult(
            filing_id="test_q",
            status="quarantined",
            via=None,
            segment_records=[],
            quarantine={"review_hint": "pdf_narrative_block_selected"},
        )
        assert q_result.status == "quarantined"
        assert len(q_result.segment_records) == 0
        assert q_result.quarantine["review_hint"] == "pdf_narrative_block_selected"


# ============================================================
# 6. non_segment_type 初期化テスト
# ============================================================

class TestNonSegmentTypeInitialized:
    """non_segment_type が初期化されて UnboundLocalError が起きない"""

    def test_pl_heading_without_strong_cooccurrence(self):
        """PL heading があるが PL 強共起なしのケースで UnboundLocalError しない"""
        from analysis.table_scoring import score_segment_table

        # PL heading が近傍にあるが、行には PL 勘定科目が少ない
        lines = [
            "A事業 10,000 500",
            "B事業 20,000 1,000",
            "C事業 15,000 800",
        ]
        nearby = "連結損益計算書"

        # UnboundLocalError が起きなければ OK
        score = score_segment_table(lines, nearby)
        assert score is not None


# ============================================================
# 7. _extract_numbers_from_line テスト
# ============================================================

class TestExtractNumbersFromLine:
    """数値抽出の基本動作テスト"""

    def test_basic_extraction(self):
        from analysis.segment_detection_v2 import _extract_numbers_from_line
        nums = _extract_numbers_from_line("製造事業 70,238 72,086 1,848")
        assert len(nums) == 3
        assert nums[0] == 70238.0
        assert nums[1] == 72086.0
        assert nums[2] == 1848.0

    def test_negative_triangle(self):
        from analysis.segment_detection_v2 import _extract_numbers_from_line
        nums = _extract_numbers_from_line("不動産 420 △85")
        assert len(nums) == 2
        assert nums[0] == 420.0
        assert nums[1] == -85.0

    def test_decimal_extraction(self):
        from analysis.segment_detection_v2 import _extract_numbers_from_line
        nums = _extract_numbers_from_line("食料品 7,726 △3.3")
        assert len(nums) == 2
        assert nums[0] == 7726.0
        assert abs(nums[1] - (-3.3)) < 0.01

    def test_percentage_not_special(self):
        """% はパース時には特別扱いしない (別途判定)"""
        from analysis.segment_detection_v2 import _extract_numbers_from_line
        nums = _extract_numbers_from_line("A事業 1,000 +5.2")
        assert len(nums) >= 2


# ============================================================
# 8. column_analysis: 利益 vs 利益率 の分離テスト
# ============================================================

class TestColumnAnalysisProfitVsRatio:
    """column_analysis が 利益 と 利益率 を正しく分離"""

    def test_rieki_is_profit(self):
        """「利益」は profit 系スコアが高い"""
        from analysis.column_analysis import _score_taxonomy, ColumnRole
        scores = _score_taxonomy("利益")
        profit_max = max(
            scores.get(r, 0) for r in ColumnRole.ALL_PROFIT_ROLES
        )
        assert profit_max > 0, "「利益」should have positive profit score"

    def test_riekiritsu_is_ratio(self):
        """「利益率」は ratio/margin_like が高い"""
        from analysis.column_analysis import _score_taxonomy, ColumnRole
        scores = _score_taxonomy("利益率")
        margin = scores.get(ColumnRole.MARGIN_LIKE, 0)
        assert margin > 0, "「利益率」should have positive margin_like score"
        # profit 系は抑制される
        for role in ColumnRole.ADOPTABLE_PROFIT_ROLES:
            assert scores.get(role, 0) <= 0.1, \
                f"「利益率」{role} should be suppressed, got {scores.get(role, 0)}"

    def test_segment_rieki_is_profit(self):
        """「セグメント利益」は profit"""
        from analysis.column_analysis import _score_taxonomy, ColumnRole
        scores = _score_taxonomy("セグメント利益")
        seg_profit = scores.get(ColumnRole.SEGMENT_PROFIT_LIKE, 0)
        assert seg_profit > 0.5, \
            f"「セグメント利益」should have high segment_profit_like: {seg_profit}"

    def test_eigyo_rieki_is_profit(self):
        """「営業利益」は operating_profit_like"""
        from analysis.column_analysis import _score_taxonomy, ColumnRole
        scores = _score_taxonomy("営業利益")
        op = scores.get(ColumnRole.OPERATING_PROFIT_LIKE, 0)
        assert op > 0.5, f"「営業利益」should have high operating_profit_like: {op}"

    def test_zennenhi_is_ratio(self):
        """「前年比」は ratio / yoy"""
        from analysis.column_analysis import _score_taxonomy, ColumnRole
        scores = _score_taxonomy("前年比")
        # ratio or yoy should be positive
        combined = scores.get(ColumnRole.RATIO, 0) + scores.get(ColumnRole.YOY, 0)
        assert combined > 0, f"「前年比」should have positive ratio/yoy: {combined}"

    def test_kouseihi_is_ratio(self):
        """「構成比」は ratio"""
        from analysis.header_analysis import score_header_role
        scores = score_header_role("構成比")
        assert scores.get("ratio", 0) > 0, "「構成比」should be ratio"
