"""tests/test_segment_name_validator.py — セグメント名バリデーターのテスト"""
import pytest
from src.segment.segment_name_validator import (
    validate_segment_name,
    validate_segment_names,
    InvalidReason,
    RowType,
)


# ============================================================
# Layer 1: Deny list — PL勘定科目
# ============================================================

class TestPLAccountDeny:
    """PL勘定科目はinvalidになること。"""

    @pytest.mark.parametrize("name", [
        "販売費及び一般管理費",
        "営業利益",
        "経常利益",
        "受取利息",
        "支払利息",
        "減価償却費",
        "当期純利益",
        "四半期純利益",
        "法人税等",
        "売上原価",
        "売上総利益",
        "特別利益",
        "特別損失",
        "営業外収益",
        "営業外費用",
        "人件費",
        "受取配当金",
        "貸倒引当金戻入額",
        "投資有価証券売却益",
        "包括利益",
        "為替差益",
        "減損損失",
        "契約解約損",
    ])
    def test_pl_account_denied(self, name):
        r = validate_segment_name(name)
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.PL_ACCOUNT
        assert r.row_type == RowType.INVALID

    @pytest.mark.parametrize("name", [
        "非支配株主に帰属する中間純利益",
        "親会社株主に帰属する当期純利益",
        "税金等調整前四半期純利益",
        "税金等調整前中間純損失",
    ])
    def test_pl_partial_match_denied(self, name):
        r = validate_segment_name(name)
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.PL_ACCOUNT


# ============================================================
# Layer 1: Deny list — BS/CF項目
# ============================================================

class TestBSCFDeny:
    @pytest.mark.parametrize("name,expected_reason", [
        ("資産合計", InvalidReason.BS_ITEM),
        ("負債合計", InvalidReason.BS_ITEM),
        ("純資産", InvalidReason.BS_ITEM),
        ("キャッシュフロー", InvalidReason.CF_ITEM),
    ])
    def test_bs_cf_denied(self, name, expected_reason):
        r = validate_segment_name(name)
        assert not r.is_valid
        assert r.invalid_reason == expected_reason


# ============================================================
# Layer 1: Deny list — ヘッダー/単位
# ============================================================

class TestHeaderUnitDeny:
    @pytest.mark.parametrize("name", [
        "売上高",
        "売上",
        "外部顧客への売上高",
        "セグメント利益",
        "営業収益",
        "百万円",
        "前年同期比",
        "増減率",
    ])
    def test_header_unit_denied(self, name):
        r = validate_segment_name(name)
        assert not r.is_valid
        assert r.invalid_reason in (InvalidReason.HEADER_LABEL, InvalidReason.UNIT_ROW)


# ============================================================
# Layer 2: Shape rules
# ============================================================

class TestShapeRules:
    def test_empty_string(self):
        r = validate_segment_name("")
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.TOO_SHORT

    def test_single_char(self):
        r = validate_segment_name("a")
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.TOO_SHORT

    def test_numeric_only(self):
        r = validate_segment_name("12,345")
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.NUMERIC_ONLY

    def test_numeric_with_triangle(self):
        r = validate_segment_name("△1,234")
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.NUMERIC_ONLY

    def test_parenthesis_only(self):
        """全角括弧のみ→normalize後に空文字列→TOO_SHORT。"""
        r = validate_segment_name("（）")
        assert not r.is_valid
        # normalize で括弧が除去され空文字列になるため TOO_SHORT が返る
        assert r.invalid_reason == InvalidReason.TOO_SHORT

    def test_sentence_punctuation(self):
        """句読点を含む文章はNG。"""
        r = validate_segment_name("前連結会計年度において、賃貸用不動産")
        assert not r.is_valid
        assert r.invalid_reason == InvalidReason.PUNCTUATION

    @pytest.mark.parametrize("name", [
        "(自",
        "当社は",
        "（注）",
        "ムは",
        "の内部",
        "円(同",
    ])
    def test_fragment_patterns(self, name):
        """実データで確認された文章断片がNGになること。"""
        r = validate_segment_name(name)
        assert not r.is_valid, f"'{name}' should be invalid but got valid"


# ============================================================
# 補助行 (validだがセグメントではない)
# ============================================================

class TestAuxiliaryRows:
    @pytest.mark.parametrize("name,expected_type", [
        ("合計", RowType.TOTAL),
        ("計", RowType.TOTAL),
        ("調整額", RowType.ADJUSTMENT),
        ("消去", RowType.ADJUSTMENT),
        ("全社", RowType.CORPORATE),
        ("その他", RowType.OTHER_SPECIAL),
    ])
    def test_auxiliary_rows_valid_but_not_segment(self, name, expected_type):
        r = validate_segment_name(name)
        assert r.is_valid
        assert r.row_type == expected_type


# ============================================================
# Layer 3: Allow signal — 正しいセグメント名
# ============================================================

class TestValidSegmentNames:
    @pytest.mark.parametrize("name", [
        "電子部品事業",
        "自動車部門",
        "ヘルスケア関連",
        "半導体製品",
        "クラウドサービス",
        "コンシューマ インターネット",
        "国内飲料",
        "海外食品",
        "日本",
        "北米",
        "欧州",
        "アジア",
        "建材事業",
    ])
    def test_valid_segment_names(self, name):
        r = validate_segment_name(name)
        assert r.is_valid
        assert r.row_type == RowType.SEGMENT
        assert r.confidence >= 0.7

    def test_allow_signal_overrides_punctuation_for_geo_segments(self):
        """地域セグメント名にallow signalがある場合はshape ruleをスキップ。"""
        r = validate_segment_name("日本事業")
        assert r.is_valid
        assert r.row_type == RowType.SEGMENT


# ============================================================
# バッチバリデーション
# ============================================================

class TestBatchValidation:
    def test_validate_segment_names(self):
        names = ["電子部品事業", "販売費及び一般管理費", "", "合計"]
        results = validate_segment_names(names)
        assert len(results) == 4
        assert results[0].is_valid and results[0].row_type == RowType.SEGMENT
        assert not results[1].is_valid
        assert not results[2].is_valid
        assert results[3].is_valid and results[3].row_type == RowType.TOTAL

    def test_real_data_mixed(self):
        """実データのランダムサンプルで誤抽出パターンがNGになること。"""
        bad_names = [
            "販売費及び一般管理費",
            "受取利息",
            "貸倒引当金戻入額",
            "ムは",
            "円(同",
            "(自",
            "当社は",
            "（注）",
            "非支配株主に帰属する中間純利益",
            "中間純利益又は中間純損失(",
            "前第",
            "の内部売上高",
        ]
        for name in bad_names:
            r = validate_segment_name(name)
            assert not r.is_valid, f"'{name}' should be invalid but got valid (reason={r.invalid_reason})"

    def test_real_data_valid(self):
        """実データの正しいセグメント名がvalidになること。"""
        good_names = [
            "コンシューマ インターネット",
            "建材事業",
            "化成品事業",
        ]
        for name in good_names:
            r = validate_segment_name(name)
            assert r.is_valid, f"'{name}' should be valid but got invalid (reason={r.invalid_reason})"
