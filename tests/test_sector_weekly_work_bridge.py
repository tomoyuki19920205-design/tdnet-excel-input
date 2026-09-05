import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.sector_weekly import CANONICAL_SQLITE_SCHEMA, SectorValidationError, connect_sector_db, weekly_window
from lib.sector_weekly_work import (
    claim_next,
    enqueue_assignment,
    enqueue_retry_candidate,
    get_assignment,
    payload_hash,
    recover_expired_leases,
)
from tools.sector_weekly_work_bridge import (
    RESULT_SCHEMA,
    SectorBridgeError,
    _normalized_report_row,
    abandon_one,
    claim_one,
    fail_one,
    heartbeat_one,
    stage_one,
    start_one,
    verify_claim_one,
    verify_payload_one,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration

AT = datetime.fromisoformat("2026-09-05T06:05:00+09:00")
OWNER = "sector-weekly-worker"


def _migrate_fixture(db: Path) -> None:
    if db.exists():
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.close()
    apply_sqlite_migration(
        db, expected_db_path=db, backup_dir=db.parent / "migration_backups",
    )


def _report() -> dict:
    material = lambda number: (
        f"### 材料{number}：海外需給{number}\n\n"
        "**確認できた事実**\n\n需給変化。\n\n"
        "**日本企業への波及**\n\n日本企業へ波及。\n\n"
        "**利益への影響**\n\n利益感応度を試算。\n\n"
        "**株価への織り込み**\n\n未織り込み。\n\n"
        "**反対材料・注意点**\n\n需給反転。" +
        ("\n\n**試算**\n\n10〜20億円。\n\n**仮説**\n\n変化は継続。" if number == 1 else "")
    )
    return {
        "importance": "A",
        "direction": "mixed",
        "summary_bullets": ["海外需給が変化", "国内利益へ波及", "反証条件を監視"],
        "watchlist_companies": [{"code": "5713", "name": "住友金属鉱山", "direction": "mixed"}],
        "next_week_watchpoints": ["取引所在庫と現物プレミアム"],
        "missed_candidates": ["マイナー金属群は確認したが重要変動なし"],
        "full_report_md": "# 【東証33業種週次】鉱業\n\n## 今週の要旨\n要旨。\n\n" + "\n\n\n\n".join(material(i) for i in range(1, 4)),
        "sources": [{
            "title": "Primary market data", "url": "https://example.com/market",
            "source_name": "Exchange", "source_type": "market_data", "published_at": "2026-09-04",
        }],
    }


def _enqueue(db: Path, code: int = 2) -> dict:
    _migrate_fixture(db)
    conn = connect_sector_db(db)
    try:
        row, _ = enqueue_assignment(conn, code, weekly_window(AT), now=AT)
        return row
    finally:
        conn.close()


def _envelope(assignment: dict, report: dict | None = None) -> dict:
    return {
        "schema_version": RESULT_SCHEMA,
        "assignment_id": assignment["assignment_id"],
        "claim_owner": OWNER,
        "sector_code": assignment["sector_code"],
        "sector_name": assignment["sector_name"],
        "period_start": assignment["period_start"],
        "period_end": assignment["period_end"],
        "attempt_count": assignment["attempt_count"],
        "contract_hash": assignment["contract_hash"],
        "report": report or _report(),
    }


def _semantic_row(published_at: str | None = "2026-08-26T00:00:00Z") -> dict:
    return {
        "schema_version": "sector_weekly_v1", "report_type": "sector_weekly",
        "sector_code": 9, "sector_name": "石油・石炭製品",
        "period_start": "2026-08-21T21:00:00Z",
        "period_end": "2026-08-28T20:59:59Z",
        "generated_at": "2026-08-29T01:00:00Z",
        "importance": "A", "direction": "mixed", "summary_bullets": ["要旨"],
        "full_report_md": "本文Z +00:00は日時以外なので不変",
        "watchlist_companies": [], "next_week_watchpoints": [], "missed_candidates": [],
        "sources": [{
            "title": "Primary", "url": "https://example.com/source",
            "source_name": "Authority", "source_type": "government",
            "published_at": published_at,
        }],
        "run_id": "sector_weekly:2026-08-29:09",
        "dedupe_key": "sector_weekly:2026-08-29:09",
    }


@pytest.mark.parametrize(
    "equivalent",
    [
        "2026-08-26T00:00:00+00:00",
        "2026-08-26T09:00:00+09:00",
        "2026-08-26T00:00:00.000000+00:00",
    ],
)
def test_canonical_comparison_normalizes_equivalent_rfc3339_instants(equivalent: str):
    assert _normalized_report_row(_semantic_row()) == _normalized_report_row(_semantic_row(equivalent))


def test_canonical_comparison_normalizes_only_allowlisted_top_level_datetimes():
    left = _semantic_row()
    right = copy.deepcopy(left)
    right["period_start"] = "2026-08-22T06:00:00+09:00"
    right["period_end"] = "2026-08-29T05:59:59+09:00"
    right["generated_at"] = "2026-08-29T10:00:00+09:00"
    assert _normalized_report_row(left) == _normalized_report_row(right)
    right["generated_at"] = "2026-08-29T10:00:01+09:00"
    assert _normalized_report_row(left) != _normalized_report_row(right)


@pytest.mark.parametrize(
    "invalid_or_different",
    [
        "2026-08-26T00:00:01Z",
        "2026-08-26T00:00:00",
        "2026/08/26 00:00:00",
        "2026-08-26T00:00:00.0000001Z",
        "not-a-time",
    ],
)
def test_canonical_comparison_rejects_different_or_invalid_source_times(invalid_or_different: str):
    if invalid_or_different == "2026-08-26T00:00:01Z":
        assert _normalized_report_row(_semantic_row()) != _normalized_report_row(
            _semantic_row(invalid_or_different)
        )
    else:
        with pytest.raises(SectorBridgeError, match="RFC3339"):
            _normalized_report_row(_semantic_row(invalid_or_different))


@pytest.mark.parametrize("field", ["publisher", "title", "url"])
def test_canonical_comparison_does_not_hide_non_datetime_source_differences(field: str):
    left = _semantic_row()
    right = copy.deepcopy(left)
    key = "source_name" if field == "publisher" else field
    right["sources"][0][key] += " changed"
    assert _normalized_report_row(left) != _normalized_report_row(right)


def test_canonical_comparison_preserves_source_order_and_does_not_mutate_or_rehash_input():
    left = _semantic_row()
    left["sources"].append({
        "title": "Second", "url": "https://example.com/second", "source_name": "Exchange",
        "source_type": "market_data", "published_at": "2026-08-27",
    })
    original = copy.deepcopy(left)
    stored_hash = payload_hash(left)
    normalized = _normalized_report_row(left)
    assert left == original and payload_hash(left) == stored_hash
    assert [item["title"] for item in normalized["sources"]] == ["Primary", "Second"]
    reordered = copy.deepcopy(left)
    reordered["sources"].reverse()
    assert _normalized_report_row(left) != _normalized_report_row(reordered)
    assert normalized["full_report_md"] == "本文Z +00:00は日時以外なので不変"


def test_sector9_shape_with_18_sources_and_10_timestamp_spellings_is_semantically_equal():
    fixture = Path(__file__).parent / "fixtures" / "sector_weekly_sector9_processed_hash.json"
    envelope = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload_hash(envelope) == "02667677dbe930ecdb82243aba555185bd245b2910b7516b6315a7fb5c566c61"
    processed = {
        "schema_version": "sector_weekly_v1", "report_type": "sector_weekly",
        "sector_code": envelope["sector_code"], "sector_name": envelope["sector_name"],
        "period_start": envelope["period_start"], "period_end": envelope["period_end"],
        "generated_at": "2026-08-29T01:00:00Z",
        "run_id": "sector_weekly:2026-08-29:09",
        "dedupe_key": "sector_weekly:2026-08-29:09",
        **envelope["report"],
    }
    canonical = copy.deepcopy(processed)
    changed = 0
    for source in canonical["sources"]:
        if source["published_at"].endswith("Z"):
            source["published_at"] = source["published_at"][:-1] + "+00:00"
            changed += 1
    assert len(processed["sources"]) == 18 and changed == 10
    processed_before = copy.deepcopy(processed)
    canonical_before = copy.deepcopy(canonical)
    assert _normalized_report_row(processed) == _normalized_report_row(canonical)
    assert processed == processed_before and canonical == canonical_before
    canonical["sources"][17]["title"] = "Actually different"
    assert _normalized_report_row(processed) != _normalized_report_row(canonical)


def test_claim_returns_one_assignment_and_prevents_double_claim(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db, 2)
    _enqueue(db, 6)
    conn = connect_sector_db(db)
    try:
        first = claim_next(conn, OWNER, now=AT)
        assert first is not None and first["sector_code"] == 2
        assert claim_next(conn, OWNER, now=AT) is None
        assert claim_next(conn, "second-worker", now=AT) is None
    finally:
        conn.close()


def test_bridge_claim_contract_contains_prompt_but_no_api_controls(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db)
    result = claim_one(db, OWNER, work_root=tmp_path / "work", at=AT)
    assert result["status"] == "claimed"
    assignment = result["assignment"]
    assert "日本企業名や国内ニュースから検索を始めず" in assignment["research_prompt"]
    assert assignment["result_schema_version"] == RESULT_SCHEMA
    assert "max_tool_calls" not in assignment["research_prompt"]
    assert "OPENAI_API_KEY" not in assignment["research_prompt"]
    assert assignment["contract_hash"]
    assert f".attempt-{assignment['attempt_count']}." in assignment["submit_path"]


def test_code_5_claim_is_textiles_and_contract_verifies_before_research(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 5)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    assert (claimed["sector_code"], claimed["sector_name"]) == (5, "繊維製品")
    active_path = Path(claimed["active_contract_path"])
    db_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    active_hash = hashlib.sha256(active_path.read_bytes()).hexdigest()
    active_mtime = active_path.stat().st_mtime_ns
    verified = verify_claim_one(
        db, claimed["assignment_id"], OWNER, claimed["contract_hash"], work_root=work, at=AT,
    )
    assert verified["verified"] is True
    assert (verified["sector_code"], verified["sector_name"], verified["attempt_count"]) == (5, "繊維製品", 1)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_hash
    assert hashlib.sha256(active_path.read_bytes()).hexdigest() == active_hash
    assert active_path.stat().st_mtime_ns == active_mtime


def test_claim_cli_json_is_ascii_safe_under_cp932_console(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 5)
    command = [
        sys.executable, str(Path(__file__).parents[1] / "tools" / "sector_weekly_work_bridge.py"),
        "--db", str(db), "--work-root", str(work), "--at", AT.isoformat(), "claim", "--owner", OWNER,
    ]
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp932"
    result = subprocess.run(command, capture_output=True, check=True, env=environment)
    assert all(byte < 128 for byte in result.stdout)
    decoded = json.loads(result.stdout.decode("cp932"))
    assert decoded["assignment"]["sector_name"] == "繊維製品"
    assert result.stderr == b""


def test_verify_claim_fails_closed_for_mojibake_or_hash_mismatch(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 5)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    with pytest.raises(SectorBridgeError, match="hash"):
        verify_claim_one(db, claimed["assignment_id"], OWNER, "0" * 64, work_root=work, at=AT)
    active_path = Path(claimed["active_contract_path"])
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["sector_name"] = "邵ｺ邵ｺ製品"
    active_path.write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="canonical mapping"):
        verify_claim_one(
            db, claimed["assignment_id"], OWNER, claimed["contract_hash"], work_root=work, at=AT,
        )


