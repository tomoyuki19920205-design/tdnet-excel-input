"""tests/test_extraction_result_validator.py — 抽出結果バリデーターのテスト

success / partial / quarantine の判定、境界値テスト、
hard_fail_reason の正確性を検証する。
"""
import pytest
from src.segment.extraction_result_validator import (
    validate_extraction_result,
    ExtractionStatus,
    HardFailReason,
    SoftFailReason,
)


# ============================================================
# ヘルパー: テスト用レコード生成
# ============================================================

def _make_record(name: str, sales=100_000_000, profit=10_000_000) -> dict:
    """デフォルトで sales/profit 付きのレコードを生成。"""
    return {
        "segment_name": name,
        "segment_sales": sales,
        "segment_profit": profit,
    }


def _make_records(names: list[str], **kwargs) -> list[dict]:
    """複数レコードをまとめて生成。"""
    return [_make_record(n, **kwargs) for n in names]


def _good_records(count: int = 3) -> list[dict]:
    """典型的な SUCCESS になるレコードセット。"""
    good_names = [
        "電子部品事業", "自動車部門", "ヘルスケア関連",
        "半導体製品", "クラウドサービス", "建材事業",
    ]
    return _make_records(good_names[:count])


# ============================================================
# 空レコード
# ============================================================

class TestEmptyRecords:
    def test_no_records(self):
        r = validate_extraction_result([], source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.NO_RECORDS
        assert r.raw_segment_count == 0
        assert r.valid_segment_count == 0
        assert r.confidence == 1.0


# ============================================================
# Success 判定
# ============================================================

class TestSuccess:
    def test_typical_success(self):
        """3セグメント、sales/profit 全て埋まっている典型的な成功ケース。"""
        records = _good_records(3)
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.hard_fail_reason == HardFailReason.NONE
        assert r.valid_segment_count == 3
        assert r.sales_non_null_count == 3
        assert r.profit_non_null_count == 3
        assert r.account_like_ratio == 0.0
        assert not r.narrative_contamination

    def test_success_with_auxiliary_rows(self):
        """セグメント + 合計/調整額 行がある場合も success。"""
        records = _good_records(3)
        records.append(_make_record("合計"))
        records.append(_make_record("調整額"))
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.SUCCESS
        # 合計/調整額は valid だが SEGMENT ではない → valid_segment_count は3
        assert r.valid_segment_count == 3

    def test_xbrl_higher_confidence_than_pdf(self):
        """XBRL 経由の方が PDF より confidence が高い。"""
        records = _good_records(3)
        r_xbrl = validate_extraction_result(records, source="xbrl")
        r_pdf = validate_extraction_result(records, source="pdf")
        assert r_xbrl.confidence > r_pdf.confidence

    def test_success_boundary_minimum(self):
        """success の最低条件ちょうど: valid=2, sales=2, profit=1。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=10),
            _make_record("自動車部門", sales=200, profit=0),  # profit = 0 → non_null false
        ]
        r = validate_extraction_result(records, source="html")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.valid_segment_count == 2
        assert r.sales_non_null_count == 2
        assert r.profit_non_null_count == 1

    def test_some_invalid_rows_under_threshold(self):
        """invalid 行が 20% 未満なら success。"""
        # 10 good + 1 invalid = 9.1% invalid (< 20% borderline)
        records = _good_records(6)
        records.extend([
            _make_record("電子部品事業", sales=500, profit=50),
            _make_record("海外食品", sales=300, profit=30),
            _make_record("国内飲料", sales=400, profit=40),
            _make_record("北米", sales=200, profit=20),
        ])
        records.append(
            _make_record("販売費及び一般管理費", sales=0, profit=0),
        )
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.invalid_segment_count == 1
        assert len(r.invalid_names) == 1


# ============================================================
# Partial 判定
# ============================================================

class TestPartial:
    def test_weak_profit(self):
        """sales OK だが profit が全て 0 → partial (weak_profit)。"""
        records = _make_records(
            ["電子部品事業", "自動車部門", "ヘルスケア関連"],
            sales=100, profit=0,
        )
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.PARTIAL
        assert r.hard_fail_reason == HardFailReason.NONE
        assert r.profit_non_null_count == 0

    def test_weak_profit_with_null_profit(self):
        """profit が None → partial。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=None),
            _make_record("自動車部門", sales=200, profit=None),
            _make_record("ヘルスケア関連", sales=300, profit=None),
        ]
        r = validate_extraction_result(records, source="html")
        assert r.status == ExtractionStatus.PARTIAL
        assert r.profit_non_null_count == 0

    def test_borderline_invalid_ratio(self):
        """invalid ratio 20-30% → partial。"""
        # 4 good + 1 invalid = 20% invalid
        records = _good_records(3)
        records.append(_make_record("海外食品", sales=100, profit=10))
        records.append(_make_record("販売費及び一般管理費", sales=0, profit=0))
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.PARTIAL


