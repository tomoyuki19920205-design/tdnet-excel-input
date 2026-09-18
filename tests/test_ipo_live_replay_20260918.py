from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Config
from src.events.common_models import EventRecord, EventType
from src.events.tdnet_event_store import build_dedupe_key
from src.fetcher import _fetch_via_jquants, classify_disclosure
from src.ipo_listing_financials import (
    IPO_LISTING_FINANCIALS,
    build_canonical_rows,
    build_ipo_notification_event,
    extract_ipo_listing_financials,
    is_ipo_listing_financial_title,
)
from src.jquants.adapter import _convert_raw_item
from tools.tdnet_ingest import run_ingest


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SOURCE_FIXTURE = FIXTURE_ROOT / "tdnet_20260918_0800.json"
PDF_ROOT = FIXTURE_ROOT / "ipo_listing"
IPO_DOCS = {
    "20260917537694": "140120260917537694",
    "20260916536984": "140120260916536984",
    "20260917537733": "140120260917537733",
    "20260917538177": "140120260917538177",
    "20260915536687": "140120260915536687",
}


def _source_items():
    raw = json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))
    items = [_convert_raw_item(row) for row in raw]
    assert all(item is not None for item in items)
    return items


def _live_items():
    with patch(
        "src.jquants.adapter.fetch_jquants_disclosures",
        return_value=_source_items(),
    ):
        return _fetch_via_jquants("20260918")


def test_fixed_0800_fixture_classifies_five_and_preserves_same_time_documents():
    items = _live_items()
    ipo = [item for item in items if item.disclosure_type == IPO_LISTING_FINANCIALS]

    assert len(items) == 16
    assert {item.source_doc_id for item in ipo} == set(IPO_DOCS)
    assert {item.ticker for item in ipo} == {"620A", "622A", "623A", "624A", "627A"}
    assert all(item.published_at == "2026-09-18 08:00" for item in items)
    assert len({item.source_doc_id for item in items}) == 16

    ohken = [item for item in items if item.ticker == "620A"]
    assert len(ohken) == 4
    assert sum(item.disclosure_type == IPO_LISTING_FINANCIALS for item in ohken) == 1
    assert next(item for item in ipo if item.ticker == "620A").ticker == "620A"

    assert classify_disclosure("東京証券取引所スタンダード市場への上場に伴う当社決算情報等のお知らせ") == IPO_LISTING_FINANCIALS
    assert classify_disclosure("東京証券取引所スタンダードへの上場に伴う当社決算情報等のお知らせ") == IPO_LISTING_FINANCIALS
    assert classify_disclosure("名古屋証券取引所ネクスト市場への上場に伴う当社決算情報等のお知らせ") == IPO_LISTING_FINANCIALS
    assert classify_disclosure("東京証券取引所 TOKYO PRO Market 上場に伴う決算情報等のお知らせ") == IPO_LISTING_FINANCIALS


def test_document_identity_allows_624a_general_and_ipo_cards_to_coexist():
    ipo_item = next(item for item in _live_items() if item.ticker == "624A" and is_ipo_listing_financial_title(item.title))
    ipo_event = build_ipo_notification_event(ipo_item)
    general_event = EventRecord(
        source_doc_id="20260917538161",
        ticker="624A",
        company_name="かがやきHD",
        disclosure_datetime="2026-09-18 08:00",
        title="事業計画及び成長可能性に関する事項",
        doc_url="https://www.release.tdnet.info/inbs/140120260917538161.pdf",
        event_type=EventType.MANAGEMENT_STRATEGY,
        subtype="announced",
    )
    assert build_dedupe_key(ipo_event) != build_dedupe_key(general_event)


def test_620a_cover_without_actual_label_keeps_stated_fy_interim_and_forecast():
    result = extract_ipo_listing_financials(PDF_ROOT / "140120260917537694.pdf")
    periods = {(row.period, row.quarter, row.kind): row for row in result.periods}

    assert set(periods) == {
        ("2025-12-31", "FY", "actual"),
        ("2026-12-31", "2Q", "actual"),
        ("2026-12-31", "FY", "forecast"),
    }
    assert periods[("2025-12-31", "FY", "actual")].metrics == {
        "sales": 6089.0,
        "operating_profit": 1940.0,
        "ordinary_profit": 1944.0,
        "net_income": 1329.0,
        "eps": 288.99,
    }
    interim = periods[("2026-12-31", "2Q", "actual")]
    assert interim.period_start == "2026-01-01"
    assert interim.period_end == "2026-06-30"
    assert interim.metrics["sales"] == 2929.0
    assert interim.metrics["income_taxes"] == 304.0
    assert interim.metrics["eps"] == 151.34
    forecast = periods[("2026-12-31", "FY", "forecast")]
    assert forecast.metrics["sales"] == 6337.0
    assert forecast.metrics["cost_of_sales"] == 1849.0
    assert forecast.metrics["gross_profit"] == 4487.0
    assert forecast.metrics["sga"] == 2274.0