def test_attempt_two_uses_new_draft_and_rejects_attempt_one_payload(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 2)
    first = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    old_draft = Path(first["submit_path"])
    old_draft.parent.mkdir(parents=True, exist_ok=True)
    old_draft.write_text(json.dumps(_envelope(first), ensure_ascii=False), encoding="utf-8")
    fail_one(db, first["assignment_id"], OWNER, "isolated retry", at=AT)
    second_at = AT + timedelta(hours=1)
    second = claim_one(db, OWNER, work_root=work, at=second_at)["assignment"]
    assert second["attempt_count"] == 2
    assert second["submit_path"] != first["submit_path"]
    assert old_draft.exists() and not Path(second["submit_path"]).exists()
    assert Path(first["active_contract_path"]).exists()
    assert Path(second["active_contract_path"]).exists()
    with pytest.raises(SectorBridgeError, match="attempt_count"):
        verify_payload_one(
            db, second["assignment_id"], OWNER, second["contract_hash"], old_draft,
            work_root=work, at=second_at,
        )


def test_textile_payload_passes_pre_stage_contract_and_stages_once(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 5)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    start_one(db, claimed["assignment_id"], OWNER, at=AT)
    report = _report()
    report["full_report_md"] = report["full_report_md"].replace("鉱業", "繊維製品", 1)
    draft = Path(claimed["submit_path"])
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(json.dumps(_envelope(claimed, report), ensure_ascii=False), encoding="utf-8")
    checked = verify_payload_one(
        db, claimed["assignment_id"], OWNER, claimed["contract_hash"], draft,
        work_root=work, at=AT,
    )
    assert checked["status"] == "payload_verified"
    staged = stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    assert staged["status"] == "handoff_pending"
    assert len(list((work / "inbox").glob("*.json"))) == 1


