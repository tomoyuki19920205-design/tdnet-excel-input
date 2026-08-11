"""
tests/test_fix_ticker_normalization.py — fix_ticker_normalization 最適化版テスト
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import logging

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

# テスト対象 import 前に環境変数をセット (load_env が走るため)
import os
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

from tools.fix_ticker_normalization import (
    normalize_ticker_for_fix,
    make_new_source_row_key,
    _collect_distinct_tickers,
    _find_target_tickers,
    _fetch_rows_by_tickers,
    scan_candidates,
    detect_collisions,
    apply_updates,
    BATCH_SIZE,
)


# ============================================================
# normalize_ticker_for_fix テスト
# ============================================================

class TestNormalizeTickerForFix:
    """5桁ticker→4桁変換のfix専用関数テスト。"""

    def test_5digit_trailing_zero(self):
        assert normalize_ticker_for_fix("78490") == "7849"

    def test_5digit_no_trailing_zero(self):
        assert normalize_ticker_for_fix("12345") is None

    def test_4digit_no_conversion(self):
        assert normalize_ticker_for_fix("7849") is None

    def test_alpha_mixed(self):
        assert normalize_ticker_for_fix("418A0") == "418A"

    def test_numeric_code_is_not_mapped_to_alpha(self):
        assert normalize_ticker_for_fix("41700") == "4170"
        assert normalize_ticker_for_fix("41800") == "4180"

    def test_empty_string(self):
        assert normalize_ticker_for_fix("") is None

    def test_3digit(self):
        assert normalize_ticker_for_fix("789") is None

    def test_6digit(self):
        assert normalize_ticker_for_fix("123456") is None

    def test_regular_5digit_trailing_zero(self):
        assert normalize_ticker_for_fix("67580") == "6758"

    def test_alpha_map_flag_does_not_change_numeric_code(self):
        assert normalize_ticker_for_fix("41800", enable_alpha_map=False) == "4180"

    def test_alpha_map_disabled_allows_numeric(self):
        """enable_alpha_map=False: numeric->numeric は許可"""
        assert normalize_ticker_for_fix("78490", enable_alpha_map=False) == "7849"

    def test_alpha_map_flag_cannot_map_numeric_code_to_alpha(self):
        assert normalize_ticker_for_fix("41800", enable_alpha_map=True) == "4180"

    def test_5digit_all_zeros(self):
        result = normalize_ticker_for_fix("00000")
        assert result is None or result == "0000"


# ============================================================
# source_row_key 再生成テスト
# ============================================================

class TestMakeNewSourceRowKey:
    """source_row_key 内の ticker 置換の正しさテスト。"""

    def test_basic_replacement(self):
        old_key = "cf|78490|2026-03-31|1Q|sales|jquants|"
        result = make_new_source_row_key(old_key, "78490", "7849")
        assert result == "cf|7849|2026-03-31|1Q|sales|jquants|"

    def test_alpha_replacement(self):
        old_key = "cf|41800|2025-12-31|4Q|operating_profit|jquants|filing123"
        result = make_new_source_row_key(old_key, "41800", "418A")
        assert result == "cf|418A|2025-12-31|4Q|operating_profit|jquants|filing123"

    def test_numeric_source_row_key_cannot_become_alpha_identity(self):
        old_key = "cf|41700|2025-12-31|FY|sales|jquants|filing123"
        normalized = normalize_ticker_for_fix("41700")
        assert normalized == "4170"
        result = make_new_source_row_key(old_key, "41700", normalized)
        assert result == "cf|4170|2025-12-31|FY|sales|jquants|filing123"
        assert "|417A|" not in result

    def test_only_first_occurrence_replaced(self):
        old_key = "cf|78490|78490|1Q|78490|source|"
        result = make_new_source_row_key(old_key, "78490", "7849")
        assert result == "cf|7849|78490|1Q|78490|source|"

    def test_preserves_other_fields(self):
        old_key = "cf|65010|2025-06-30|2Q|gross_profit|tdnet|abc-def"
        result = make_new_source_row_key(old_key, "65010", "6501")
        assert result == "cf|6501|2025-06-30|2Q|gross_profit|tdnet|abc-def"


# ============================================================
# _find_target_tickers テスト
# ============================================================

class TestFindTargetTickers:
    """5文字tickerのうち正規化可能なものを特定するテスト。"""

    def test_filters_convertible(self):
        all_tickers = {"7849", "78490", "12345", "41800", "1301"}
        targets, skipped = _find_target_tickers(all_tickers)
        # 78490 -> 7849, 41800 -> 418A
        assert set(targets) == {"78490", "41800"}
        assert skipped == 1  # 12345 は5桁だが変換不可

    def test_no_5char_tickers(self):
        all_tickers = {"7849", "1301", "418A"}
        targets, skipped = _find_target_tickers(all_tickers)
        assert targets == []
        assert skipped == 0


# ============================================================
# scan_candidates テスト (モック)
# ============================================================

class TestScanCandidates:
    """2段階方式の候補抽出テスト。"""

    @patch("tools.fix_ticker_normalization._fetch_rows_by_tickers")
    @patch("tools.fix_ticker_normalization._collect_distinct_tickers")
    def test_filters_only_convertible(self, mock_collect, mock_fetch):
        """5桁tickerのうち変換可能なものだけが候補に入る"""
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}

        # Step 1: distinct ticker に 5桁(変換可能/不可) + 4桁 が混在
        mock_collect.return_value = {"78490", "12345", "7849", "1301"}

        # Step 2: 変換可能 ticker (78490) の行
        mock_fetch.return_value = [
            {"source_row_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "ticker": "78490", "period": "2026-03-31", "quarter": "1Q",
             "metric": "sales", "value": 100, "unit": "JPY",
             "source": "jquants", "filing_id": ""},
        ]

        candidates, skipped = scan_candidates(mock_session, mock_config)

        assert len(candidates) == 1
        assert candidates[0]["raw_ticker"] == "78490"
        assert candidates[0]["norm_ticker"] == "7849"
        assert skipped == 1  # "12345" は5桁だが変換不可

    @patch("tools.fix_ticker_normalization._fetch_rows_by_tickers")
    @patch("tools.fix_ticker_normalization._collect_distinct_tickers")
    def test_empty_table_returns_empty(self, mock_collect, mock_fetch):
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        mock_collect.return_value = set()

        candidates, skipped = scan_candidates(mock_session, mock_config)
        assert candidates == []
        assert skipped == 0
        mock_fetch.assert_not_called()


# ============================================================
# _is_unit_mismatch テスト
# ============================================================

from tools.fix_ticker_normalization import _is_unit_mismatch


class TestIsUnitMismatch:
    """値が1000000倍関係か判定するテスト。"""

    def test_yen_to_million(self):
        """32000000 (円) vs 32 (百万円) → True"""
        assert _is_unit_mismatch(32000000, 32) is True

    def test_million_to_yen(self):
        """32 (百万円) vs 32000000 (円) → True"""
        assert _is_unit_mismatch(32, 32000000) is True

    def test_same_value(self):
        assert _is_unit_mismatch(100, 100) is False

    def test_other_ratio(self):
        """1000倍 → False (1M倍のみ許可)"""
        assert _is_unit_mismatch(100000, 100) is False

    def test_zero_zero(self):
        assert _is_unit_mismatch(0, 0) is False

    def test_none_value(self):
        assert _is_unit_mismatch(None, 100) is False
        assert _is_unit_mismatch(100, None) is False

    def test_float_tolerance(self):
        """浮動小数点誤差があっても判定できる"""
        assert _is_unit_mismatch(32000000.001, 32) is True


# ============================================================
# detect_collisions テスト (モック)
# ============================================================

class TestDetectCollisions:
    """source_row_key ベースの衝突判定テスト（3分類対応）。"""

    def test_no_collision(self):
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = []
        mock_session.get.return_value = response

        candidates = [
            {"new_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "old_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "raw_ticker": "78490", "norm_ticker": "7849",
             "value": 100, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        updatable, collisions = detect_collisions(mock_session, mock_config, candidates)
        assert len(updatable) == 1
        assert len(collisions) == 0

    def test_identical_collision(self):
        """同値+同sourceの既存行 → identical"""
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"source_row_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "ticker": "7849", "value": 100, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        mock_session.get.return_value = response

        candidates = [
            {"new_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "old_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "raw_ticker": "78490", "norm_ticker": "7849",
             "value": 100, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        updatable, collisions = detect_collisions(mock_session, mock_config, candidates)
        assert len(collisions) == 1
        assert collisions[0]["collision_type"] == "identical"

    def test_unit_mismatch_collision(self):
        """値が1000000倍関係 + 全フィールド一致 → unit_mismatch"""
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"source_row_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "ticker": "7849", "value": 32, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        mock_session.get.return_value = response

        candidates = [
            {"new_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "old_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "raw_ticker": "78490", "norm_ticker": "7849",
             "value": 32000000, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        updatable, collisions = detect_collisions(mock_session, mock_config, candidates)
        assert len(collisions) == 1
        assert collisions[0]["collision_type"] == "unit_mismatch"

    def test_true_conflict_collision(self):
        """異なる値で1M倍関係でもない → true_conflict"""
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"source_row_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "ticker": "7849", "value": 999, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        mock_session.get.return_value = response

        candidates = [
            {"new_key": "cf|7849|2026-03-31|1Q|sales|jquants|",
             "old_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "raw_ticker": "78490", "norm_ticker": "7849",
             "value": 100, "source": "jquants",
             "period": "2026-03-31", "quarter": "1Q", "metric": "sales"},
        ]
        updatable, collisions = detect_collisions(mock_session, mock_config, candidates)
        assert len(collisions) == 1
        assert collisions[0]["collision_type"] == "true_conflict"


# ============================================================
# apply — dry-run で upsert/delete が呼ばれないことのテスト
# ============================================================

class TestDryRunNoSideEffects:
    """dry-run では apply_updates が呼ばれないことを確認。"""

    @patch("tools.fix_ticker_normalization.apply_updates")
    @patch("tools.fix_ticker_normalization.detect_collisions")
    @patch("tools.fix_ticker_normalization.scan_candidates")
    @patch("tools.fix_ticker_normalization.requests.Session")
    def test_dry_run_skips_apply(self, mock_session_cls, mock_scan, mock_detect, mock_apply):
        mock_scan.return_value = (
            [{"raw_ticker": "78490", "norm_ticker": "7849",
              "old_key": "old", "new_key": "new",
              "period": "2026-03-31", "quarter": "1Q",
              "metric": "sales", "source": "jquants"}],
            0,
        )
        mock_detect.return_value = (
            [{"raw_ticker": "78490", "norm_ticker": "7849",
              "old_key": "old", "new_key": "new"}],
            [],
        )

        from tools.fix_ticker_normalization import main
        with patch("sys.argv", ["prog", "--dry-run"]):
            main()

        mock_apply.assert_not_called()


# ============================================================
# apply_updates テスト (モック)
# ============================================================

class TestApplyUpdates:
    """insert→delete バッチ処理のテスト。"""

    def _make_candidate(self, raw="78490", norm="7849"):
        return {
            "raw_ticker": raw, "norm_ticker": norm,
            "old_key": f"cf|{raw}|2026-03-31|1Q|sales|jquants|",
            "new_key": f"cf|{norm}|2026-03-31|1Q|sales|jquants|",
            "ticker": raw, "period": "2026-03-31", "quarter": "1Q",
            "metric": "sales", "value": 100, "unit": "JPY",
            "source": "jquants",
            "source_row_key": f"cf|{raw}|2026-03-31|1Q|sales|jquants|",
        }

    @patch("tools.fix_ticker_normalization.supabase_upsert")
    @patch("tools.fix_ticker_normalization._safe_delete")
    def test_successful_insert_delete(self, mock_delete, mock_upsert):
        mock_upsert.return_value = {"ok": True, "count": 1}
        mock_delete.return_value = {"ok": True, "status": 200}
        session = MagicMock()
        wc = {"rest_url": "https://test/rest/v1", "headers": {}, "key": "k"}
        rc = {"rest_url": "https://test/rest/v1", "headers": {}}
        stats = apply_updates(session, wc, rc, [self._make_candidate()])
        assert stats["inserted"] == 1
        assert stats["deleted"] == 1
        assert stats["insert_failed"] == 0
        assert stats["delete_failed"] == 0

    @patch("tools.fix_ticker_normalization.supabase_upsert")
    @patch("tools.fix_ticker_normalization._safe_delete")
    def test_insert_success_delete_failure(self, mock_delete, mock_upsert):
        mock_upsert.return_value = {"ok": True, "count": 1}
        mock_delete.return_value = {"ok": False, "error": "timeout"}
        session = MagicMock()
        wc = {"rest_url": "https://test/rest/v1", "headers": {}, "key": "k"}
        rc = {"rest_url": "https://test/rest/v1", "headers": {}}
        stats = apply_updates(session, wc, rc, [self._make_candidate()])
        assert stats["inserted"] == 1
        assert stats["deleted"] == 0
        assert stats["delete_failed"] == 1
        assert stats["insert_failed"] == 0

    @patch("tools.fix_ticker_normalization.supabase_upsert")
    @patch("tools.fix_ticker_normalization._safe_delete")
    def test_insert_failure_skips_delete(self, mock_delete, mock_upsert):
        mock_upsert.return_value = {"ok": False, "error": "server error"}
        session = MagicMock()
        wc = {"rest_url": "https://test/rest/v1", "headers": {}, "key": "k"}
        rc = {"rest_url": "https://test/rest/v1", "headers": {}}
        stats = apply_updates(session, wc, rc, [self._make_candidate()])
        assert stats["insert_failed"] == 1
        assert stats["deleted"] == 0
        mock_delete.assert_not_called()

    @patch("tools.fix_ticker_normalization.supabase_upsert")
    @patch("tools.fix_ticker_normalization._safe_delete")
    def test_batch_splitting(self, mock_delete, mock_upsert):
        mock_upsert.return_value = {"ok": True, "count": BATCH_SIZE}
        mock_delete.return_value = {"ok": True, "status": 200}
        session = MagicMock()
        wc = {"rest_url": "https://test/rest/v1", "headers": {}, "key": "k"}
        rc = {"rest_url": "https://test/rest/v1", "headers": {}}
        candidates = []
        for i in range(BATCH_SIZE + 5):
            raw = f"{7000 + i * 10:05d}"
            norm = f"{7000 + i * 10 // 10:04d}"
            candidates.append(self._make_candidate(raw, norm))
        stats = apply_updates(session, wc, rc, candidates)
        assert stats["batches"] == 2
        assert mock_upsert.call_count == 2


# ============================================================
# ログ出力テスト
# ============================================================

class TestLogging:
    """ログに candidate / collision / updated / delete_failed が含まれること。"""

    @patch("tools.fix_ticker_normalization._fetch_rows_by_tickers")
    @patch("tools.fix_ticker_normalization._collect_distinct_tickers")
    def test_scan_log_contains_counts(self, mock_collect, mock_fetch, caplog):
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        mock_collect.return_value = {"78490"}
        mock_fetch.return_value = [
            {"source_row_key": "cf|78490|2026-03-31|1Q|sales|jquants|",
             "ticker": "78490", "period": "2026-03-31", "quarter": "1Q",
             "metric": "sales", "value": 100, "unit": "JPY",
             "source": "jquants", "filing_id": ""},
        ]
        with caplog.at_level(logging.INFO, logger="fix_ticker_norm"):
            scan_candidates(mock_session, mock_config)
        log_text = " ".join(caplog.messages)
        assert "convertible=" in log_text
        assert "skipped_invalid=" in log_text

    def test_collision_log_contains_counts(self, caplog):
        mock_session = MagicMock()
        mock_config = {"rest_url": "https://test/rest/v1", "headers": {}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = []
        mock_session.get.return_value = response
        with caplog.at_level(logging.INFO, logger="fix_ticker_norm"):
            detect_collisions(mock_session, mock_config, [
                {"new_key": "cf|7849|...", "raw_ticker": "78490", "norm_ticker": "7849"},
            ])
        log_text = " ".join(caplog.messages)
        assert "collisions=" in log_text
        assert "updatable=" in log_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