def test_scheduler_live_entrypoint_replay_creates_five_cards_and_is_idempotent(tmp_path):
    items = _live_items()
    config = Config(
        state_db_path=str(tmp_path / "state.db"),
        decision_db_path=str(tmp_path / "decision.db"),
    )
    config.start_date = "20260918"
    config.watch_tickers = ["9999"]  # stale watchlist must not suppress IPO metadata

    cards: dict[str, object] = {}
    canonical: dict[str, dict] = {
        "sentinel-non-ipo": {"ticker": "9999", "value": 1.0},
    }
    stats = {"new_cards": 0, "new_rows": 0, "write_calls": 0}
    queued_reports: dict[str, int] = {}

    general_event = EventRecord(
        source_doc_id="20260917538161",
        ticker="624A",
        company_name="かがやきHD",
        disclosure_datetime="2026-09-18 08:00",
        title="事業計画及び成長可能性に関する事項",
        doc_url="https://www.release.tdnet.info/inbs/140120260917538161.pdf",
        event_type=EventType.MANAGEMENT_STRATEGY,
        subtype="announced",
    )
    cards[build_dedupe_key(general_event)] = general_event
    old_ipo = build_ipo_notification_event(SimpleNamespace(
        ticker="625A",
        company_name="Ｇ－Ｓｋｙｆａｌｌ",
        title="東京証券取引所グロース市場への上場に伴う決算情報等のお知らせ",
        doc_url="https://www.release.tdnet.info/inbs/140120260916537319.pdf",
        published_at="2026-09-17 08:00",
        source_doc_id="20260916537319",
    ))
    old_key = build_dedupe_key(old_ipo)
    cards[old_key] = old_ipo

    def fake_notification(item, *, dry_run=False):
        event = build_ipo_notification_event(item)
        key = build_dedupe_key(event)
        if key in cards:
            action = "skipped"
        else:
            cards[key] = event
            stats["new_cards"] += 1
            action = "inserted"
        return {"action": action, "display_title": event.title, "source_url": event.doc_url}

    def fake_write(extraction, *, ticker, filing_id, disclosed_at, **_kwargs):
        stats["write_calls"] += 1
        rows = build_canonical_rows(
            extraction,
            ticker=ticker,
            filing_id=filing_id,
            disclosed_at=disclosed_at,
        )
        for row in rows:
            if row["source_row_key"] not in canonical:
                stats["new_rows"] += 1
            canonical[row["source_row_key"]] = row
        return {"ok": True, "action": "upserted", "rows": len(rows), "periods": len(extraction.periods)}

    def fake_download(url, _directory, **_kwargs):
        file_id = Path(url).stem
        path = PDF_ROOT / f"{file_id}.pdf"
        assert path.exists()
        return str(path)

    def fake_ensure_pending(item, event_id):
        ticker = item.ticker
        queued_reports[ticker] = queued_reports.get(ticker, 0) + 1
        return {
            "action": "inserted" if queued_reports[ticker] == 1 else "existing",
            "id": f"report-{ticker}",
            "status": "pending",
            "event_id": event_id,
        }

    event_result = SimpleNamespace(
        processed=len(items), detected=0, saved=0, notified=0, errors=[],
        skipped_all_doc_ids=[item.disclosure_id for item in items if item.disclosure_type != IPO_LISTING_FINANCIALS],
    )
    env = {
        "ENABLE_EARNINGS_V2_PIPELINE": "0",
        "PRIOR_COMPARATIVE_REALTIME_ENABLED": "0",
        "JQUANTS_SHADOW_ENABLED": "0",
        "TDNET_PDF_PREFETCH_WORKERS": "1",
    }

    with (
        patch.dict("os.environ", env),
        patch("tools.tdnet_ingest.fetch_new_disclosures", return_value=items),
        patch("tools.tdnet_ingest.download_document", side_effect=fake_download),
        patch("tools.tdnet_ingest.save_ipo_notification", side_effect=fake_notification),
        patch("tools.tdnet_ingest.write_ipo_financials", side_effect=fake_write),
        patch("tools.tdnet_ingest.ensure_pending_report", side_effect=fake_ensure_pending),
        patch("tools.tdnet_ingest._run_jquants_shadow"),
        patch("src.events.event_pipeline.process_documents", return_value=event_result),
        patch("lib.pipeline.financial_reconciliation_runtime.reconcile_ingest_run"),
    ):
        first = run_ingest(config, skip_notify=True)
        first_counts = dict(stats)
        first_keys = set(canonical)
        first_cards = dict(cards)
        second = run_ingest(config, skip_notify=True)

    assert first["total"] == 5
    assert all(row["status"] == "inserted" for row in first["results"])
    assert stats["new_cards"] == first_counts["new_cards"] == 5
    assert first_counts["new_rows"] > 0
    assert stats["new_rows"] == first_counts["new_rows"]
    assert stats["write_calls"] == first_counts["write_calls"] == 5
    assert set(canonical) == first_keys
    assert cards == first_cards
    assert second["total"] == 5
    assert all(row["status"] == "skipped" for row in second["results"])
    assert cards[old_key] is old_ipo
    assert canonical["sentinel-non-ipo"] == {"ticker": "9999", "value": 1.0}
    assert queued_reports == {ticker: 2 for ticker in ("620A", "622A", "623A", "624A", "627A")}

    new_titles = {
        event.title for key, event in cards.items()
        if key not in {build_dedupe_key(general_event), old_key}
    }
    assert new_titles == {
        "新規上場 620A 応研",
        "新規上場 622A テクノクラフト",
        "新規上場 623A ベルテックス",
        "新規上場 624A かがやきHD",
        "新規上場 627A アキッパ",
    }
    assert cards[build_dedupe_key(general_event)] is general_event

    periods = {
        (row["ticker"], row["period"], row["quarter"])
        for key, row in canonical.items() if key != "sentinel-non-ipo"
    }
    assert ("620A", "2026-12-31", "2Q") in periods
    assert ("620A", "2025-12-31", "FY") in periods
    assert ("620A", "2026-12-31", "FY") in periods
    assert all(any(ticker == wanted for ticker, *_ in periods) for wanted in {"620A", "622A", "623A", "624A", "627A"})