def test_lease_expiry_recovers_assignment_for_retry(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    assignment = _enqueue(db)
    conn = connect_sector_db(db)
    try:
        claim_next(conn, OWNER, now=AT, lease_seconds=60)
        assert recover_expired_leases(conn, now=AT + timedelta(seconds=61)) == 1
        recovered = get_assignment(conn, assignment["assignment_id"])
        assert recovered["status"] == "retry_pending"
        assert recovered["claim_owner"] is None
    finally:
        conn.close()


def test_abandon_rejects_late_bridge_submit_and_next_hour_reclaims(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    start_one(db, claimed["assignment_id"], OWNER, at=AT)
    for minute in (10, 20, 30, 40):
        heartbeat_one(db, claimed["assignment_id"], OWNER, at=AT + timedelta(minutes=minute))
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
    released = abandon_one(
        db, claimed["assignment_id"], OWNER, work_root=work,
        at=AT + timedelta(minutes=49), reason="fixture budget",
    )
    assert released["status"] == "retry_pending"
    with pytest.raises(SectorBridgeError, match="does not own"):
        stage_one(
            db, claimed["assignment_id"], OWNER, draft, work_root=work,
            at=AT + timedelta(minutes=50),
        )
    reclaimed = claim_one(db, OWNER, work_root=work, at=AT + timedelta(hours=1))["assignment"]
    assert reclaimed["assignment_id"] == claimed["assignment_id"]
    assert reclaimed["attempt_count"] == 2
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
    conn.close()


def test_valid_stage_is_local_only_atomic_and_idempotent(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    start_one(db, claimed["assignment_id"], OWNER, at=AT)
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
    first = stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    second = stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    assert first["status"] == "handoff_pending"
    assert second["status"] == "already_staged"
    assert (work / "inbox" / f"{claimed['assignment_id']}.json").exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
    row = conn.execute(
        "SELECT status,attempt_count,claim_owner,lease_expires_at,last_error_type,submitted_payload_hash "
        "FROM sector_weekly_work_assignments WHERE assignment_id=?",
        (claimed["assignment_id"],),
    ).fetchone()
    assert row[:5] == ("retry_pending", 1, None, None, "sync_pending")
    assert len(row[5]) == 64


def test_new_stage_rejects_legacy_english_labels_before_inbox(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    report = _report()
    replacements = {
        "**確認できた事実**\n\n": "**Fact**: ",
        "**日本企業への波及**\n\n": "**Transmission**: ",
        "**利益への影響**\n\n": "**Magnitude**: ",
        "**株価への織り込み**\n\n": "**Pricing-in**: ",
        "**反対材料・注意点**\n\n": "**Counterevidence**: ",
        "**試算**\n\n": "**Estimate**: ",
        "**仮説**\n\n": "**Hypothesis**: ",
    }
    for current, legacy in replacements.items():
        report["full_report_md"] = report["full_report_md"].replace(current, legacy)
    draft = tmp_path / "legacy.json"
    draft.write_text(json.dumps(_envelope(claimed, report), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SectorValidationError, match="standalone Japanese label paragraphs"):
        stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    assert not (work / "inbox" / f"{claimed['assignment_id']}.json").exists()


def test_structurally_invalid_material_is_blocked_before_inbox_and_canonical(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    report = _report()
    report["full_report_md"] = report["full_report_md"].replace(
        "**反対材料・注意点**", "反対材料・注意点", 1,
    )
    draft = tmp_path / "bad-structure.json"
    draft.write_text(json.dumps(_envelope(claimed, report), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Exception, match="material 1.*反対材料・注意点"):
        stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    assert not (work / "inbox" / f"{claimed['assignment_id']}.json").exists()
    conn = connect_sector_db(db)
    try:
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
        assert get_assignment(conn, claimed["assignment_id"])["status"] == "retry_pending"
    finally:
        conn.close()


def test_restage_conflicting_payload_is_rejected_without_overwriting_inbox(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")
    stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    inbox = work / "inbox" / f"{claimed['assignment_id']}.json"
    original = inbox.read_bytes()
    changed = _envelope(claimed)
    changed["report"]["direction"] = "positive"
    draft.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="conflicting payload"):
        stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    assert inbox.read_bytes() == original


def test_staged_transport_work_is_not_reclaimed(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "result.json"
    draft.write_text(json.dumps(_envelope(claimed), ensure_ascii=False), encoding="utf-8")

    stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    conn = connect_sector_db(db)
    try:
        assignment = get_assignment(conn, claimed["assignment_id"])
        assert assignment["status"] == "retry_pending"
        assert assignment["last_error_type"] == "sync_pending"
        assert claim_next(conn, OWNER, now=AT + timedelta(hours=1)) is None
        conn.execute(
            "UPDATE sector_weekly_work_assignments SET last_error_type='sync_error' WHERE assignment_id=?",
            (claimed["assignment_id"],),
        )
        conn.commit()
        assert claim_next(conn, OWNER, now=AT + timedelta(hours=2)) is None
    finally:
        conn.close()


def test_malformed_payload_is_quarantined_and_retried(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    draft = tmp_path / "bad.json"
    draft.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="valid UTF-8 JSON"):
        stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)
    conn = connect_sector_db(db)
    try:
        row = get_assignment(conn, claimed["assignment_id"])
        assert row["status"] == "retry_pending"
        assert row["last_error_type"] == "SectorBridgeError"
        assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0
    finally:
        conn.close()
    quarantined = list((work / "quarantine").glob(f"{claimed['assignment_id']}.attempt-1*.json"))
    assert len(quarantined) == 1


def test_retry_is_claimed_before_a_fresh_sector(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db, 2)
    first = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(SectorBridgeError):
        stage_one(db, first["assignment_id"], OWNER, bad, work_root=work, at=AT)
    _enqueue(db, 3)
    second = claim_one(db, OWNER, work_root=work, at=AT + timedelta(hours=1))["assignment"]
    assert second["sector_code"] == 2
    assert second["assignment_id"] == first["assignment_id"]
    assert second["attempt_count"] == 2


@pytest.mark.parametrize("field,value", [
    ("claim_owner", "wrong-worker"),
    ("sector_code", 4),
    ("period_end", "2026-09-06T05:59:59+09:00"),
])
def test_submit_rejects_owner_sector_or_period_mismatch(tmp_path: Path, field: str, value: object):
    db = tmp_path / "db.sqlite"
    work = tmp_path / "work"
    _enqueue(db)
    claimed = claim_one(db, OWNER, work_root=work, at=AT)["assignment"]
    envelope = _envelope(claimed)
    envelope[field] = value
    draft = tmp_path / "wrong.json"
    draft.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SectorBridgeError, match="does not match"):
        stage_one(db, claimed["assignment_id"], OWNER, draft, work_root=work, at=AT)


def test_sunday_retry_enqueues_one_missing_or_failed_sector(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _migrate_fixture(db)
    window = weekly_window(AT)
    conn = connect_sector_db(db)
    try:
        first, created = enqueue_retry_candidate(
            conn, window, now=datetime.fromisoformat("2026-09-06T15:00:00+09:00"),
        )
        assert created is True
        assert first["sector_code"] == 1
        assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 1
    finally:
        conn.close()


def test_sector_queue_does_not_create_or_mutate_company_news_tables(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    _enqueue(db)
    conn = sqlite3.connect(db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sector_weekly_work_assignments" in names
    assert not any(name.startswith("company_news") for name in names)
