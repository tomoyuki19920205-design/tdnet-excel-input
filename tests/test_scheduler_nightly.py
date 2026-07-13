"""Regression tests for the Nightly market_data connection."""

import logging
from unittest.mock import MagicMock, patch

import tools.scheduler_nightly as scheduler


def _result(name: str, rc: int = 0) -> scheduler.StepResult:
    result = scheduler.StepResult(name)
    result.rc = rc
    result.status = "success" if rc == 0 else "warning"
    result.duration = 0.01
    return result


def _run_nightly(*, failures=None, dry_run=False):
    failures = failures or {}
    calls = []
    summary = []

    def fake_run_step(name, cmd, **_kwargs):
        calls.append((name, cmd))
        return _result(name, failures.get(name, 0))

    def capture_summary(steps, _elapsed):
        summary.extend(steps)

    argv = ["scheduler_nightly.py"]
    if dry_run:
        argv.append("--dry-run")

    with (
        patch.object(scheduler.sys, "argv", argv),
        patch.object(scheduler, "run_step", side_effect=fake_run_step),
        patch.object(
            scheduler, "acquire_dual_lock", return_value=(MagicMock(), MagicMock())
        ),
        patch.object(scheduler, "release_dual_lock"),
        patch.object(scheduler, "_print_summary", side_effect=capture_summary),
        patch.object(scheduler.os, "makedirs"),
        patch.object(scheduler.logging, "basicConfig"),
        patch.object(
            scheduler.logging,
            "StreamHandler",
            side_effect=lambda *_args, **_kwargs: logging.NullHandler(),
        ),
        patch.object(
            scheduler.logging,
            "FileHandler",
            side_effect=lambda *_args, **_kwargs: logging.NullHandler(),
        ),
    ):
        return_code = scheduler.main()

    return return_code, calls, summary


def _names(calls):
    return [name for name, _cmd in calls]


def test_market_steps_run_in_required_order():
    return_code, calls, _summary = _run_nightly()

    names = _names(calls)
    assert return_code == 0
    assert names.index("per-share-extract") < names.index("market-price-fetch")
    assert names.index("market-price-fetch") < names.index("market-data-sync")
    assert names.index("market-data-sync") < names.index("per-share-sync")


def test_successful_price_fetch_invokes_market_data_sync():
    _return_code, calls, _summary = _run_nightly()

    assert "market-data-sync" in _names(calls)


def test_failed_price_fetch_skips_sync_and_later_steps_continue():
    return_code, calls, summary = _run_nightly(
        failures={"market-price-fetch": 1}
    )

    names = _names(calls)
    sync_result = next(step for step in summary if step.name == "market-data-sync")
    assert return_code == 0
    assert "market-data-sync" not in names
    assert sync_result.status == "warning"
    assert sync_result.stdout_tail == "SKIPPED: market-price-fetch failed rc=1"
    assert "per-share-sync" in names
    assert "edinet-order-extract-nightly" in names


def test_failed_market_data_sync_does_not_stop_later_steps():
    return_code, calls, _summary = _run_nightly(
        failures={"market-data-sync": 1}
    )

    names = _names(calls)
    assert return_code == 0
    assert names.index("market-data-sync") < names.index("per-share-sync")
    assert "edinet-order-extract-nightly" in names


def test_market_commands_match_existing_cli_contracts():
    _return_code, calls, _summary = _run_nightly()
    commands = dict(calls)

    assert commands["market-price-fetch"] == [
        scheduler.PYTHON,
        "-X",
        "utf8",
        "tools/fetch_jquants_prices.py",
        "--recent",
    ]
    assert commands["market-data-sync"] == [
        scheduler.PYTHON,
        "-X",
        "utf8",
        "tools/sync_market_data.py",
        "--apply",
    ]

    _return_code, dry_run_calls, _summary = _run_nightly(dry_run=True)
    dry_run_commands = dict(dry_run_calls)
    assert dry_run_commands["market-price-fetch"] == commands["market-price-fetch"]
    assert dry_run_commands["market-data-sync"] == [
        scheduler.PYTHON,
        "-X",
        "utf8",
        "tools/sync_market_data.py",
    ]


def test_existing_edinet_extract_failure_still_skips_event_step():
    return_code, calls, summary = _run_nightly(
        failures={"edinet-order-extract-nightly": 1}
    )

    names = _names(calls)
    event_result = next(
        step for step in summary if step.name == "edinet-order-event-nightly"
    )
    assert return_code == 0
    assert "edinet-order-event-nightly" not in names
    assert event_result.status == "warning"
    assert event_result.stdout_tail == "SKIPPED: extract step failed"
