#!/usr/bin/env python3
"""
テスト: _is_tanshin_title と classify_disclosure の整合性
       + retryable skip (StateDB.is_processed)
"""
import os
import sys
import tempfile
import sqlite3

import pytest

# プロジェクトルート
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.fetcher import classify_disclosure
from src.extractor import _is_tanshin_title
from src.models import DisclosureType, FINANCIAL_STATEMENT_KEYWORDS, Status
from src.db import StateDB


# ============================================================
# _is_tanshin_title と classify_disclosure の整合テスト
# ============================================================

class TestTanshinKeywordConsistency:
    """共通キーワード定数を使って両関数が整合することを検証"""

    @pytest.mark.parametrize("title,expected", [
        # 決算短信 — 両方 True
        ("2026年３月期 決算短信〔日本基準〕", True),
        ("2026年３月期決算短信〔日本基準〕（連結）", True),

        # 四半期決算 — ★修正前は _is_tanshin_title で False になっていた
        ("2026年５月期 第３四半期決算", True),
        ("2026年３月期 第２四半期決算", True),
        ("2026年12月期 第１四半期決算〔日本基準〕", True),

        # 通期決算
        ("2026年３月期 通期決算", True),

        # 訂正決算短信
        ("（訂正）2025年３月期 訂正決算短信", True),

        # 除外キーワード（説明資料等）→ False
        ("2026年３月期 決算説明会資料", False),
        ("2026年３月期 決算短信 補足資料", False),
        ("2026年３月期 Q&A 決算短信", False),
        ("(再)2026年３月期 決算短信", False),

        # 無関係タイトル → False
        ("人事異動のお知らせ", False),
        ("自己株式取得のお知らせ", False),
        ("業績予想の修正", False),
        ("", False),
    ])
    def test_is_tanshin_title(self, title, expected):
        """_is_tanshin_title が期待通りの結果を返すか"""
        assert _is_tanshin_title(title) == expected, (
            f"title={title!r} expected={expected} got={_is_tanshin_title(title)}"
        )

    @pytest.mark.parametrize("title,expected_type", [
        # FINANCIAL_STATEMENT 判定
        ("2026年３月期 決算短信〔日本基準〕", DisclosureType.FINANCIAL_STATEMENT),
        ("2026年５月期 第３四半期決算", DisclosureType.FINANCIAL_STATEMENT),
        ("2026年３月期 通期決算", DisclosureType.FINANCIAL_STATEMENT),
        ("（訂正）2025年３月期 訂正決算短信", DisclosureType.FINANCIAL_STATEMENT),

        # FORECAST_REVISION 判定
        ("業績予想の修正に関するお知らせ", DisclosureType.FORECAST_REVISION),
        ("通期業績予想と実績との差異に関するお知らせ", DisclosureType.FORECAST_REVISION),

        # 対象外
        ("人事異動のお知らせ", None),
        ("配当予想の修正に関するお知らせ", DisclosureType.DIVIDEND_REVISION),  # dividend_revision として分類
    ])
    def test_classify_disclosure(self, title, expected_type):
        """classify_disclosure が期待通りに分類するか"""
        assert classify_disclosure(title) == expected_type

    def test_keyword_consistency(self):
        """classify_disclosure と _is_tanshin_title が同じキーワード定数を参照していることを確認"""
        # FINANCIAL_STATEMENT_KEYWORDS のすべてのキーワードで
        # classify_disclosure → FINANCIAL_STATEMENT かつ _is_tanshin_title → True
        for kw in FINANCIAL_STATEMENT_KEYWORDS:
            title = f"2026年３月期 {kw}"
            dtype = classify_disclosure(title)
            is_tanshin = _is_tanshin_title(title)
            assert dtype == DisclosureType.FINANCIAL_STATEMENT, (
                f"classify_disclosure({title!r})={dtype}, expected FINANCIAL_STATEMENT"
            )
            assert is_tanshin is True, (
                f"_is_tanshin_title({title!r})={is_tanshin}, expected True"
            )

    def test_quarterly_title_3160_regression(self):
        """3160 大光のタイトル回帰テスト: 「四半期決算」は PDF フォールバック対象"""
        title = "2026年５月期 第３四半期決算"
        assert classify_disclosure(title) == DisclosureType.FINANCIAL_STATEMENT
        assert _is_tanshin_title(title) is True


# ============================================================
# StateDB retryable skip テスト
# ============================================================

class TestRetryableSkip:
    """retryable skip ステータスが is_processed で再処理対象になることを検証"""

    def _make_db(self, tmp_path):
        db_path = os.path.join(str(tmp_path), "test_state.db")
        return StateDB(db_path)

    def test_success_is_processed(self, tmp_path):
        """成功ステータスは処理済み扱い"""
        db = self._make_db(tmp_path)
        db.record("disc-001", "3160", "R8/5", "3Q", Status.SUCCESS)
        assert db.is_processed("disc-001") is True
        db.close()

    def test_parse_failed_is_processed(self, tmp_path):
        """パース失敗も処理済み扱い（再パースは設計上不要）"""
        db = self._make_db(tmp_path)
        db.record("disc-002", "3160", "", "", Status.PARSE_FAILED, error_detail="抽出失敗")
        assert db.is_processed("disc-002") is True
        db.close()

    def test_skipped_not_tanshin_is_retryable(self, tmp_path):
        """SKIPPED_NOT_TANSHIN は未処理扱い（retryable）"""
        db = self._make_db(tmp_path)
        db.record("disc-003", "3160", "", "", Status.SKIPPED_NOT_TANSHIN,
                  error_detail="SKIP_PDF_NOT_TANSHIN: title=第３四半期決算")
        assert db.is_processed("disc-003") is False
        db.close()

    def test_retryable_skip_then_success(self, tmp_path):
        """retryable skip → コード修正後に再ingestで成功"""
        db = self._make_db(tmp_path)

        # 1回目: retryable skip
        db.record("disc-004", "3160", "", "", Status.SKIPPED_NOT_TANSHIN)
        assert db.is_processed("disc-004") is False

        # 2回目: 成功（INSERT OR REPLACE で上書き）
        db.record("disc-004", "3160", "R8/5", "3Q", Status.SUCCESS)
        assert db.is_processed("disc-004") is True

        db.close()

    def test_unknown_id_not_processed(self, tmp_path):
        """未知の disclosure_id は未処理"""
        db = self._make_db(tmp_path)
        assert db.is_processed("unknown-id") is False
        db.close()
