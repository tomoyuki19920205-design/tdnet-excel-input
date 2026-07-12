from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


def _filing(**overrides):
    values = dict(filing_id="fid", requested_disclosure_no="20260709590505", ticker="7601", expected_period="2027-02-28", expected_quarter="1Q", doc_url="pdf", xbrl_url="xbrl", title="title", disclosure_date="2026-07-10")
    values.update(overrides); return SimpleNamespace(**values)


def _patch_gate(monkeypatch, resolver, verifier):
    import lib.backfill.worker_v4 as w
    import lib.backfill.cache as cache
    paths = SimpleNamespace(cache_dir="tmp")
    monkeypatch.setattr(cache, "ensure_cache_layout", Mock(return_value=paths)); monkeypatch.setattr(cache, "write_metadata", Mock())
    monkeypatch.setattr(cache, "save_quarantine", Mock()); monkeypatch.setattr(cache, "append_filing_log", Mock())
    monkeypatch.setattr("src.segment.segment_zip_resolver.resolve_xbrl_zip", resolver)
    monkeypatch.setattr("src.segment.zip_identity_verifier.verify_zip_identity", verifier)
    return w, cache


@pytest.mark.parametrize("field,reason", [("requested_disclosure_no", "missing_requested_disclosure_no"), ("ticker", "missing_expected_ticker"), ("expected_period", "missing_expected_period"), ("expected_quarter", "missing_expected_quarter")])
def test_missing_values_stop_before_all_sources(monkeypatch, field, reason):
    resolver, verifier = Mock(), Mock(); w, _ = _patch_gate(monkeypatch, resolver, verifier)
    downstream = Mock(); monkeypatch.setattr(w, "_download_originals", downstream)
    result = w.process_one_filing_v4(_filing(**{field: ""}), cache_root="tmp")
    assert result.status == "quarantined" and result.quarantine_reason == reason
    resolver.assert_not_called(); verifier.assert_not_called(); downstream.assert_not_called()


def test_resolver_and_verifier_rejections_stop_before_download(monkeypatch):
    resolver = Mock(return_value=SimpleNamespace(zip_path=None, status="JQUANTS_URL_NOT_FOUND", error_reason="resolver_reason", trusted_provenance=None)); verifier = Mock()
    w, _ = _patch_gate(monkeypatch, resolver, verifier); download = Mock(); ai = Mock(); monkeypatch.setattr(w, "_download_originals", download); monkeypatch.setattr(w, "extract_segments_with_ai", ai)
    result = w.process_one_filing_v4(_filing(), cache_root="tmp", dry_run_only=True)
    assert result.quarantine_reason == "resolver_reason"; resolver.assert_called_once_with(doc_id="20260709590505", ticker="7601", expected_quarter="1Q", expected_period="2027-02-28", allow_jquants_fetch=False, persist_provenance=False)
    verifier.assert_not_called(); download.assert_not_called(); ai.assert_not_called()


@pytest.mark.parametrize("verdict", ["exact_document_id_match", "official_linked_xbrl_match"])
def test_accepted_identity_uses_same_zip_and_pdf_only(monkeypatch, verdict):
    provenance = object(); resolver = Mock(return_value=SimpleNamespace(zip_path="verified.zip", status="FOUND_CACHE", error_reason="", trusted_provenance=provenance)); verifier = Mock(return_value=SimpleNamespace(passed=True, verdict=verdict, rejection_reason=""))
    w, _ = _patch_gate(monkeypatch, resolver, verifier)
    download = Mock(return_value=(None, None)); monkeypatch.setattr(w, "_download_originals", download)
    seen = []
    monkeypatch.setattr(w, "_extract_financials_data", lambda doc, xbrl, *a, **k: (seen.append(xbrl) or None, ""))
    monkeypatch.setattr(w, "_try_xbrl_source", lambda xbrl, *a, **k: (_ for _ in ()).throw(RuntimeError(xbrl)))
    with pytest.raises(RuntimeError, match="verified.zip"):
        w.process_one_filing_v4(_filing(), cache_root="tmp", dry_run_only=False)
    assert seen == ["verified.zip"]
    download.assert_called_once(); assert download.call_args.kwargs["include_xbrl"] is False
    assert verifier.call_args.kwargs["trusted_provenance"] is provenance
    assert all(v != "ANY" and v != "" for v in (verifier.call_args.kwargs["expected_ticker"], verifier.call_args.kwargs["expected_period"], verifier.call_args.kwargs["expected_quarter"]))


@pytest.mark.parametrize("mode,expected", [(True, False), (False, True)])
def test_dry_run_flags(monkeypatch, mode, expected):
    resolver = Mock(return_value=SimpleNamespace(zip_path=None, status="SKIPPED_NO_FETCH_ALLOWED", error_reason="", trusted_provenance=None)); verifier = Mock(); w, _ = _patch_gate(monkeypatch, resolver, verifier)
    w.process_one_filing_v4(_filing(), cache_root="tmp", dry_run_only=mode)
    assert resolver.call_args.kwargs["allow_jquants_fetch"] is expected
    assert resolver.call_args.kwargs["persist_provenance"] is expected


def test_pdf_only_skips_archive_and_xbrl_download(monkeypatch):
    import lib.backfill.worker as worker
    import lib.backfill.cache as cache
    paths = SimpleNamespace(source_pdf="pdf", cache_dir="cache", xbrl_zip="xbrl-zip")
    monkeypatch.setattr(cache, "has_pdf", lambda _: False)
    archive = Mock(); monkeypatch.setattr(cache, "resolve_xbrl_from_archive", archive)
    pdf_download = Mock(return_value="pdf"); xbrl_download = Mock()
    copy_file = Mock()
    monkeypatch.setattr("src.downloader.download_document", pdf_download)
    monkeypatch.setattr("src.downloader.download_document_ex", xbrl_download)
    monkeypatch.setattr(worker.shutil, "copy2", copy_file)

    result = worker._download_originals(_filing(), paths, {"attempts": {}}, retry_download=1, timeout_download=1, sleep_fn=lambda _: None, include_xbrl=False)

    assert result == ("pdf", None)
    assert pdf_download.call_args_list == [call("pdf", "cache")]
    archive.assert_not_called()
    xbrl_download.assert_not_called()
    copy_file.assert_not_called()


@pytest.mark.parametrize("mode", [True, False])
def test_runner_passes_dry_run_to_worker(monkeypatch, mode):
    import lib.backfill.phase2_runner as runner
    called = Mock(return_value=SimpleNamespace(status="skipped_normal", metrics={"total_ms": 0}))
    monkeypatch.setattr("lib.backfill.worker_v4.process_one_filing_v4", called)
    class Future:
        def result(self): return called.return_value
    class Executor:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def submit(self, fn, arg): fn(arg); return Future()
    monkeypatch.setattr(runner, "ThreadPoolExecutor", Executor); monkeypatch.setattr(runner, "as_completed", lambda futures: list(futures))
    store = SimpleNamespace(mark_done=Mock()); metrics = SimpleNamespace(record_v2_result=Mock()); log = SimpleNamespace(log_filing_result_v2=Mock())
    runner.run_phase2_v4([{"filing_id": "fid"}], {"fid": _filing()}, store=store, metrics=metrics, run_logger=log, run_id="r", dry_run_only=mode)
    assert called.call_args.kwargs["dry_run_only"] is mode
