"""Regression tests for security-code identity in legacy repair tooling."""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from tools.repair_alpha_tickers import classify_ticker


def test_numeric_raw_codes_remain_numeric_tickers() -> None:
    assert classify_ticker("41700") == ("normal_5digit", "4170")
    assert classify_ticker("41800") == ("normal_5digit", "4180")
    assert classify_ticker("47200") == ("normal_5digit", "4720")


def test_alphanumeric_raw_codes_preserve_alpha_identity() -> None:
    assert classify_ticker("417A0") == ("alpha_should_convert", "417A")
    assert classify_ticker("418A0") == ("alpha_should_convert", "418A")
    assert classify_ticker("472A0") == ("alpha_should_convert", "472A")
