import json
import sqlite3
from pathlib import Path

import pytest

from lib.news_monitor import NewsValidationError, connect_news_db, make_dedupe_key, upsert_run, validate_payload
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
