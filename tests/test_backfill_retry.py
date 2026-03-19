"""tests/test_backfill_retry.py — retry 制御テスト"""
from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.backfill.retry import (
    retry_with_backoff, RetryConfig, TimeoutConfig, classify_review_hint,
)


class TestRetryWithBackoff:
    def test_success_first_attempt(self):
        result = retry_with_backoff(
            lambda: "ok",
            stage="download",
            max_attempts=3,
            sleep_fn=lambda _: None,
        )
        assert result.success is True
        assert result.value == "ok"
        assert result.attempts == 1

    def test_retry_then_success(self):
        attempts = [0]

        def _fn():
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("transient error")
            return "recovered"

        result = retry_with_backoff(
            _fn,
            stage="download",
            max_attempts=3,
            sleep_fn=lambda _: None,
        )
        assert result.success is True
        assert result.value == "recovered"
        assert result.attempts == 3

    def test_all_attempts_exhausted(self):
        result = retry_with_backoff(
            lambda: (_ for _ in ()).throw(RuntimeError("always fail")),
            stage="xbrl",
            max_attempts=2,
            sleep_fn=lambda _: None,
        )
        assert result.success is False
        assert result.attempts == 2
        assert "always fail" in result.last_error

    def test_timeout_stops_retry(self):
        """timeout_sec を超えると停止。"""
        call_count = [0]

        def _slow_fn():
            call_count[0] += 1
            import time
            time.sleep(0.05)  # 各 attempt で少し時間を使う
            raise RuntimeError("fail")

        result = retry_with_backoff(
            _slow_fn,
            stage="pdf",
            max_attempts=100,
            timeout_sec=0.08,   # 1〜2 attempt 後に timeout
            sleep_fn=lambda _: None,
        )
        assert result.success is False
        assert result.timed_out is True
        assert result.attempts < 100  # 全部は実行しない

    def test_backoff_sleep_called(self):
        """retry 間に sleep が呼ばれる。"""
        sleep_calls = []

        def _mock_sleep(sec):
            sleep_calls.append(sec)

        attempts = [0]

        def _fn():
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("fail")
            return "ok"

        result = retry_with_backoff(
            _fn,
            stage="download",
            max_attempts=3,
            base_delay=0.5,
            sleep_fn=_mock_sleep,
        )
        assert result.success is True
        # 2回 retry → 2回 sleep
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == 0.5   # 0.5 * 2^0
        assert sleep_calls[1] == 1.0   # 0.5 * 2^1


class TestRetryConfig:
    def test_defaults(self):
        cfg = RetryConfig()
        assert cfg.download == 3
        assert cfg.xbrl == 2
        assert cfg.pdf == 1

    def test_max_attempts(self):
        cfg = RetryConfig(download=5)
        assert cfg.max_attempts("download") == 5


class TestTimeoutConfig:
    def test_defaults(self):
        cfg = TimeoutConfig()
        assert cfg.download == 30
        assert cfg.xbrl == 60
        assert cfg.pdf == 120


class TestClassifyReviewHint:
    def test_timeout(self):
        assert classify_review_hint("download", "", True) == "download_timeout"
        assert classify_review_hint("xbrl", "", True) == "xbrl_timeout"
        assert classify_review_hint("pdf", "", True) == "pdf_timeout"

    def test_download_404(self):
        assert classify_review_hint("download", "404 Not Found", False) == "download_not_found"

    def test_download_network(self):
        assert classify_review_hint("download", "ConnectionError", False) == "download_network_error"

    def test_xbrl_missing(self):
        assert classify_review_hint("xbrl", "file not found", False) == "xbrl_missing"

    def test_xbrl_parse(self):
        assert classify_review_hint("xbrl", "XML parse error", False) == "xbrl_parse_failed"

    def test_pdf_table(self):
        assert classify_review_hint("pdf", "no table found", False) == "pdf_table_parse_failed"

    def test_generic(self):
        assert classify_review_hint("other", "something", False) == "other_failed"
