from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.material_url_retry import (
    STATUS_ARCHIVED,
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_VALID,
    RetryCandidate,
    ValidationResult,
    connect_retry_db,
    record_failed_candidate,
    run_due_retries,
    validate_material_url,
)


T0 = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)


def candidate(key="doc-1"):
    return RetryCandidate(
        source_key=key,
        source="JQUANTS_PRIMARY",
        ticker="6294",
        company_name="オカダアイヨン",
        title="2027年3月期 第1四半期 決算説明会資料",
        document_url=f"https://www.release.tdnet.info/inbs/{key}.pdf",
        disclosure_datetime="2026-08-26 13:00",
        disclosure_type="earnings_material",
        source_doc_id=key,
    )


def due(conn, at):
    conn.execute(
        "UPDATE material_url_retries SET next_retry_at=? WHERE status IN ('pending_retry','archived')",
        (at.isoformat(),),
    )
    conn.commit()


def test_initial_timeout_then_retry_success_publishes_original_metadata(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    item = candidate()
    assert record_failed_candidate(
        conn, item, ValidationResult(STATUS_PENDING, "Timeout"), now=T0,
    ) == STATUS_PENDING
    due(conn, T0 + timedelta(minutes=5))
    published = []
    result = run_due_retries(
        conn, now=T0 + timedelta(minutes=5), runner="realtime",
        validator=lambda _: ValidationResult(STATUS_VALID, "pdf_verified", 206),
        publish=lambda restored: published.append(restored) or True,
    )
    assert result["recovered"] == 1
    assert published == [item]
    assert published[0].disclosure_datetime == "2026-08-26 13:00"
    assert published[0].title == item.title and published[0].ticker == "6294"


def test_initial_404_is_pending_and_later_pdf_recovers(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    item = candidate()
    first = ValidationResult(STATUS_PENDING, "not_found", 404)
    assert record_failed_candidate(conn, item, first, now=T0) == STATUS_PENDING
    due(conn, T0 + timedelta(minutes=5))
    result = run_due_retries(
        conn, now=T0 + timedelta(minutes=5), runner="realtime",
        validator=lambda _: ValidationResult(STATUS_VALID, "pdf_verified", 200),
        publish=lambda _: True,
    )
    assert result["recovered"] == 1
    assert conn.execute("SELECT status FROM material_url_retries").fetchone()[0] == STATUS_VALID


def test_repeated_404_phantom_finalizes_only_after_attempt_and_age_threshold(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    item = candidate("phantom")
    missing = ValidationResult(STATUS_PENDING, "not_found", 404)
    for attempt in range(5):
        assert record_failed_candidate(
            conn, item, missing, now=T0 + timedelta(hours=attempt),
        ) == STATUS_PENDING
    assert record_failed_candidate(
        conn, item, missing, now=T0 + timedelta(hours=5),
    ) == STATUS_PENDING
    assert record_failed_candidate(
        conn, item, missing, now=T0 + timedelta(hours=6),
    ) == STATUS_INVALID


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_transient_http_status_is_not_permanently_invalid(status, tmp_path):
    class Response:
        def __init__(self, code):
            self.status_code = code
            self.url = "https://example.test/a.pdf"
            self.headers = {}

        def close(self):
            pass

        def iter_content(self, _size):
            yield b""

    class Session:
        def get(self, *_args, **_kwargs):
            return Response(status)

    result = validate_material_url("https://example.test/a.pdf", session=Session())
    assert result.classification == STATUS_PENDING
    conn = connect_retry_db(tmp_path / f"retry-{status}.db")
    for attempt in range(20):
        state = record_failed_candidate(
            conn, candidate(str(status)), result, now=T0 + timedelta(days=attempt),
        )
    assert state == STATUS_PENDING


@pytest.mark.parametrize("error", [requests.Timeout("late"), requests.ConnectionError("down")])
def test_transport_failure_is_pending(error):
    class Session:
        def get(self, *_args, **_kwargs):
            raise error

    assert validate_material_url(
        "https://example.test/a.pdf", session=Session(),
    ).classification == STATUS_PENDING


def test_old_repeated_transient_moves_to_cold_archive_but_can_still_recover(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    item = candidate("cold-retry")
    transient = ValidationResult(STATUS_PENDING, "transient_http", 503)
    status = record_failed_candidate(conn, item, transient, now=T0)
    for attempt in range(99):
        status = record_failed_candidate(
            conn, item, transient, now=T0 + timedelta(days=31, minutes=attempt),
        )
    assert status == STATUS_ARCHIVED
    recovery_time = T0 + timedelta(days=40)
    due(conn, recovery_time)
    result = run_due_retries(
        conn, now=recovery_time, runner="nightly",
        validator=lambda _: ValidationResult(STATUS_VALID, "pdf_verified", 206),
        publish=lambda _: True,
    )
    assert result["recovered"] == 1
    assert conn.execute("SELECT status FROM material_url_retries").fetchone()[0] == STATUS_VALID


def test_multiple_retry_runs_publish_one_notification(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    record_failed_candidate(
        conn, candidate(), ValidationResult(STATUS_PENDING, "Timeout"), now=T0,
    )
    due(conn, T0 + timedelta(minutes=5))
    published = []
    kwargs = dict(
        now=T0 + timedelta(minutes=5),
        validator=lambda _: ValidationResult(STATUS_VALID, "pdf_verified", 206),
        publish=lambda item: published.append(item.source_key) or True,
    )
    assert run_due_retries(conn, runner="realtime", **kwargs)["recovered"] == 1
    assert run_due_retries(conn, runner="nightly", **kwargs)["recovered"] == 0
    assert published == ["doc-1"]


def test_realtime_nightly_competition_uses_one_lease(tmp_path):
    path = tmp_path / "retry.db"
    seed = connect_retry_db(path)
    record_failed_candidate(
        seed, candidate(), ValidationResult(STATUS_PENDING, "Timeout"), now=T0,
    )
    due(seed, T0 + timedelta(minutes=5))
    seed.close()
    published = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(name):
        conn = connect_retry_db(path)
        barrier.wait()

        def publish(item):
            with lock:
                published.append((name, item.source_key))
            return True

        run_due_retries(
            conn, now=T0 + timedelta(minutes=5), runner=name,
            validator=lambda _: ValidationResult(STATUS_VALID, "pdf_verified", 206),
            publish=publish,
        )
        conn.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("realtime", "nightly")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(published) == 1


def test_run_records_monitoring_counts(tmp_path):
    conn = connect_retry_db(tmp_path / "retry.db")
    record_failed_candidate(
        conn, candidate(), ValidationResult(STATUS_PENDING, "Timeout"), now=T0,
    )
    result = run_due_retries(
        conn, now=T0, runner="nightly", validator=lambda _: pytest.fail("not due"),
        publish=lambda _: pytest.fail("not due"),
    )
    assert result["pending"] == 1
    row = conn.execute(
        "SELECT runner,pending_count,claimed_count FROM material_url_retry_runs"
    ).fetchone()
    assert tuple(row) == ("nightly", 1, 0)
