from pathlib import Path

PROMPT = (Path(__file__).resolve().parents[1] / "config/ny_market_daily_scheduled_prompt.txt").read_text(encoding="utf-8")

def test_dedicated_runtime_explicitly_drains_ny_inbox_to_production():
    assert '--inbox "C:/Users/takuy/OneDrive/tdnet-ny-market-runtime/data/ny_market_inbox"' in PROMPT
    assert 'python tools/company_news_inbox_worker.py --once --root "C:/Users/takuy/OneDrive/tdnet-excel-input"' in PROMPT
    assert '--db "C:/Users/takuy/OneDrive/tdnet-excel-input/decision_db.db"' in PROMPT

def test_scheduled_delivery_requires_strict_api_readback_and_single_jst_date():
    assert "api_latest_news_stream" in PROMPT
    assert "raise_for_status" in PROMPT
    assert "Asia/Tokyo" in PROMPT
    assert "report/run/API各1件" in PROMPT
    assert "HTTP失敗・0件・重複・本文hash不一致" in PROMPT
