from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import tools.sync_market_data as subject
from tools.file_lock import FileLock


def _ledger(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path) if path else ":memory:")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_data_sync_ledger (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.commit()
    return conn


def _row(cap: float, ticker: str = "2163") -> dict:
    return {
        "ticker": ticker,
        "date": "2026-08-12",
        "close": 981.0,
        "market_cap": cap,
        "fetched_at": "ignored-by-hash",
    }


def _install_rows(monkeypatch, tmp_path: Path, rows: list[dict]) -> Path:
    ledger_path = tmp_path / "ledger.db"
    conn = _ledger(ledger_path)
    conn.close()
    monkeypatch.setattr(subject, "read_sqlite", lambda *args, **kwargs: [dict(r) for r in rows])
    monkeypatch.setattr(subject, "_open_ledger", lambda _path: sqlite3.connect(ledger_path))
    return ledger_path


class _SuccessfulAPI:
    payloads: list[list[dict]] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def upsert_batch(self, _table, data, on_conflict=""):
        self.payloads.append(data)
        return len(data)


def test_market_cap_change_changes_sync_payload_hash() -> None:
    assert subject._payload_hash(_row(10_398_269_403)) != subject._payload_hash(
        _row(20_852_341_504)
    )


def test_read_sqlite_uses_price_eligibility_not_ordinary_stock(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "market.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE market_data (
          ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
          volume INTEGER, turnover REAL, adj_factor REAL, adj_close REAL,
          adj_volume INTEGER, market_cap REAL
        );
        CREATE TABLE market_data_universe (
          date TEXT, ticker TEXT, is_common_stock INTEGER,
          is_ordinary_stock INTEGER, is_jquants_price_eligible INTEGER
        );
        INSERT INTO market_data VALUES
          ('6623','2026-08-14',100,101,99,100,10,1000,NULL,NULL,NULL,NULL),
          ('7203','2026-08-14',3000,3020,2980,3020,20,60400,1,3020,20,NULL);
        INSERT INTO market_data_universe VALUES
          ('2026-08-14','6623',1,1,0),
          ('2026-08-14','7203',1,1,1);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(subject, "market_data_retention_start", lambda: "2026-08-01")

    rows = subject.read_sqlite(str(db_path))

    assert [row["ticker"] for row in rows] == ["7203"]


def test_pending_only_excludes_ledger_current_rows() -> None:
    ledger = _ledger()
    current = _row(10_398_269_403, "1301")
    current["_sync_hash"] = subject._payload_hash(current)
    subject._record_synced(ledger, [current])

    changed = _row(20_852_341_504, "2163")
    pending, unchanged = subject._pending_rows([_row(10_398_269_403, "1301"), changed], ledger)

    assert unchanged == 1
    assert [row["ticker"] for row in pending] == ["2163"]


def test_second_apply_is_rejected_without_supabase_request(monkeypatch, tmp_path) -> None:
    blocker = FileLock(subject._SYNC_LOCK_NAME, state_dir=str(tmp_path))
    assert blocker.acquire()
    monkeypatch.setattr(subject, "_SupabaseAPI", lambda *_: pytest.fail("API must not be constructed"))
    try:
        stats = subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                             state_dir=str(tmp_path), sleep_fn=lambda _: None)
    finally:
        blocker.release()
    assert stats["rejected"] == "single_instance_lock"


def test_apply_lock_released_after_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(subject, "_sync_with_lock", lambda *a, **k: {"errors": 0})
    stats = subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                         state_dir=str(tmp_path))
    assert stats["errors"] == 0
    assert not (tmp_path / "market_data_sync.lock").exists()


def test_apply_lock_released_after_exception(monkeypatch, tmp_path) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(subject, "_sync_with_lock", explode)
    with pytest.raises(RuntimeError, match="boom"):
        subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                     state_dir=str(tmp_path))
    assert not (tmp_path / "market_data_sync.lock").exists()


def test_dry_run_does_not_acquire_apply_lock(monkeypatch, tmp_path) -> None:
    blocker = FileLock(subject._SYNC_LOCK_NAME, state_dir=str(tmp_path))
    assert blocker.acquire()
    monkeypatch.setattr(subject, "read_sqlite", lambda *a, **k: [_row(1)])
    try:
        stats = subject.sync("unused", "", "", dry_run=True, recent_days=0,
                             state_dir=str(tmp_path))
    finally:
        blocker.release()
    assert stats["dry_run"] is True


