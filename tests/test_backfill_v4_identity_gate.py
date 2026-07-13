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
    verifier.return_value.internal_id = "20260709590505"
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


def test_v2_xbrl_source_uses_filing_canonical_fiscal_period(monkeypatch):
    import lib.backfill.worker_v2 as worker_v2

    extracted = Mock(return_value=[SimpleNamespace(
        normalized_segment_name="Smartstore", raw_segment_name="Smartstore",
        period="2026-05-31", quarter="1Q", sales=1242325, profit=-89191,
    )])
    monkeypatch.setattr("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip", extracted)
    filing = _filing(title="title-derived-value")
    pl_zip_path = "verified.zip"

    result = worker_v2._try_xbrl_source(
        pl_zip_path, None, filing,
        {"period": "2026-05-31", "quarter": "1Q"},
        filing.requested_disclosure_no, None, None,
        retry_xbrl=1, timeout_xbrl=1, sleep_fn=lambda _: None,
    )

    extracted.assert_called_once_with(
        pl_zip_path, period="2027-02-28", quarter="1Q", title="title-derived-value",
    )
    assert extracted.call_args.kwargs["period"] != "2026-05-31"
    assert filing.requested_disclosure_no == "20260709590505"
    assert result.source == "xbrl"


@pytest.mark.parametrize(
    ("expected_period", "expected_quarter"),
    [("", ""), (None, None)],
)
def test_v2_xbrl_source_missing_canonical_values_preserves_defaults(monkeypatch, expected_period, expected_quarter):
    import lib.backfill.worker_v2 as worker_v2

    extracted = Mock(return_value=[])
    monkeypatch.setattr("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip", extracted)
    filing = _filing(expected_period=expected_period, expected_quarter=expected_quarter)

    result = worker_v2._try_xbrl_source(
        "verified.zip", None, filing, {"period": "2026-05-31", "quarter": "1Q"},
        filing.requested_disclosure_no, None, None,
        retry_xbrl=1, timeout_xbrl=1, sleep_fn=lambda _: None,
    )

    extracted.assert_called_once_with("verified.zip", period=None, quarter=None, title="title")
    assert result.error == "xbrl_no_segment_facts"


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


def test_offline_pdf_only_does_not_call_network(monkeypatch):
    import lib.backfill.worker as worker
    import lib.backfill.cache as cache

    paths = SimpleNamespace(source_pdf="pdf", cache_dir="cache", xbrl_zip="xbrl-zip")
    monkeypatch.setattr(cache, "has_pdf", lambda _: False)
    pdf_download = Mock(); monkeypatch.setattr("src.downloader.download_document", pdf_download)

    result = worker._download_originals(_filing(), paths, {"attempts": {}}, retry_download=1, timeout_download=1, sleep_fn=lambda _: None, include_xbrl=False, offline_mode=True)

    assert result == (None, None)
    pdf_download.assert_not_called()


def test_isolated_worker_passes_offline_mode_after_identity_gate(monkeypatch):
    resolver = Mock(return_value=SimpleNamespace(zip_path="verified.zip", status="FOUND_CACHE", error_reason="", trusted_provenance=None))
    verifier = Mock(return_value=SimpleNamespace(passed=True, verdict="exact_document_id_match", rejection_reason="", internal_id="20260709590505"))
    w, _ = _patch_gate(monkeypatch, resolver, verifier)
    download = Mock(side_effect=RuntimeError("download boundary"))
    monkeypatch.setattr(w, "_download_originals", download)

    with pytest.raises(RuntimeError, match="download boundary"):
        w.process_one_filing_v4(_filing(), cache_root="isolated-cache", dry_run_only=True, isolated_worker_dry_run=True)

    assert download.call_args.kwargs["include_xbrl"] is False
    assert download.call_args.kwargs["offline_mode"] is True
    verifier.assert_called_once()


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


def test_runner_passes_isolated_worker_dry_run_to_worker(monkeypatch):
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

    runner.run_phase2_v4([{"filing_id": "fid"}], {"fid": _filing()}, store=store, metrics=metrics, run_logger=log, run_id="r", isolated_worker_dry_run=True)

    assert called.call_args.kwargs["isolated_worker_dry_run"] is True


