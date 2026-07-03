"""tests/jquants/test_shadow_integration.py - Shadow Run integration tests

Target: tools/tdnet_ingest._run_jquants_shadow()

NOTE: _run_jquants_shadow() uses lazy import inside try block,
      so patch target is "src.jquants.shadow_runner.run_shadow_comparison".
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.tdnet_ingest import _run_jquants_shadow
from src.models import DisclosureItem, DisclosureType
from src.jquants.shadow_runner import ShadowDiffResult

_PATCH_TARGET = "src.jquants.shadow_runner.run_shadow_comparison"


def _make_item(ticker="7388"):
    return DisclosureItem(
        disclosure_id="sha256abc",
        ticker=ticker,
        company_name="Test Co",
        title="Test Title",
        doc_url="https://www.release.tdnet.info/inbs/140120260630584087.pdf",
        published_at="2026-06-30 15:30",
        xbrl_url=None,
        disclosure_type=DisclosureType.FINANCIAL_STATEMENT,
    )


def _make_result(jq_total=100, legacy_total=50, matched=45, missing=5):
    r = ShadowDiffResult(date_str="20260630")
    r.jquants_total = jq_total
    r.jquants_filtered = 10
    r.legacy_total = legacy_total
    r.matched_count = matched
    r.missing_in_legacy = [f"1401202606305840{i:02d}" for i in range(missing)]
    return r


class TestShadowDisabledByDefault:

    def test_not_called_when_env_unset(self):
        called = []
        env = {k: v for k, v in os.environ.items() if k != "JQUANTS_SHADOW_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            with patch(_PATCH_TARGET, side_effect=lambda *a, **kw: called.append(True)):
                _run_jquants_shadow([_make_item()])
        assert called == []

    def test_not_called_when_env_zero(self):
        called = []
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "0"}):
            with patch(_PATCH_TARGET, side_effect=lambda *a, **kw: called.append(True)):
                _run_jquants_shadow([_make_item()])
        assert called == []

    def test_no_network_call_when_disabled(self):
        import requests as _req
        env = {k: v for k, v in os.environ.items() if k != "JQUANTS_SHADOW_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(_req.Session, "get") as mock_get:
                _run_jquants_shadow([])
                mock_get.assert_not_called()


class TestShadowEnabledByEnv:

    def test_called_when_enabled(self):
        called = []

        def _mock(date_str, *, legacy_items=None, **kw):
            called.append(date_str)
            return _make_result()

        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=_mock):
                _run_jquants_shadow([_make_item()], date_str="20260630")

        assert len(called) == 1

    def test_legacy_items_passed_correctly(self):
        received = []

        def _mock(date_str, *, legacy_items=None, **kw):
            received.extend(legacy_items or [])
            return _make_result()

        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=_mock):
                _run_jquants_shadow([_make_item("7388"), _make_item("9999")], date_str="20260630")

        assert len(received) == 2
        assert received[0].ticker == "7388"
        assert received[1].ticker == "9999"

    def test_date_str_yyyymmdd_passed(self):
        received = []

        def _mock(date_str, *, legacy_items=None, **kw):
            received.append(date_str)
            return _make_result()

        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=_mock):
                _run_jquants_shadow([], date_str="20260630")

        assert received == ["20260630"]

    def test_date_str_yyyy_mm_dd_converted(self):
        received = []

        def _mock(date_str, *, legacy_items=None, **kw):
            received.append(date_str)
            return _make_result()

        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=_mock):
                _run_jquants_shadow([], date_str="2026-06-30")

        assert received == ["20260630"]

    def test_date_str_none_uses_today(self):
        received = []

        def _mock(date_str, *, legacy_items=None, **kw):
            received.append(date_str)
            return _make_result()

        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=_mock):
                _run_jquants_shadow([], date_str=None)

        assert len(received) == 1
        assert len(received[0]) == 8


class TestExceptionDoesNotPropagate:

    def test_runtime_error_not_raised(self):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=RuntimeError("timeout")):
                _run_jquants_shadow([_make_item()], date_str="20260630")

    def test_connection_error_not_raised(self):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=ConnectionError("unreachable")):
                _run_jquants_shadow([], date_str="20260630")

    def test_import_error_not_raised(self):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1"}):
            with patch(_PATCH_TARGET, side_effect=ImportError("cannot import")):
                try:
                    _run_jquants_shadow([], date_str="20260630")
                except Exception as e:
                    pytest.fail(f"Exception propagated: {e}")

    def test_api_key_missing_not_raised(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JQUANTS_SHADOW_ENABLED", "JQUANTS_API_KEY")}
        env["JQUANTS_SHADOW_ENABLED"] = "1"
        with patch.dict(os.environ, env, clear=True):
            try:
                _run_jquants_shadow([], date_str="20260630")
            except Exception as e:
                pytest.fail(f"Exception propagated: {e}")

    def test_value_error_not_raised(self):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=ValueError("bad value")):
                _run_jquants_shadow([], date_str="20260630")


class TestLogTags:

    def test_trigger_log_tag_present(self, caplog):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, return_value=_make_result()):
                with caplog.at_level(logging.INFO, logger="jquants.shadow"):
                    _run_jquants_shadow([_make_item()], date_str="20260630")
        assert "[JQUANTS_SHADOW_TRIGGER]" in "\n".join(caplog.messages)

    def test_summary_log_tag_present(self, caplog):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, return_value=_make_result()):
                with caplog.at_level(logging.INFO, logger="jquants.shadow"):
                    _run_jquants_shadow([_make_item()], date_str="20260630")
        assert "[JQUANTS_SHADOW_SUMMARY]" in "\n".join(caplog.messages)

    def test_error_log_tag_on_exception(self, caplog):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, side_effect=RuntimeError("mock error")):
                with caplog.at_level(logging.ERROR, logger="jquants.shadow"):
                    _run_jquants_shadow([], date_str="20260630")
        assert "[JQUANTS_SHADOW_ERROR]" in "\n".join(caplog.messages)

    def test_summary_contains_truncation_gap(self, caplog):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, return_value=_make_result(jq_total=150, legacy_total=50)):
                with caplog.at_level(logging.INFO, logger="jquants.shadow"):
                    _run_jquants_shadow([_make_item()], date_str="20260630")
        logs = [m for m in caplog.messages if "[JQUANTS_SHADOW_SUMMARY]" in m]
        assert logs
        assert "truncation_gap" in logs[0]

    def test_trigger_contains_legacy_count(self, caplog):
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": "dummy"}):
            with patch(_PATCH_TARGET, return_value=_make_result()):
                with caplog.at_level(logging.INFO, logger="jquants.shadow"):
                    _run_jquants_shadow([_make_item(), _make_item("9999")], date_str="20260630")
        logs = [m for m in caplog.messages if "[JQUANTS_SHADOW_TRIGGER]" in m]
        assert logs
        assert "legacy_count=2" in logs[0]


class TestApiKeyNotLogged:

    def test_api_key_not_in_logs(self, caplog):
        dummy_key = "MY_SUPER_SECRET_JQUANTS_KEY_DO_NOT_LOG"
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": dummy_key}):
            with patch(_PATCH_TARGET, return_value=_make_result()):
                with caplog.at_level(logging.DEBUG):
                    _run_jquants_shadow([_make_item()], date_str="20260630")
        assert dummy_key not in "\n".join(caplog.messages)

    def test_api_key_not_in_error_logs(self, caplog):
        dummy_key = "ANOTHER_SECRET_KEY_NEVER_LOG_XYZ"
        with patch.dict(os.environ, {"JQUANTS_SHADOW_ENABLED": "1", "JQUANTS_API_KEY": dummy_key}):
            with patch(_PATCH_TARGET, side_effect=RuntimeError("some error")):
                with caplog.at_level(logging.DEBUG):
                    _run_jquants_shadow([], date_str="20260630")
        assert dummy_key not in "\n".join(caplog.messages)
