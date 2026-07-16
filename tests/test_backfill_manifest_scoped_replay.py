import json

import pytest

import tools.backfill_segments_tdnet as cli
from lib.backfill.listing_sources.base import FilingInfo
from lib.backfill.state_store import BackfillStateStore


def _filing(index: int, *, requested_id: str | None = None) -> FilingInfo:
    requested = f"20260713{index:06d}" if requested_id is None else requested_id
    return FilingInfo(
        filing_id=f"filing-{index}",
        requested_disclosure_no=requested,
        expected_period="2026-05-31",
        expected_quarter="FY",
        ticker=f"{1000 + index:04d}",
        company_name="Test Co",
        published_at="2026-07-13T00:00:00+09:00",
        title="決算短信",
        disclosure_date="2026-07-13",
        doc_url=f"https://example.invalid/{index}.pdf",
        xbrl_url=f"https://example.invalid/{index}.zip",
        doc_type="financial_statement",
        listing_source="test",
        has_xbrl=True,
    )


def _store(tmp_path, filings, *, status="done", stage="extracted"):
    store = BackfillStateStore(str(tmp_path / "state.db"))
    store.register_filings(filings)
    for filing in filings:
        store.conn.execute(
            "UPDATE filing_state SET status=?, stage=?, attempt_count=2 WHERE filing_id=?",
            (status, stage, filing.filing_id),
        )
    store.conn.commit()
    return store


def _states(store):
    return {
        row["filing_id"]: (row["status"], row["stage"], row["attempt_count"])
        for row in store.conn.execute(
            "SELECT filing_id,status,stage,attempt_count FROM filing_state ORDER BY filing_id"
        )
    }


def test_replay_requeues_only_manifest_done_rows(tmp_path):
    targets = [_filing(i) for i in range(5)]
    outside = _filing(99)
    store = _store(tmp_path, [*targets, outside])
    before = _states(store)

    summary = cli._replay_manifest_done(store, targets)

    after = _states(store)
    assert all(after[f.filing_id][:2] == ("queued", "listing") for f in targets)
    assert after[outside.filing_id] == before[outside.filing_id]
    assert summary["manifest_replay_requested_count"] == 5
    assert summary["manifest_replay_matched_count"] == 5
    assert summary["manifest_replay_requeued_count"] == 5
    assert summary["manifest_replay_non_target_changed_count"] == 0


@pytest.mark.parametrize("status,stage", [("running", "xbrl"), ("failed", "xbrl"), ("queued", "listing")])
def test_replay_state_mismatch_is_atomic(tmp_path, status, stage):
    targets = [_filing(i) for i in range(5)]
    store = _store(tmp_path, targets)
    store.conn.execute(
        "UPDATE filing_state SET status=?, stage=? WHERE filing_id=?",
        (status, stage, targets[2].filing_id),
    )
    store.conn.commit()
    before = _states(store)

    with pytest.raises(cli.ManifestReplayStop) as exc_info:
        cli._replay_manifest_done(store, targets)

    assert exc_info.value.code == cli._MANIFEST_REPLAY_STATE_MISMATCH
    assert _states(store) == before


def test_replay_missing_target_is_atomic(tmp_path):
    targets = [_filing(i) for i in range(5)]
    store = _store(tmp_path, targets[:-1])
    before = _states(store)

    with pytest.raises(cli.ManifestReplayStop) as exc_info:
        cli._replay_manifest_done(store, targets)

    assert exc_info.value.code == cli._MANIFEST_REPLAY_STATE_MISMATCH
    assert _states(store) == before


def test_replay_rejects_duplicate_or_blank_requested_ids(tmp_path):
    store = _store(tmp_path, [_filing(1), _filing(2)])
    with pytest.raises(cli.ManifestReplayStop) as duplicate:
        cli._replay_manifest_done(store, [_filing(1, requested_id="same"), _filing(2, requested_id="same")])
    assert duplicate.value.code == cli._MANIFEST_REPLAY_MANIFEST_INVALID
    with pytest.raises(cli.ManifestReplayStop) as blank:
        cli._replay_manifest_done(store, [_filing(1, requested_id="")])
    assert blank.value.code == cli._MANIFEST_REPLAY_MANIFEST_INVALID


def test_replay_does_not_call_global_reset(tmp_path, monkeypatch):
    targets = [_filing(i) for i in range(2)]
    store = _store(tmp_path, targets)
    monkeypatch.setattr(store, "reset_done_to_queued", lambda: pytest.fail("global reset called"))
    cli._replay_manifest_done(store, targets)
    assert all(state[:2] == ("queued", "listing") for state in _states(store).values())


@pytest.mark.parametrize(
    "args",
    [
        ["--replay-manifest-done"],
        ["--replay-manifest-done", "--filing-list", "x.json"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--worker-version", "v2"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--workers", "2"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--resume"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--repair-extracted"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--retry-failed"],
        ["--replay-manifest-done", "--filing-list", "x.json", "--apply", "--isolated-worker-dry-run"],
    ],
)
def test_invalid_replay_modes_emit_structured_stop(monkeypatch, capsys, args):
    monkeypatch.setattr("sys.argv", ["backfill_segments_tdnet.py", *args])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["stop_code"] == cli._MANIFEST_REPLAY_INVALID_MODE
    assert payload["worker_started"] is False


def test_summary_is_backward_compatible_when_replay_is_off():
    class Metrics:
        def summary_dict(self):
            return {}

    summary = cli._summary_with_validation_rejections(Metrics())
    assert summary["manifest_replay_enabled"] is False
    assert summary["manifest_replay_requeued_count"] == 0
