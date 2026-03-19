# ============================================================
# test_excel_parser.py — Excelパーサーのユニットテスト
# ============================================================
"""
openpyxlで動的にExcelファイルを生成し、各パーサーロジックを検証する。
"""
from __future__ import annotations

import os
import tempfile

import openpyxl
import pytest

from src.migration.excel_parser import (
    _detect_company_code,
    _normalize_quarter,
    _is_header_row,
    _parse_fiscal_year_end,
    parse_excel,
)


# ------------------------------------------------------------------
# ヘルパー: テスト用Excel作成
# ------------------------------------------------------------------
def _create_test_excel(
    rows: list[dict],
    sheet_name: str = "PL",
) -> str:
    """
    行データのリストからExcelファイルを生成する。

    rows: [ {col_letter: value, ...}, ... ]
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    for i, row_data in enumerate(rows, start=1):
        for col_letter, value in row_data.items():
            ws[f"{col_letter}{i}"] = value

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    wb.close()
    return path


# ------------------------------------------------------------------
# _detect_company_code テスト
# ------------------------------------------------------------------
class TestDetectCompanyCode:
    def test_valid_4digit(self):
        assert _detect_company_code("7203") == "7203"

    def test_valid_5digit(self):
        assert _detect_company_code("72030") == "72030"

    def test_none(self):
        assert _detect_company_code(None) is None

    def test_empty(self):
        assert _detect_company_code("") is None

    def test_text(self):
        assert _detect_company_code("トヨタ") is None

    def test_too_short(self):
        assert _detect_company_code("123") is None

    def test_zenkaku(self):
        assert _detect_company_code("７２０３") == "7203"


# ------------------------------------------------------------------
# _normalize_quarter テスト
# ------------------------------------------------------------------
class TestNormalizeQuarter:
    def test_1q(self):
        assert _normalize_quarter("1Q") == "1Q"

    def test_4q(self):
        assert _normalize_quarter("4Q") == "4Q"

    def test_lowercase(self):
        assert _normalize_quarter("2q") == "2Q"

    def test_number_only(self):
        assert _normalize_quarter("3") == "3Q"

    def test_invalid(self):
        assert _normalize_quarter("5Q") is None

    def test_none(self):
        assert _normalize_quarter(None) is None

    def test_text(self):
        assert _normalize_quarter("通期") is None

    def test_zenkaku_full(self):
        """全角数字＋全角Q: '１Ｑ' → '1Q'"""
        assert _normalize_quarter("１Ｑ") == "1Q"

    def test_zenkaku_2q(self):
        """全角: '２Ｑ' → '2Q'"""
        assert _normalize_quarter("２Ｑ") == "2Q"

    def test_zenkaku_3q(self):
        """全角: '３Ｑ' → '3Q'"""
        assert _normalize_quarter("３Ｑ") == "3Q"

    def test_zenkaku_4q(self):
        """全角: '４Ｑ' → '4Q'"""
        assert _normalize_quarter("４Ｑ") == "4Q"

    def test_mixed_half_num_zen_q(self):
        """半角数字＋全角Q: '3Ｑ' → '3Q'"""
        assert _normalize_quarter("3Ｑ") == "3Q"

    def test_zenkaku_small_q(self):
        """全角小文字q: '４ｑ' → '4Q'"""
        assert _normalize_quarter("４ｑ") == "4Q"

    def test_zenkaku_number_only(self):
        """全角数字のみ: '２' → '2Q'"""
        assert _normalize_quarter("２") == "2Q"


# ------------------------------------------------------------------
# _is_header_row テスト（タプルベース・緩和条件）
#   O列必須（「売上」含む） + P/Q/Rのうち2つ以上一致でTrue
# ------------------------------------------------------------------
class TestIsHeaderRow:
    def test_valid_header_all_match(self):
        """O〜R全一致: True"""
        row = (None,) * 14 + ("売上", "粗利", "粗利率", "管理費", None)
        assert _is_header_row(row) is True

    def test_valid_header_two_aux(self):
        """O列必須 + P+Q一致 (R空文字): True"""
        row = (None,) * 14 + ("売上", "粗利", "粗利率", "", None)
        assert _is_header_row(row) is True

    def test_valid_header_r_missing_none(self):
        """O列 + P+Q一致 (R=None): True"""
        row = (None,) * 14 + ("売上", "粗利", "粗利率", None, None)
        assert _is_header_row(row) is True

    def test_valid_header_p_and_r(self):
        """O列 + P+R一致 (Q空): True"""
        row = (None,) * 14 + ("売上", "粗利", "", "管理費", None)
        assert _is_header_row(row) is True

    def test_invalid_only_one_aux(self):
        """O列 + P/Q/Rのうち1つだけ一致: False"""
        row = (None,) * 14 + ("売上", "粗利", "", "", None)
        assert _is_header_row(row) is False

    def test_no_o_column(self):
        """O列に売上なし: False"""
        row = (None,) * 14 + ("", "粗利", "粗利率", "管理費", None)
        assert _is_header_row(row) is False

    def test_uriage_shunyu(self):
        """O列が「売上収益」でもTrue"""
        row = (None,) * 14 + ("売上収益", "粗利", "粗利率", "管理費", None)
        assert _is_header_row(row) is True

    def test_short_tuple(self):
        row = (None,) * 10
        assert _is_header_row(row) is False


# ------------------------------------------------------------------
# _parse_fiscal_year_end テスト
# ------------------------------------------------------------------
class TestParseFiscalYearEnd:
    def test_reiwa_march(self):
        assert _parse_fiscal_year_end("R8/3") == "2026-03-31"

    def test_reiwa_december(self):
        assert _parse_fiscal_year_end("R7/12") == "2025-12-31"

    def test_western_slash(self):
        assert _parse_fiscal_year_end("2026/3") == "2026-03-31"

    def test_western_kanji(self):
        assert _parse_fiscal_year_end("2026年3月") == "2026-03-31"

    def test_reiwa_kanji(self):
        assert _parse_fiscal_year_end("令和8年3月") == "2026-03-31"

    def test_with_ki_suffix(self):
        assert _parse_fiscal_year_end("R8/3期") == "2026-03-31"

    def test_february(self):
        # 2月末日: 閏年でない場合
        assert _parse_fiscal_year_end("R7/2") == "2025-02-28"

    def test_none(self):
        assert _parse_fiscal_year_end(None) is None

    def test_empty(self):
        assert _parse_fiscal_year_end("") is None

    def test_bad_month_33(self):
        """月=33は範囲外 → None（例外なし）"""
        assert _parse_fiscal_year_end("R8/33") is None

    def test_bad_month_0(self):
        """月=0は範囲外 → None"""
        assert _parse_fiscal_year_end("2026/0") is None

    def test_noise_number_string(self):
        """数字ノイズ文字列は年度として解釈しない"""
        assert _parse_fiscal_year_end("124012") is None

    def test_noise_with_semicolon(self):
        """セミコロン混じりノイズは年度として解釈しない"""
        assert _parse_fiscal_year_end("11000；1870") is None

    def test_noise_partial_match(self):
        """数字/月のような部分一致は拒否"""
        assert _parse_fiscal_year_end("2026/3extra") is None


# ------------------------------------------------------------------
# 統合テスト: parse_excel
# ------------------------------------------------------------------
class TestParseExcel:
    def test_basic_single_company(self):
        """基本: 1社4四半期の正常パース"""
        rows = [
            # row 1: 企業コード + C列メモ
            {"A": "7203", "C": "トヨタ自動車"},
            # row 2: ヘッダー行
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            # row 3: 1Q（M列に年度）
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30,
             "Q": 0.30, "R": 10, "S": 20, "Z": "好調"},
            # row 4: 2Q（年度引継ぎ）
            {"N": "2Q", "O": 210, "P": 65, "Q": 0.31, "R": 22, "S": 43},
            # row 5: 3Q
            {"N": "3Q", "O": 320, "P": 100, "Q": 0.31, "R": 33, "S": 67},
            # row 6: 4Q
            {"N": "4Q", "O": 440, "P": 140, "Q": 0.32, "R": 44, "S": 96},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 1

            block = result.blocks[0]
            assert block.company_code == "7203"
            assert block.memo_c == "トヨタ自動車"
            assert len(block.records) == 4

            # 1Q
            r1 = block.records[0]
            assert r1.quarter == "1Q"
            assert r1.fiscal_year_end == "2026-03-31"
            assert r1.sales == 100
            assert r1.note == "好調"

            # 2Q: 年度引継ぎ
            r2 = block.records[1]
            assert r2.quarter == "2Q"
            assert r2.fiscal_year_end == "2026-03-31"
            assert r2.sales == 210

            # 4Q
            r4 = block.records[3]
            assert r4.quarter == "4Q"
            assert r4.operating_profit == 96
        finally:
            os.unlink(path)

    def test_two_companies(self):
        """2社が縦並びの場合"""
        rows = [
            # 会社1
            {"A": "7203", "C": "トヨタ"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30, "R": 10, "S": 20},
            {"N": "2Q", "O": 210, "P": 65, "Q": 0.31, "R": 22, "S": 43},
            # 会社2
            {"A": "6758"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R7/3", "N": "1Q", "O": 50, "P": 15, "Q": 0.30, "R": 5, "S": 10},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 2
            assert result.blocks[0].company_code == "7203"
            assert len(result.blocks[0].records) == 2
            assert result.blocks[1].company_code == "6758"
            assert len(result.blocks[1].records) == 1
            assert result.blocks[1].records[0].fiscal_year_end == "2025-03-31"
        finally:
            os.unlink(path)

    def test_skip_no_code_block(self):
        """企業コードなしブロックはスキップ"""
        rows = [
            # ヘッダーのみ、企業コードなし
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30, "R": 10, "S": 20},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 0
        finally:
            os.unlink(path)

    def test_150_row_limit(self):
        """150行超過でSKIP_DISTANCEログ"""
        rows: list[dict] = [{"A": "7203"}]
        # ヘッダーなし、151行分の空行を追加（実際に行ができるようにダミー値を入れる）
        for _ in range(155):
            rows.append({"B": "dummy"})
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            # SKIP_DISTANCEログが出ている
            skip_logs = [l for l in result.logs if l.log_type == "SKIP_DISTANCE"]
            assert len(skip_logs) >= 1
            assert skip_logs[0].company_code == "7203"
        finally:
            os.unlink(path)

    def test_year_inheritance(self):
        """1Qの年度が2Q〜4Qに引き継がれる"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30, "R": 10, "S": 20},
            {"N": "3Q", "O": 300, "P": 90, "Q": 0.30, "R": 30, "S": 60},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 1
            assert len(result.blocks[0].records) == 2
            # 3Qは1Qの年度を引き継ぐ
            assert result.blocks[0].records[1].fiscal_year_end == "2026-03-31"
        finally:
            os.unlink(path)

    def test_z_column_memo_with_newline(self):
        """Z列メモの改行保持"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30,
             "Q": 0.30, "R": 10, "S": 20, "Z": "1行目\n2行目\n3行目"},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert result.blocks[0].records[0].note == "1行目\n2行目\n3行目"
        finally:
            os.unlink(path)

    def test_segment_parsing(self):
        """AA列以降のセグメントペア読み取り"""
        rows = [
            {"A": "7203"},
            # ヘッダー: PL + セグメント名
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費",
             "AA": "自動車売上", "AB": "自動車利益", "AC": "金融売上", "AD": "金融利益"},
            # データ行
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30,
             "R": 10, "S": 20, "AA": 80, "AB": 15, "AC": 20, "AD": 5},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            rec = result.blocks[0].records[0]
            assert len(rec.segments) == 2
            assert rec.segments[0].segment_name == "自動車売上"
            assert rec.segments[0].segment_sales == 80
            assert rec.segments[0].segment_profit == 15
            assert rec.segments[0].segment_order == 0
            assert rec.segments[1].segment_name == "金融売上"
            assert rec.segments[1].segment_sales == 20
            assert rec.segments[1].segment_profit == 5
            assert rec.segments[1].segment_order == 1
        finally:
            os.unlink(path)

    def test_segment_unknown_name(self):
        """セグメント名欠損時にUNKNOWN_N"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30,
             "R": 10, "S": 20, "AA": 80, "AB": 15},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            rec = result.blocks[0].records[0]
            assert len(rec.segments) == 1
            assert rec.segments[0].segment_name == "UNKNOWN_1"
        finally:
            os.unlink(path)

    def test_multiple_fiscal_years(self):
        """同一企業内に複数年度（1Q→2Q→次の1Q→2Q）"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            # 年度1
            {"M": "R7/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30, "R": 10, "S": 20},
            {"N": "2Q", "O": 200, "P": 60, "Q": 0.30, "R": 20, "S": 40},
            {"N": "3Q", "O": 300, "P": 90, "Q": 0.30, "R": 30, "S": 60},
            {"N": "4Q", "O": 400, "P": 120, "Q": 0.30, "R": 40, "S": 80},
            # 年度2
            {"M": "R8/3", "N": "1Q", "O": 110, "P": 33, "Q": 0.30, "R": 11, "S": 22},
            {"N": "2Q", "O": 220, "P": 66, "Q": 0.30, "R": 22, "S": 44},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 1
            recs = result.blocks[0].records
            assert len(recs) == 6
            # 年度1の4レコード
            for r in recs[:4]:
                assert r.fiscal_year_end == "2025-03-31"
            # 年度2の2レコード
            for r in recs[4:]:
                assert r.fiscal_year_end == "2026-03-31"
        finally:
            os.unlink(path)

    def test_wrong_sheet_name(self):
        """存在しないシート名でエラー"""
        path = _create_test_excel([{"A": "test"}])
        try:
            with pytest.raises(ValueError, match="シート .* が見つかりません"):
                parse_excel(path, "NONEXISTENT")
        finally:
            os.unlink(path)

    def test_zenkaku_quarter_integration(self):
        """統合テスト: 全角四半期 '１Ｑ' でパースできること"""
        rows = [
            {"A": "7203"},
            {"O": "売上", "P": "粗利", "Q": "粗利率", "R": "管理費"},
            {"M": "R8/3", "N": "１Ｑ", "O": 100, "P": 30, "Q": 0.30, "R": 10, "S": 20},
            {"N": "２Ｑ", "O": 210, "P": 65, "Q": 0.31, "R": 22, "S": 43},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 1
            assert len(result.blocks[0].records) == 2
            assert result.blocks[0].records[0].quarter == "1Q"
            assert result.blocks[0].records[1].quarter == "2Q"
        finally:
            os.unlink(path)

    def test_relaxed_header_detection(self):
        """統合テスト: ヘッダーのR列が欠損でもパースできること"""
        rows = [
            {"A": "7203"},
            # R列（管理費）なし、O+P+Qの3列のうち2つ補助一致
            {"O": "売上", "P": "粗利", "Q": "粗利率"},
            {"M": "R8/3", "N": "1Q", "O": 100, "P": 30, "Q": 0.30, "S": 20},
        ]
        path = _create_test_excel(rows)
        try:
            result = parse_excel(path, "PL")
            assert len(result.blocks) == 1
            assert len(result.blocks[0].records) == 1
            assert result.blocks[0].records[0].sales == 100
        finally:
            os.unlink(path)
