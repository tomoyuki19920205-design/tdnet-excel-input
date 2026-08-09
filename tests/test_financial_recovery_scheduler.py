from unittest.mock import patch

from tests.test_scheduler_nightly import _names, _run_nightly


def test_nightly_rechecks_financial_queue_after_jquants_sync():
    _return_code, calls, _summary = _run_nightly()
    names = _names(calls)
    assert names.index("sync-jquants-fin") < names.index("financial-recovery")
    assert names.index("financial-recovery") < names.index("per-share-extract")


def test_nightly_financial_recovery_dry_run_has_no_apply_flag():
    _return_code, calls, _summary = _run_nightly(dry_run=True)
    command = dict(calls)["financial-recovery"]
    assert command[-1] == "--dry-run"
    assert "--apply" not in command