# ============================================================
# Quarantine 判定
# ============================================================

class TestQuarantine:
    def test_too_few_valid_segments(self):
        """有効セグメントが1つだけ → quarantine (too_few_valid_segments)。"""
        # account_like_ratio < 50% になるようにフラグメント行で埋める
        records = [
            _make_record("電子部品事業", sales=100, profit=10),
            _make_record("12,345", sales=0, profit=0),       # NUMERIC_ONLY (account ではない)
            _make_record("の内部", sales=0, profit=0),        # FRAGMENT
        ]
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_VALID_SEGMENTS

    def test_too_few_sales(self):
        """sales が全て 0 → quarantine。"""
        records = _make_records(
            ["電子部品事業", "自動車部門", "ヘルスケア関連"],
            sales=0, profit=10,
        )
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_SALES

    def test_high_invalid_ratio(self):
        """invalid 行が 30% 以上 → quarantine。"""
        # 2 good + 3 invalid = 60% invalid → 先に too_few_valid で引っかかる可能性
        # → 7 good + 3 invalid = 30% invalid で境界テスト
        records = _good_records(6)
        records.append(_make_record("海外食品", sales=100, profit=10))
        records.extend([
            _make_record("販売費及び一般管理費"),
            _make_record("受取利息"),
            _make_record("支払利息"),
        ])
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.HIGH_INVALID_RATIO

    def test_narrative_contamination(self):
        """叙述文が多い場合 → quarantine。"""
        records = [
            _make_record("当社グループにおいて推進した結果", sales=100, profit=10),
            _make_record("今後についてはさらなる成長に関して", sales=200, profit=20),
            _make_record("電子部品事業", sales=300, profit=30),
        ]
        # 3件中2件 (67%) に叙述パターン → narrative contamination
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.NARRATIVE_CONTAMINATION
        assert r.narrative_contamination

    def test_account_like_dominant(self):
        """PL勘定科目行が50%以上 → quarantine。"""
        records = [
            _make_record("売上原価"),
            _make_record("営業利益"),
            _make_record("経常利益"),
            _make_record("電子部品事業", sales=100, profit=10),
        ]
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.ACCOUNT_LIKE_DOMINANT

    def test_single_valid_segment_xbrl_quarantine(self):
        """有効セグメント1つ (XBRL, 例外off) → quarantine。"""
        records = [_make_record("電子部品事業", sales=100, profit=10)]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_VALID_SEGMENTS

    def test_single_valid_segment_pdf_quarantine(self):
        """有効セグメント1つ (PDF) → 従来通り quarantine。"""
        records = [_make_record("電子部品事業", sales=100, profit=10)]
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_VALID_SEGMENTS


# ============================================================
# 境界値テスト
# ============================================================

