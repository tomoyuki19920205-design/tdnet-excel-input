import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib.ny_market import NYMarketValidationError, validate_payload
from tests.ny_market_quality_fixture import payload as full_payload
from tools.build_ny_market_provisional_payload import build
from tools import build_ny_market_market_data_packet as packet_builder
from tools.ny_market_inbox_worker import WorkerPaths, run_once


ROOT = Path(__file__).resolve().parent


def test_real_20261028_verified_sections_stay_explicitly_provisional():
    packet = json.loads((ROOT / "fixtures/ny_market_20261028_verified_sections.json").read_text(encoding="utf-8"))
    interim = build(packet, date(2026, 10, 28), "open")
    assert interim["quality_status"] == "provisional"
    assert interim["top_gainers_20"] == []
    assert "純粋上昇率ランキングは確定していません" in interim["report_markdown"]
    assert "NUWEの株式併合後の価格基準を確認中" in interim["report_markdown"]
    assert len(interim["index_moves"]) == 5 and len(interim["sector_moves"]) == 11
    assert validate_payload(interim).report["quality_status"] == "provisional"
    forged = {**interim, "quality_status": "complete"}
    with pytest.raises(NYMarketValidationError):
        validate_payload(forged)
    altered = json.loads(json.dumps(interim))
    altered["canonical_market_data"]["indexes"][0]["change_pct"] = 0
    with pytest.raises(NYMarketValidationError, match="hash mismatch"):
        validate_payload(altered)


def _synthetic_packet() -> dict:
    stamp = "2026-09-02T07:10:00+09:00"
    row = {"symbol": "SOX", "session_date": "2026-09-01", "previous_session_date": "2026-08-31",
           "previous_close": 100.0, "close": 101.0, "change_pct": 1.0,
           "price_basis": "regular_close", "adjusted": False,
           "provider": "fixture", "source_identifier": "https://example.org/sox",
           "raw_response_sha256": "a" * 64}
    return {"price_basis": "regular_close", "adjusted": False, "market_session_date": "2026-09-01",
            "source": {"name": "fixture", "url": "https://example.org/market", "retrieved_at": stamp},
            "indexes": [row], "sectors": [], "market_data_contract_version": "ny_market_data_provisional_v1",
            "market_data_generated_at": stamp, "providers": ["fixture"],
            "raw_response_hashes": ["a" * 64], "discrepancy_count": 0,
            "top_gainers_20": [], "unresolved_items": [{"field": "Top20", "reason": "share basis unresolved"}]}


def test_builder_emits_provisional_packet_and_keeps_verified_checkpoint(tmp_path, monkeypatch, capsys):
    snapshot = _synthetic_packet()
    snapshot.pop("excluded_corporate_action_count", None)
    calls = {"snapshot": 0, "top20": 0}

    def fetch_snapshot(*_):
        calls["snapshot"] += 1
        return snapshot

    def fail_top20(*_, **__):
        calls["top20"] += 1
        raise packet_builder.MarketDataError("NUWE price basis unresolved")

    monkeypatch.setattr(packet_builder, "build_index_sector_snapshot", fetch_snapshot)
    monkeypatch.setattr(packet_builder, "build_canonical_market_data_packet", fail_top20)
    checkpoint = tmp_path / "session-checkpoint.json"
    for number in (1, 2, 3, 4):
        output = tmp_path / f"packet-{number}.json"
        monkeypatch.setattr("sys.argv", ["packet_builder", "--market-session-date", "2026-09-01",
                                        "--output", str(output), "--checkpoint", str(checkpoint),
                                        "--allow-provisional"])
        assert packet_builder.main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "provisional"
        assert result["excluded_corporate_action_count"] == 0
        assert json.loads(output.read_text(encoding="utf-8"))["ranking_status"] == "unconfirmed"
    assert calls == {"snapshot": 1, "top20": 3}
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert state["stage"] == "provisional" and state["last_error"] == "NUWE price basis unresolved"
    assert state["data_failure_count"] == 3
    notice = tmp_path / "new-notice.json"
    notice.write_text(json.dumps({"NUWE": "https://www.nasdaqtrader.com/TraderNews.aspx?id=eca2026-764"}))
    monkeypatch.setattr("sys.argv", ["packet_builder", "--market-session-date", "2026-09-01",
                                    "--output", str(tmp_path / "packet-5.json"), "--checkpoint", str(checkpoint),
                                    "--corporate-action-notices", str(notice), "--allow-provisional"])
    assert packet_builder.main() == 0
    assert calls["top20"] == 4
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["data_failure_count"] == 1


def test_provisional_upgrades_one_row_to_complete_and_duplicate_is_skipped(tmp_path):
    inbox = tmp_path / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    interim = build(_synthetic_packet(), date(2026, 9, 2), "open")
    (inbox / "ny_market_daily_20260902_interim.json").write_text(json.dumps(interim), encoding="utf-8")
    paths = WorkerPaths.from_values(tmp_path, tmp_path / "db.sqlite")
    assert run_once(paths, ny_sync_func=lambda *_: {})["status"] == "completed"
    conn = sqlite3.connect(paths.db)
    assert conn.execute("SELECT quality_status FROM canonical_ny_market_reports").fetchone()[0] == "provisional"
    assert conn.execute("SELECT status FROM canonical_ny_market_report_runs").fetchone()[0] == "provisional"
    conn.close()
    complete = full_payload()
    complete["generated_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    (inbox / "ny_market_daily_20260902_complete.json").write_text(json.dumps(complete, ensure_ascii=False), encoding="utf-8")
    assert run_once(paths, ny_sync_func=lambda *_: {})["status"] == "completed"
    conn = sqlite3.connect(paths.db)
    assert conn.execute("SELECT count(*), quality_status FROM canonical_ny_market_reports").fetchone() == (1, "complete")
    assert conn.execute("SELECT status,attempt FROM canonical_ny_market_report_runs").fetchone() == ("success", 2)
    conn.close()
    (inbox / "ny_market_daily_20260902_duplicate.json").write_text(json.dumps(complete, ensure_ascii=False), encoding="utf-8")
    outcome = run_once(paths, ny_sync_func=lambda *_: (_ for _ in ()).throw(AssertionError("duplicate sync")))
    assert outcome["results"][0]["status"] == "already_completed"
    conn = sqlite3.connect(paths.db)
    assert conn.execute("SELECT attempt FROM canonical_ny_market_report_runs").fetchone()[0] == 2
    conn.close()


def test_sync_failure_resumes_from_checkpoint_without_reingestion(tmp_path):
    inbox = tmp_path / "data" / "news_inbox"
    inbox.mkdir(parents=True)
    interim = build(_synthetic_packet(), date(2026, 9, 2), "open")
    (inbox / "ny_market_daily_20260902_interim.json").write_text(json.dumps(interim), encoding="utf-8")
    paths = WorkerPaths.from_values(tmp_path, tmp_path / "db.sqlite")
    first = run_once(paths, ny_sync_func=lambda *_: (_ for _ in ()).throw(RuntimeError("temporary 503")))
    assert first["status"] == "completed_with_errors"
    assert json.loads((paths.work_dir / "checkpoint.json").read_text())["pending"][interim["stable_key"]]["sync_attempts"] == 1
    second = run_once(paths, ny_sync_func=lambda *_: {})
    assert second["detected"] == 0 and second["completed"] == 1
    conn = sqlite3.connect(paths.db)
    assert conn.execute("SELECT status,attempt FROM canonical_ny_market_report_runs").fetchone() == ("provisional", 1)
    conn.close()