@pytest.mark.parametrize("skip_pdf", [True, False])
def test_runner_passes_skip_pdf_to_worker(monkeypatch, skip_pdf):
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

    runner.run_phase2_v4([{"filing_id": "fid"}], {"fid": _filing()}, store=store, metrics=metrics, run_logger=log, run_id="r", skip_pdf=skip_pdf)

    assert called.call_args.kwargs["skip_pdf"] is skip_pdf


def _successful_xbrl_candidate(w):
    validation = SimpleNamespace(
        status=SimpleNamespace(value="success"), confidence=0.95, reason="ok",
        hard_fail_reason=SimpleNamespace(value=""), raw_segment_count=1,
        valid_segment_count=1, invalid_segment_count=0, sales_non_null_count=1,
        profit_non_null_count=1, invalid_names=[], account_like_ratio=0.0,
        narrative_contamination=False,
    )
    return w.SourceCandidate(
        source="xbrl", attempted=True, available=True,
        segment_records=[{"ticker": "7601", "period": "2027-02-28", "quarter": "1Q", "segment_name": "Core", "segment_sales": 1, "segment_profit": 1}],
        validation=validation,
    )


def _prepare_skip_pdf_worker(
    monkeypatch, *, xbrl_success, verdict="exact_document_id_match",
    internal_id="20260709590505",
):
    resolver = Mock(return_value=SimpleNamespace(zip_path="verified.zip", status="FOUND_CACHE", error_reason="", trusted_provenance=None))
    verifier = Mock(return_value=SimpleNamespace(passed=True, verdict=verdict, rejection_reason="", internal_id=internal_id, zip_sha256="sha"))
    w, cache = _patch_gate(monkeypatch, resolver, verifier)
    monkeypatch.setattr(cache, "save_extract_financials_result", Mock())
    monkeypatch.setattr(cache, "save_extract_segments_result", Mock())
    monkeypatch.setattr(w, "_extract_financials_data", Mock(return_value=({"period": "2027-02-28", "quarter": "1Q"}, "xbrl")))
    if xbrl_success:
        monkeypatch.setattr(w, "_try_xbrl_source", Mock(return_value=_successful_xbrl_candidate(w)))
    else:
        monkeypatch.setattr(w, "_try_xbrl_source", Mock(return_value=w.SourceCandidate(source="xbrl", attempted=True, available=True, error="xbrl_no_segment_facts")))
    return w


@pytest.mark.parametrize(
    ("verdict", "requested_id", "internal_id"),
    [
        ("exact_document_id_match", "20260709590505", "20260709590505"),
        ("official_linked_xbrl_match", "20260713591788", "20260713340570"),
    ],
)
def test_verified_v4_xbrl_uses_internal_id_for_business_column(
    monkeypatch, verdict, requested_id, internal_id,
):
    w = _prepare_skip_pdf_worker(
        monkeypatch, xbrl_success=True, verdict=verdict,
        internal_id=internal_id,
    )
    monkeypatch.setattr(
        "lib.backfill.segment_partial_check.check_xbrl_partial_segments",
        Mock(return_value=(False, "", {"xbrl_count": 1, "edinet_hist_count": None, "other_ratio": 0.0})),
    )

    result = w.process_one_filing_v4(
        _filing(filing_id="862b70fdccda143c86712d70", requested_disclosure_no=requested_id),
        cache_root="tmp", skip_pdf=True,
    )

    record = result.segment_records[0]
    assert record["_requested_disclosure_no"] == requested_id
    assert record["_internal_document_id"] == internal_id
    assert record["tdnet_doc_id"] == internal_id
    assert record["tdnet_doc_id"] != "862b70fdccda143c86712d70"


def test_verified_identity_without_internal_id_is_rejected_without_fallback(monkeypatch):
    w = _prepare_skip_pdf_worker(monkeypatch, xbrl_success=True, internal_id="")
    download = Mock(); xbrl = Mock(); pdf = Mock(); ai = Mock()
    monkeypatch.setattr(w, "_download_originals", download)
    monkeypatch.setattr(w, "_try_xbrl_source", xbrl)
    monkeypatch.setattr(w, "_try_pdf_source_v4", pdf)
    monkeypatch.setattr(w, "extract_segments_with_ai", ai)

    result = w.process_one_filing_v4(
        _filing(filing_id="manifest-filing-id"), cache_root="tmp",
    )

    assert result.status == "quarantined"
    assert result.quarantine_reason == "verified_xbrl_provenance_incomplete"
    download.assert_not_called(); xbrl.assert_not_called(); pdf.assert_not_called(); ai.assert_not_called()