class TestBoundaryValues:
    def test_exact_2_valid_2_sales_1_profit(self):
        """最低条件ぴったり → success。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=10),
            _make_record("自動車部門", sales=200, profit=0),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.valid_segment_count == 2
        assert r.sales_non_null_count == 2
        assert r.profit_non_null_count == 1

    def test_1_valid_xbrl_is_quarantine(self):
        """有効セグメント1つ (XBRL, 例外off) → quarantine。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=10),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.QUARANTINE

    def test_1_sales_is_quarantine(self):
        """sales 1つだけ → quarantine。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=10),
            _make_record("自動車部門", sales=0, profit=10),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.QUARANTINE

    def test_0_profit_is_partial(self):
        """profit 0 → partial (success ではない)。"""
        records = [
            _make_record("電子部品事業", sales=100, profit=0),
            _make_record("自動車部門", sales=200, profit=0),
            _make_record("ヘルスケア関連", sales=300, profit=0),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.PARTIAL

    def test_29_percent_invalid_is_not_quarantine(self):
        """invalid 29% → quarantine にならない (境界値)。"""
        # 5 good + 2 invalid = 28.6% < 30%
        records = _good_records(5)
        records.extend([
            _make_record("販売費及び一般管理費"),
            _make_record("受取利息"),
        ])
        r = validate_extraction_result(records, source="pdf")
        assert r.status != ExtractionStatus.QUARANTINE

    def test_30_percent_invalid_is_quarantine(self):
        """invalid 30% → quarantine (境界値)。"""
        # 7 good + 3 invalid = 30%
        records = _good_records(6)
        records.append(_make_record("海外食品", sales=100, profit=10))
        records.extend([
            _make_record("販売費及び一般管理費"),
            _make_record("受取利息"),
            _make_record("支払利息"),
        ])
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE


# ============================================================
# 返り値フィールド検証
# ============================================================

class TestReturnFields:
    def test_all_fields_present(self):
        """全フィールドが返り値に含まれること。"""
        records = _good_records(3)
        r = validate_extraction_result(records, source="xbrl")
        assert hasattr(r, "status")
        assert hasattr(r, "confidence")
        assert hasattr(r, "reason")
        assert hasattr(r, "hard_fail_reason")
        assert hasattr(r, "raw_segment_count")
        assert hasattr(r, "valid_segment_count")
        assert hasattr(r, "invalid_segment_count")
        assert hasattr(r, "sales_non_null_count")
        assert hasattr(r, "profit_non_null_count")
        assert hasattr(r, "invalid_names")
        assert hasattr(r, "account_like_ratio")
        assert hasattr(r, "narrative_contamination")

    def test_invalid_names_populated(self):
        """invalid_names に不正名が入ること。"""
        records = _good_records(3)
        records.append(_make_record("販売費及び一般管理費"))
        r = validate_extraction_result(records, source="pdf")
        assert "販売費及び一般管理費" in r.invalid_names

    def test_raw_vs_valid_count(self):
        """raw_segment_count >= valid_segment_count。"""
        records = _good_records(3)
        records.append(_make_record("受取利息"))
        r = validate_extraction_result(records, source="pdf")
        assert r.raw_segment_count == 4
        assert r.valid_segment_count == 3
        assert r.invalid_segment_count == 1

    def test_source_in_reason(self):
        """reason にソース名が含まれること。"""
        records = _good_records(3)
        r = validate_extraction_result(records, source="xbrl")
        assert "xbrl" in r.reason


# ============================================================
# 実データパターン
# ============================================================

class TestRealDataPatterns:
    def test_pl_table_misextracted(self):
        """PL表を誤抽出した場合 → quarantine。"""
        records = [
            _make_record("売上原価", sales=500, profit=0),
            _make_record("販売費及び一般管理費", sales=300, profit=0),
            _make_record("営業利益", sales=0, profit=200),
            _make_record("経常利益", sales=0, profit=180),
            _make_record("当期純利益", sales=0, profit=120),
        ]
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE

    def test_correct_segment_table(self):
        """正しいセグメント表 → success。"""
        records = [
            _make_record("コンシューマ インターネット", sales=50_000, profit=5_000),
            _make_record("エンタープライズ", sales=30_000, profit=3_000),
            _make_record("フィンテック", sales=20_000, profit=2_000),
            _make_record("合計", sales=100_000, profit=10_000),
            _make_record("調整額", sales=0, profit=-1_000),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.valid_segment_count == 3  # 合計/調整額は SEGMENT 以外

    def test_mixed_good_bad_with_null_profit(self):
        """セグメント名は正しいが profit が全 NULL → partial。"""
        records = [
            {"segment_name": "日本", "segment_sales": 1000, "segment_profit": None},
            {"segment_name": "北米", "segment_sales": 2000, "segment_profit": None},
            {"segment_name": "欧州", "segment_sales": 3000, "segment_profit": None},
        ]
        r = validate_extraction_result(records, source="html")
        assert r.status == ExtractionStatus.PARTIAL
        assert r.sales_non_null_count == 3
        assert r.profit_non_null_count == 0


# ============================================================
# Single-Segment 例外テスト
# ============================================================

class TestSingleSegmentException:
    """XBRL source 限定の single-segment 許容ルールのテスト。"""

    def test_xbrl_single_segment_quarantine_when_flag_off(self):
        """XBRL + valid=1 + flag off → quarantine。"""
        records = [_make_record("Protective Clothing And Environmental Materials", sales=3303, profit=355)]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_VALID_SEGMENTS

    def test_xbrl_single_segment_partial_when_flag_on(self):
        """XBRL + valid=1 + flag on → PARTIAL。"""
        import src.segment.extraction_result_validator as mod
        orig = mod._ENABLE_SINGLE_SEGMENT_EXCEPTION
        try:
            mod._ENABLE_SINGLE_SEGMENT_EXCEPTION = True
            records = [_make_record("Protective Clothing And Environmental Materials", sales=3303, profit=355)]
            r = validate_extraction_result(records, source="xbrl")
            assert r.status == ExtractionStatus.PARTIAL
            assert r.hard_fail_reason == HardFailReason.NONE
            assert "single-segment" in r.reason
        finally:
            mod._ENABLE_SINGLE_SEGMENT_EXCEPTION = orig

    def test_pdf_single_segment_quarantine(self):
        """PDF + valid=1 → 従来通り quarantine。"""
        records = [_make_record("Protective Clothing And Environmental Materials", sales=3303, profit=355)]
        r = validate_extraction_result(records, source="pdf")
        assert r.status == ExtractionStatus.QUARANTINE
        assert r.hard_fail_reason == HardFailReason.TOO_FEW_VALID_SEGMENTS

    def test_xbrl_single_segment_no_sales_quarantine_even_flag_on(self):
        """XBRL + valid=1 だが sales=0 → flag on でも quarantine。"""
        import src.segment.extraction_result_validator as mod
        orig = mod._ENABLE_SINGLE_SEGMENT_EXCEPTION
        try:
            mod._ENABLE_SINGLE_SEGMENT_EXCEPTION = True
            records = [_make_record("電子部品事業", sales=0, profit=10)]
            r = validate_extraction_result(records, source="xbrl")
            assert r.status == ExtractionStatus.QUARANTINE
        finally:
            mod._ENABLE_SINGLE_SEGMENT_EXCEPTION = orig

    def test_xbrl_zero_valid_still_quarantine(self):
        """XBRL + valid=0 → quarantine (例外適用されない)。"""
        records = [_make_record("販売費及び一般管理費", sales=100, profit=10)]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.QUARANTINE


class TestPdfCompat:
    """source=pdf_compat (v1互換モード) のテスト。"""

    def test_pdf_compat_bypasses_account_like(self):
        """pdf_compat は account_like_dominant をスキップして ok を返す。"""
        records = [
            _make_record("売上原価", sales=100, profit=10),
            _make_record("販売費及び一般管理費", sales=200, profit=20),
            _make_record("電子部品事業", sales=300, profit=30),
        ]
        # pdf (strict) → quarantine
        r_strict = validate_extraction_result(records, source="pdf")
        assert r_strict.status == ExtractionStatus.QUARANTINE
        # pdf_compat → ok (v1互換)
        r_compat = validate_extraction_result(records, source="pdf_compat")
        assert r_compat.status == ExtractionStatus.SUCCESS
        assert "v1互換" in r_compat.reason

    def test_pdf_compat_bypasses_high_invalid_ratio(self):
        """pdf_compat は high_invalid_ratio をスキップして ok を返す。"""
        records = [
            _make_record("第1四半期", sales=100, profit=10),
            _make_record("第2四半期", sales=200, profit=20),
            _make_record("電子部品事業", sales=300, profit=30),
            _make_record("機械事業", sales=400, profit=40),
        ]
        # pdf_compat → ok
        r = validate_extraction_result(records, source="pdf_compat")
        assert r.status == ExtractionStatus.SUCCESS

    def test_pdf_compat_single_segment_ok(self):
        """pdf_compat は valid=1, sales=1 で ok を返す。"""
        records = [_make_record("電子部品事業", sales=100, profit=10)]
        # pdf (strict) → quarantine
        r_strict = validate_extraction_result(records, source="pdf")
        assert r_strict.status == ExtractionStatus.QUARANTINE
        # pdf_compat → ok
        r_compat = validate_extraction_result(records, source="pdf_compat")
        assert r_compat.status == ExtractionStatus.SUCCESS

    def test_pdf_compat_no_valid_segments_but_sales_ok(self):
        """pdf_compat は valid=0 でも sales>=1 なら ok (v1互換)。"""
        records = [_make_record("販売費及び一般管理費", sales=100, profit=10)]
        r = validate_extraction_result(records, source="pdf_compat")
        assert r.status == ExtractionStatus.SUCCESS

    def test_pdf_compat_no_sales_quarantine(self):
        """pdf_compat でも sales=0 は quarantine。"""
        records = [_make_record("電子部品事業", sales=0, profit=0)]
        r = validate_extraction_result(records, source="pdf_compat")
        assert r.status == ExtractionStatus.QUARANTINE


class TestXbrlShortAbbreviationExempt:
    """XBRL source 限定の too_short_no_signal 免除テスト。"""

    def test_d2c_rescued_with_xbrl_source(self):
        """D2C は XBRL source で sales ありなら valid に復帰。"""
        records = [
            _make_record("Media Solutions", sales=7986, profit=971),
            _make_record("D2C", sales=1432, profit=94),
            _make_record("Entertainment", sales=1900, profit=194),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.status == ExtractionStatus.SUCCESS
        assert r.valid_segment_count == 3
        assert r.invalid_segment_count == 0

    def test_d2c_not_rescued_with_pdf_source(self):
        """D2C は PDF source では免除されない。"""
        records = [
            _make_record("Media Solutions", sales=7986, profit=971),
            _make_record("D2C", sales=1432, profit=94),
            _make_record("Entertainment", sales=1900, profit=194),
        ]
        r = validate_extraction_result(records, source="pdf")
        assert r.invalid_segment_count == 1  # D2C は invalid のまま

    def test_roman_numeral_not_rescued(self):
        """ローマ数字単独 (IV) は免除しない。"""
        records = [
            _make_record("セグメントA事業", sales=100, profit=10),
            _make_record("IV", sales=50, profit=5),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.invalid_segment_count >= 1

    def test_no_fact_not_rescued(self):
        """sales/profit ともに None なら免除しない。"""
        records = [
            _make_record("セグメントA事業", sales=100, profit=10),
            _make_record("AB", sales=None, profit=None),
        ]
        r = validate_extraction_result(records, source="xbrl")
        assert r.invalid_segment_count >= 1
