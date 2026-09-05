import json
import sqlite3
from pathlib import Path

import pytest

from lib.news_monitor import (NewsValidationError, connect_news_db, make_dedupe_key,
                              publication_display_value, upsert_run, validate_payload)
from tools.ingest_company_news import ingest_file

FIXTURE = Path(__file__).parent / "fixtures" / "company_news_v1.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_validation_and_temporal_fields():
    run = validate_payload(payload())
    assert run.events[0]["temporal_status"] == "current"
    assert run.events[0]["source_url"] == "https://example.com/news/1"


@pytest.mark.parametrize(("field", "value"), [("ticker", "javascript"), ("schema_version", "v0")])
def test_invalid_run_fields(field, value):
    data = payload(); data[field] = value
    with pytest.raises(NewsValidationError): validate_payload(data)


@pytest.mark.parametrize(("field", "value"), [("source_url", "javascript:alert(1)"), ("direction", "bullish"), ("importance", "urgent"), ("category", "rumor")])
def test_invalid_item_fields(field, value):
    data = payload(); data["items"][0][field] = value
    with pytest.raises(NewsValidationError): validate_payload(data)


def test_full_article_payload_is_rejected():
    data = payload(); data["items"][0]["article_body"] = "full article"
    with pytest.raises(NewsValidationError):
        validate_payload(data)


def test_empty_items_and_idempotent_run(tmp_path):
    data = payload(); data["items"] = []
    run = validate_payload(data)
    conn = connect_news_db(tmp_path / "news.db")
    upsert_run(conn, run); upsert_run(conn, run)
    assert conn.execute("select count(*) from canonical_news_events").fetchone()[0] == 0
    assert conn.execute("select count(*) from canonical_news_scan_runs").fetchone()[0] == 1


def test_exact_dedupe_and_upsert(tmp_path):
    run = validate_payload(payload())
    conn = connect_news_db(tmp_path / "news.db")
    upsert_run(conn, run); upsert_run(conn, run)
    assert conn.execute("select count(*) from canonical_news_events").fetchone()[0] == 1
    assert conn.execute("select count(*) from canonical_news_scan_runs").fetchone()[0] == 1


def test_quarantine_is_atomic(tmp_path):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    source = inbox / "bad.json"; data = payload(); data["items"].append({"headline": "bad"})
    source.write_text(json.dumps(data), encoding="utf-8")
    assert not ingest_file(source, tmp_path / "news.db", inbox / "processed", inbox / "quarantine")
    conn = sqlite3.connect(tmp_path / "news.db")
    assert conn.execute("select count(*) from canonical_news_events").fetchone()[0] == 0
    assert (inbox / "quarantine" / "bad.json").exists()


def test_precision_aware_payloads_and_reader_compatibility(tmp_path):
    data = payload()
    exact = data["items"][0]["published_at"]
    legacy_key = make_dedupe_key("7203", data["items"][0]["source_url"], exact,
                                 data["items"][0]["headline"])
    data["items"][0].update(published_at=None, published_date="2026-08-29", published_precision="date")
    event = validate_payload(data).events[0]
    assert event["published_at"] is None and event["published_date"] == "2026-08-29"
    assert event["published_precision"] == "date"
    assert publication_display_value(event) == "2026-08-29"
    assert event["dedupe_key"] != legacy_key
    conn = connect_news_db(tmp_path / "precision.db")
    upsert_run(conn, validate_payload(data))
    row = conn.execute("SELECT published_at,published_date,published_precision FROM canonical_news_events").fetchone()
    assert tuple(row) == (None, "2026-08-29", "date")


def test_legacy_exact_payload_keeps_existing_dedupe_and_display():
    data = payload()
    raw = data["items"][0]
    event = validate_payload(data).events[0]
    assert event["published_precision"] == "datetime" and event["published_date"] is None
    assert event["dedupe_key"] == make_dedupe_key("7203", raw["source_url"], event["published_at"], raw["headline"])
    assert publication_display_value(event) == event["published_at"]


def test_unknown_publication_is_valid_and_deterministically_deduped():
    data = payload()
    data["items"][0].update(published_at=None, published_date=None, published_precision="unknown")
    first = validate_payload(data).events[0]
    second = validate_payload(data).events[0]
    assert first["published_at"] is None and publication_display_value(first, unknown="日付不明") == "日付不明"
    assert first["dedupe_key"] == second["dedupe_key"]


@pytest.mark.parametrize("fields", [
    {"published_at": "2026-08-29T09:00:00+09:00", "published_date": "2026-08-29", "published_precision": "date"},
    {"published_at": None, "published_date": None, "published_precision": "datetime"},
    {"published_at": None, "published_date": "2026-08-29", "published_precision": "unknown"},
])
def test_publication_cross_field_contradictions_fail_closed(fields):
    data = payload(); data["items"][0].update(fields)
    with pytest.raises(NewsValidationError):
        validate_payload(data)
