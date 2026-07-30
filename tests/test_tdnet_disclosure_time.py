from __future__ import annotations

from unittest.mock import patch

from src.events.common_models import EventRecord
from src.events import tdnet_event_store
from src.tdnet_disclosure_time import (
    canonicalize_tdnet_url,
    fetch_official_listing_times,
    is_date_only,
    resolve_official_disclosure_datetime,
)


class _Response:
    status_code = 200
    url = "https://www.release.tdnet.info/inbs/I_list_001_20260730.html"
    text = """
    <table><tr><td>15:30</td><td>21270</td><td>日本M&A</td>
    <td><a href='140120260729502118.pdf'>決算短信</a></td></tr></table>
    """

    def raise_for_status(self):
        return None


def test_resolver_uses_official_tdnet_listing_time():
    actual = resolve_official_disclosure_datetime(
        "2026-07-30",
        "https://www.release.tdnet.info/inbs/140120260729502118.pdf",
        get=lambda *args, **kwargs: _Response(),
    )
    assert actual == "2026-07-30T15:30:00+09:00"


def test_listing_fetch_returns_url_to_time_map():
    mapping = fetch_official_listing_times(
        "2026-07-30", get=lambda *args, **kwargs: _Response(), max_pages=1
    )
    assert mapping["https://www.release.tdnet.info/inbs/140120260729502118.pdf"] == "2026-07-30T15:30:00+09:00"


def test_date_only_is_not_accepted_as_timestamp():
    assert is_date_only("2026-07-30")
    assert not is_date_only("2026-07-30 15:30")
    assert canonicalize_tdnet_url("https://x.test/a.pdf?token=x") == "https://x.test/a.pdf"
    assert tdnet_event_store._sanitize_disclosed_at("2026-07-30") is None


def test_store_resolves_date_only_before_building_row(monkeypatch):
    event = EventRecord(
        ticker="2127", company_name="日本M&A", event_type="earnings", subtype="1Q",
        title="決算短信", disclosure_datetime="2026-07-30",
        doc_url="https://www.release.tdnet.info/inbs/140120260729502118.pdf",
    )
    monkeypatch.setattr(
        tdnet_event_store, "resolve_official_disclosure_datetime",
        lambda *_args, **_kwargs: "2026-07-30T15:30:00+09:00",
    )
    monkeypatch.setattr(tdnet_event_store, "_get_supabase", lambda: None)
    result = tdnet_event_store.save_event_to_supabase(event)
    assert result["error"] == "supabase_not_available"
    assert event.disclosure_datetime == "2026-07-30T15:30:00+09:00"


def test_store_refuses_unresolved_date_only_time(monkeypatch):
    event = EventRecord(
        ticker="2127", event_type="earnings", disclosure_datetime="2026-07-30",
        doc_url="https://www.release.tdnet.info/inbs/missing.pdf",
    )
    monkeypatch.setattr(tdnet_event_store, "resolve_official_disclosure_datetime", lambda *_a, **_kw: None)
    assert tdnet_event_store.save_event_to_supabase(event)["error"] == "official_tdnet_disclosure_time_not_found"