def test_pdf_fallback_tdnet_doc_id_is_not_overwritten_by_verified_xbrl_identity(monkeypatch):
    w = _prepare_skip_pdf_worker(
        monkeypatch, xbrl_success=True,
        verdict="official_linked_xbrl_match", internal_id="official-internal-id",
    )
    monkeypatch.setattr(w, "_download_originals", Mock(return_value=("cached.pdf", None)))
    pdf_candidate = _successful_xbrl_candidate(w)
    pdf_candidate.source = "pdf"
    pdf_candidate.segment_records[0]["tdnet_doc_id"] = "pdf-filing-id"
    monkeypatch.setattr(w, "_try_pdf_source_v4", Mock(return_value=pdf_candidate))
    monkeypatch.setattr(
        "lib.backfill.segment_partial_check.check_xbrl_partial_segments",
        Mock(return_value=(True, "suspicious", {"xbrl_count": 1, "edinet_hist_count": None, "other_ratio": 0.0})),
    )
    monkeypatch.setattr(
        "lib.backfill.segment_partial_check.decide_fallback_adoption",
        Mock(return_value=(True, "use_pdf_v4")),
    )

    result = w.process_one_filing_v4(_filing(), cache_root="tmp", skip_pdf=False)

    assert result.selected_path == "pdf"
    assert result.segment_records[0]["tdnet_doc_id"] == "pdf-filing-id"


def test_skip_pdf_keeps_suspicious_xbrl_and_never_calls_pdf(monkeypatch):
    w = _prepare_skip_pdf_worker(monkeypatch, xbrl_success=True)
    download = Mock(); pdf = Mock(); ai = Mock()
    monkeypatch.setattr(w, "_download_originals", download)
    monkeypatch.setattr(w, "_try_pdf_source_v4", pdf)
    monkeypatch.setattr(w, "extract_segments_with_ai", ai)
    monkeypatch.setattr("lib.backfill.segment_partial_check.check_xbrl_partial_segments", Mock(return_value=(True, "suspicious", {"xbrl_count": 1, "edinet_hist_count": None, "other_ratio": 0.0})))

    result = w.process_one_filing_v4(_filing(), cache_root="tmp", skip_pdf=True)

    assert result.selected_path == "xbrl"
    assert result.fallback_used is False
    assert "partial_check_skipped_by_skip_pdf" in result.candidate_summary
    download.assert_not_called(); pdf.assert_not_called(); ai.assert_not_called()


def test_skip_pdf_xbrl_failure_never_calls_pdf_or_ai(monkeypatch):
    w = _prepare_skip_pdf_worker(monkeypatch, xbrl_success=False)
    download = Mock(); pdf = Mock(); ai = Mock()
    monkeypatch.setattr(w, "_download_originals", download)
    monkeypatch.setattr(w, "_try_pdf_source_v4", pdf)
    monkeypatch.setattr(w, "extract_segments_with_ai", ai)

    result = w.process_one_filing_v4(_filing(), cache_root="tmp", skip_pdf=True)

    assert result.status == "quarantined"
    assert result.reason == "xbrl_no_segment_facts"
    assert result.selected_path == "none"
    download.assert_not_called(); pdf.assert_not_called(); ai.assert_not_called()


def test_skip_pdf_false_keeps_partial_pdf_comparison(monkeypatch):
    w = _prepare_skip_pdf_worker(monkeypatch, xbrl_success=True)
    monkeypatch.setattr(w, "_download_originals", Mock(return_value=("cached.pdf", None)))
    pdf = Mock(return_value=w.SourceCandidate(source="pdf", attempted=True, available=True, segment_records=[]))
    monkeypatch.setattr(w, "_try_pdf_source_v4", pdf)
    monkeypatch.setattr("lib.backfill.segment_partial_check.check_xbrl_partial_segments", Mock(return_value=(True, "suspicious", {"xbrl_count": 1, "edinet_hist_count": None, "other_ratio": 0.0})))
    monkeypatch.setattr("lib.backfill.segment_partial_check.decide_fallback_adoption", Mock(return_value=(False, "keep_xbrl")))

    result = w.process_one_filing_v4(_filing(), cache_root="tmp", skip_pdf=False)

    assert result.selected_path == "xbrl"
    pdf.assert_called_once()
