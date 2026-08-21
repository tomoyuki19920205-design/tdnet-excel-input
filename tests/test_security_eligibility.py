from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from src.events.common_models import EventRecord, EventType
from src.events.notify_rules import should_notify_event
from src.events.tdnet_event_store import save_event_to_supabase
from src.models import DisclosureItem
from src.security_eligibility import (
    classify_disclosure_security,
    classify_security_eligibility,
)


TARGET_ETFS = (
    "1482", "1496", "1497", "1656", "2012", "2255", "2256",
    "2257", "2258", "2259", "236A", "237A", "238A",
)


@pytest.mark.parametrize("product_category", ["014", "023"])
def test_etf_and_etn_product_categories_are_excluded(product_category: str):
    decision = classify_security_eligibility(
        "ZZZZ", product_category=product_category,
        title="銘柄属性に依存するため商品名キーワードなし",
    )
    assert decision.is_etf_like
    assert decision.authoritative
    assert decision.source == "item_product_category"


@pytest.mark.parametrize("product_category", ["011", "012", "013", "021"])
def test_non_etf_official_categories_are_not_excluded(product_category: str):
    decision = classify_security_eligibility(
        "ZZZZ", product_category=product_category,
        title="ETFという文字があっても正式属性を優先",
    )
    assert not decision.is_etf_like
    assert decision.authoritative


def test_tdnet_public_item_classification_excludes_etf_and_etn():
    etf = classify_security_eligibility("ZZZZ", tdnet_public_items=["36507"])
    etn = classify_security_eligibility("ZZZY", tdnet_public_items=["37507"])
    assert etf.is_etf_like and etf.matched_public_item == "36507"
    assert etn.is_etf_like and etn.matched_public_item == "37507"


def test_individual_reit_is_preserved_but_reit_etf_is_excluded():
    individual_reit = classify_security_eligibility(
        "8951", product_category="013", company_name="日本ビルファンド投資法人"
    )
    reit_etf = classify_security_eligibility(
        "ZZZZ", product_category="014", company_name="REIT指数連動商品"
    )
    assert not individual_reit.is_etf_like
    assert reit_etf.is_etf_like


def test_alphanumeric_ordinary_stock_is_preserved():
    decision = classify_security_eligibility("285A", product_category="011")
    assert not decision.is_etf_like


def test_legacy_text_fallback_only_when_master_is_unavailable():
    missing_db = Path("tests/.missing-security-master.db")
    fallback = classify_security_eligibility(
        "ZZZZ", master_db_path=missing_db,
        title="上場投資信託 約款変更のお知らせ",
    )
    assert fallback.is_etf_like
    assert not fallback.authoritative
    assert fallback.source == "legacy_text_fallback"


@pytest.mark.parametrize("ticker", TARGET_ETFS)
def test_20260821_problem_tickers_are_etfs_in_authoritative_local_master(ticker: str):
    decision = classify_security_eligibility(ticker, as_of_date="2026-08-21")
    assert decision.is_etf_like
    assert decision.authoritative
    assert decision.source == "jquants_equities_master"
    assert decision.product_category == "014"
    assert decision.master_date == "2026-08-20"


@pytest.mark.parametrize(
    "title",
    [
        "2026年7月期 決算短信",
        "2026年7月期 決算短信の訂正について",
        "分配金支払のお知らせ",
        "信託約款変更のお知らせ",
        "運用報告書掲載のお知らせ",
    ],
)
def test_all_document_types_for_etf_are_security_excluded_before_ingest(
    title: str, monkeypatch: pytest.MonkeyPatch,
):
    from tools import tdnet_ingest

    item = DisclosureItem(
        disclosure_id="etf-doc", ticker="1482", company_name="issuer",
        title=title, doc_url="https://example.invalid/etf.pdf",
        published_at="2026-08-21 11:00",
    )

    def forbidden_download(*args, **kwargs):
        raise AssertionError("ETF PDF/XBRL download must not run")

    monkeypatch.setattr(tdnet_ingest, "download_document", forbidden_download)
    result = tdnet_ingest._process_single(
        item,
        config=object(),
        state_db=object(),
        decision_db=object(),
        run_id="test",
        dry_run=True,
    )
    assert result["status"] == "skipped"
    assert result["detail"] == "etf_like_security"


def test_notification_and_viewer_persistence_defense_for_etf(monkeypatch: pytest.MonkeyPatch):
    event = EventRecord(
        ticker="1482",
        company_name="issuer",
        disclosure_datetime="2026-08-21 11:00",
        title="2026年7月期 決算短信",
        event_type=EventType.FORECAST_REVISION,
        subtype="upward",
    )

    assert not should_notify_event(event)

    # The master guard runs before Supabase client creation, so no DB write is
    # possible even when this function is invoked directly.
    monkeypatch.setattr(
        "src.events.tdnet_event_store._get_supabase",
        lambda: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    result = save_event_to_supabase(event)
    assert result["action"] == "security_excluded"
    assert result["reason"] == "etf_like_security"


def test_direct_earnings_pipeline_drops_etf_before_parser_or_events(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.events import earnings_production_pipeline as pipeline

    item = DisclosureItem(
        disclosure_id="etf-earnings", ticker="1482", company_name="issuer",
        title="2026年7月期 決算短信",
        doc_url="https://example.invalid/etf.pdf",
        published_at="2026-08-21 11:00",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("ETF parser/downloader must not run")

    monkeypatch.setattr(pipeline, "extract_earnings_data", forbidden)
    with sqlite3.connect(":memory:") as conn:
        result = pipeline.run_earnings_production(
            [item], conn, dry_run=True, notify_enabled=False
        )

    assert result.total_disclosures == 1
    assert result.tanshin_count == 0
    assert result.saved_count == 0
    assert result.notified_count == 0


def test_ordinary_stock_event_remains_notification_eligible():
    event = EventRecord(
        ticker="7203",
        company_name="トヨタ自動車",
        disclosure_datetime="2026-08-21 11:00",
        title="業績予想の上方修正",
        event_type=EventType.FORECAST_REVISION,
        subtype="upward",
    )
    assert should_notify_event(event)