@pytest.mark.parametrize(
    ("stamp", "blocked"),
    [
        ("2026-08-13T15:19:59+09:00", False),
        ("2026-08-13T15:20:00+09:00", True),
        ("2026-08-13T16:09:59+09:00", True),
        ("2026-08-13T16:10:00+09:00", False),
    ],
)
def test_full_apply_blackout_boundaries(monkeypatch, tmp_path, stamp, blocked) -> None:
    monkeypatch.setattr(subject, "_sync_with_lock", lambda *a, **k: {"errors": 0})
    stats = subject.sync(
        "unused", "url", "key", dry_run=False, recent_days=0,
        state_dir=str(tmp_path), reference_time=datetime.fromisoformat(stamp),
    )
    assert (stats.get("rejected") == "realtime_blackout") is blocked


def test_full_apply_rejected_when_realtime_active(monkeypatch, tmp_path) -> None:
    realtime = FileLock("realtime", state_dir=str(tmp_path))
    assert realtime.acquire()
    monkeypatch.setattr(subject, "_sync_with_lock", lambda *a, **k: pytest.fail("sync must not start"))
    try:
        stats = subject.sync(
            "unused", "url", "key", dry_run=False, recent_days=0,
            state_dir=str(tmp_path), reference_time=datetime.fromisoformat("2026-08-13T19:00:00+09:00"),
        )
    finally:
        realtime.release()
    assert stats["rejected"] == "realtime_active"


def test_realtime_mid_sync_pauses_before_next_batch(monkeypatch, tmp_path) -> None:
    _install_rows(monkeypatch, tmp_path, [_row(1, "1301"), _row(2, "1332")])
    _SuccessfulAPI.payloads = []
    monkeypatch.setattr(subject, "_SupabaseAPI", _SuccessfulAPI)
    monkeypatch.setattr(subject, "_BATCH_SIZE", 1)
    active = iter([False, True, False])
    monkeypatch.setattr(subject, "_realtime_lock_active", lambda _state: next(active))
    sleeps: list[float] = []

    stats = subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                         state_dir=str(tmp_path / "locks"), sleep_fn=sleeps.append)

    assert len(_SuccessfulAPI.payloads) == 2
    assert stats["realtime_pauses"] == 1
    assert subject._REALTIME_POLL_SEC in sleeps


def test_circuit_breaker_stops_after_three_consecutive_failures(monkeypatch, tmp_path) -> None:
    rows = [_row(i, str(1301 + i)) for i in range(5)]
    _install_rows(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(subject, "_BATCH_SIZE", 1)
    calls = []

    class FailingAPI:
        def __init__(self, *_):
            pass

        def upsert_batch(self, *_args, **_kwargs):
            calls.append(1)
            raise TimeoutError("slow")

    monkeypatch.setattr(subject, "_SupabaseAPI", FailingAPI)
    stats = subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                         state_dir=str(tmp_path / "locks"), sleep_fn=lambda _: None)
    assert len(calls) == 3
    assert stats["batches_failed"] == 3
    assert stats["circuit_breaker_open"] is True


def test_only_successful_batches_update_ledger(monkeypatch, tmp_path) -> None:
    ledger_path = _install_rows(monkeypatch, tmp_path, [_row(1, "1301"), _row(2, "1332")])
    monkeypatch.setattr(subject, "_BATCH_SIZE", 1)
    calls = 0

    class PartialAPI:
        def __init__(self, *_):
            pass

        def upsert_batch(self, _table, data, on_conflict=""):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("second batch failed")
            return len(data)

    monkeypatch.setattr(subject, "_SupabaseAPI", PartialAPI)
    subject.sync("unused", "url", "key", dry_run=False, recent_days=30,
                 state_dir=str(tmp_path / "locks"), sleep_fn=lambda _: None)

    conn = sqlite3.connect(ledger_path)
    tickers = [row[0] for row in conn.execute("SELECT ticker FROM market_data_sync_ledger")]
    conn.close()
    assert tickers == ["1301"]
