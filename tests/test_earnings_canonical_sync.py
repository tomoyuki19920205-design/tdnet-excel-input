import pytest
import sqlite3
import dataclasses
from unittest.mock import patch, MagicMock, ANY
from src.events.earnings_production_pipeline import run_earnings_production, _sync_canonical_financials
from src.events.summary_financials import EarningsSummaryData

class DummyDoc:
    def __init__(self, ticker, title, disclosure_id="123456"):
        self.ticker = ticker
        self.title = title
        self.disclosure_id = disclosure_id
        self.doc_id = disclosure_id
        self.xbrl_url = ""
        self.doc_url = ""
        self.published_at = "2026-07-10 12:00:00"
        self.disclosure_datetime = "2026-07-10 12:00:00"

    def __getattr__(self, name):
        return ""


@pytest.fixture(autouse=True)
def mock_resolve_xbrl_zip_default(tmp_path_factory):
    from src.segment.segment_zip_resolver import ZipResolveResult
    tmp_dir = tmp_path_factory.mktemp("default_zip_dir")
    dummy_zip = make_identity_test_zip(
        tmp_path=tmp_dir,
        requested_disclosure_no="20260709590505",
        internal_document_id="20260709590505",
        ticker="7601",
        period="2027-02-28",
        quarter="1Q",
        document_type="attachment_xbrl"
    )
    with patch("src.segment.segment_zip_resolver.resolve_xbrl_zip") as m:
        m.return_value = ZipResolveResult(
            zip_path=str(dummy_zip),
            source="tdnet_cache",
            status="FOUND_CACHE",
            error_reason="",
            cache_hit=True,
            downloaded=False,
            requested_disclosure_no="20260709590505",
            zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
            trusted_provenance=None,
            resolution_kind="exact_cache"
        )
        yield m


class TestCanonicalSyncIntegration:
    @pytest.fixture
    def setup_db(self):
        conn = sqlite3.connect(":memory:")
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        ensure_earnings_summary_table(conn)
        return conn

    @pytest.fixture
    def mock_deps(self):
        with patch("src.events.earnings_production_pipeline._find_cached_xbrl") as m_cache, \
             patch("src.events.earnings_production_pipeline.run_shadow_write_plan") as m_shadow, \
             patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events") as m_save_events, \
             patch("src.events.earnings_production_pipeline.send_earnings_discord") as m_discord, \
             patch("src.events.earnings_production_pipeline._sync_canonical_financials") as m_sync:

            m_cache.return_value = "dummy.zip"

            e = EarningsSummaryData(sales_current=9361000000, op_current=589000000)

            m_save_events.return_value = {"action": "inserted", "dedupe_key": "123"}
            m_discord.return_value = True

            yield {
                "cache": m_cache,
                "save_events": m_save_events,
                "sync": m_sync,
                "e": e,
            }

    def test_case1_sequential_428A_3Q(self, setup_db, mock_deps, monkeypatch):
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch:
            m_fetch.return_value = {
                "earnings": dataclasses.asdict(mock_deps["e"]),
                "company_name": "Cypress HD",
                "fiscal_year": "2026",
                "quarter": "3Q",
                "summary_line": "",
                "segment_lines": [],
                "company_reasons": [],
                "segment_reasons": [],
                "full_message": "",
                "guidance": None,
                "is_4q": False,
                "fy_reason": "quarter=3Q"
            }
            doc = DummyDoc("428A", "2026年8月期 第3四半期決算短信〔IFRS〕（連結）", "123456")

            run_earnings_production([doc], setup_db, webhook_url="")

        assert mock_deps["sync"].call_count == 1
        call_args = mock_deps["sync"].call_args[1]
        assert call_args["ticker"] == "428A"
        assert call_args["period"] == "2026-08-31"
        assert call_args["quarter"] == "3Q"
        assert call_args["route"] == "sequential"
        assert call_args["sales_value"] == 9361000000
        assert call_args["op_value"] == 589000000

    def test_case2_subprocess_428A_3Q(self, setup_db, mock_deps, monkeypatch):
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "428A")

        with patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run") as m_run, \
             patch("src.events.earnings_subprocess_runner.build_save_ready_payload") as m_payload, \
             patch("src.events.earnings_subprocess_runner.validate_save_ready_payload") as m_valid, \
             patch("src.events.earnings_subprocess_runner.build_save_call_plan") as m_plan, \
             patch("src.events.earnings_subprocess_runner.validate_save_call_plan") as m_cp_valid, \
             patch("src.events.earnings_subprocess_runner.build_discord_call_plan") as m_discord_plan, \
             patch("src.events.tdnet_event_store.save_event_to_supabase") as m_supa:

            m_run.return_value = {"results": [{"ticker": "428A", "status": "ok"}]}
            m_valid.return_value = (True, "")
            m_cp_valid.return_value = (True, "")
            m_payload.return_value = {
                "extracted": {
                    "period": "2026-08-31",
                    "guidance": {}
                }
            }
            m_plan.return_value = {
                "earnings_summary_args": {
                    "ticker": "428A",
                    "title": "2026年8月期 第3四半期決算短信〔IFRS〕（連結）",
                    "quarter": "3Q",
                    "sales_value": 9361000000,
                    "op_value": 589000000,
                    "fingerprint": "123456",
                    "company_name": "Cypress HD",
                    "fiscal_year": "2026",
                    "disclosure_date": "2026-07-10"
                },
                "tdnet_event_payload": {"source_doc_id": "123456"}
            }
            m_discord_plan.return_value = {"discord_message": "test"}
            m_supa.return_value = {"action": "inserted"}

            doc = DummyDoc("428A", "2026年8月期 第3四半期決算短信〔IFRS〕（連結）", "123456")

            run_earnings_production([doc], setup_db, webhook_url="")

        assert mock_deps["sync"].call_count == 1
        call_args = mock_deps["sync"].call_args[1]
        assert call_args["ticker"] == "428A"
        assert call_args["period"] == "2026-08-31"
        assert call_args["quarter"] == "3Q"
        assert call_args["route"] == "subprocess"

    def test_case3_save_failure_no_sync(self, setup_db, mock_deps, monkeypatch):
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        mock_deps["save_events"].return_value = {"action": "error"}

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch, \
             patch("src.events.tdnet_event_store._get_supabase", return_value=MagicMock()), \
             patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True), \
             patch("src.events.earnings_production_pipeline._check_canonical_segments_saved", return_value=True):
            m_fetch.return_value = {
                "earnings": dataclasses.asdict(mock_deps["e"]),
                "company_name": "Cypress HD",
                "fiscal_year": "2026",
                "quarter": "3Q",
            }
            doc = DummyDoc("428A", "2026年8月期 第3四半期決算短信〔IFRS〕（連結）", "123456")

            run_earnings_production([doc], setup_db, webhook_url="")

        assert mock_deps["sync"].call_count == 0

    def test_case4_dry_run_pipeline(self, setup_db, mock_deps, monkeypatch):
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        mock_deps["save_events"].return_value = {"action": "dry_run"}

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch:
            m_fetch.return_value = {
                "earnings": dataclasses.asdict(mock_deps["e"]),
                "company_name": "Cypress HD",
                "fiscal_year": "2026",
                "quarter": "3Q",
            }
            doc = DummyDoc("428A", "2026年8月期 第3四半期決算短信〔IFRS〕（連結）", "123456")

            run_earnings_production([doc], setup_db, webhook_url="", dry_run=True)

        assert mock_deps["sync"].call_count == 0

    def test_case5_already_exists_no_double_sync(self, setup_db, mock_deps, monkeypatch):
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        mock_deps["save_events"].return_value = {"action": "dedup_skipped"}

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch, \
             patch("src.events.tdnet_event_store._get_supabase", return_value=MagicMock()), \
             patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True), \
             patch("src.events.earnings_production_pipeline._check_canonical_segments_saved", return_value=True):
            m_fetch.return_value = {
                "earnings": dataclasses.asdict(mock_deps["e"]),
                "company_name": "Cypress HD",
                "fiscal_year": "2026",
                "quarter": "3Q",
            }
            doc = DummyDoc("428A", "2026年8月期 第3四半期決算短信〔IFRS〕（連結）", "123456")

            run_earnings_production([doc], setup_db, webhook_url="")

        assert mock_deps["sync"].call_count == 0


class TestCanonicalSyncFunction:
    @patch("lib.pipeline.canonical_writer.write_financials_canonical")
    def test_case4_dry_run_db_write_guard(self, mock_write, monkeypatch):
        monkeypatch.setenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", "0")
        monkeypatch.setenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", "0")

        with patch("src.events.canonical_write_gateway.build_normalized_canonical_write_plan") as m_build:
            m_build.return_value = []
            _sync_canonical_financials(
                "428A", "2026-08-31", "3Q", 100, 200, None, None, {}, "123", dry_run=True, route="seq"
            )
        assert mock_write.call_count == 0


def make_identity_test_zip(
    tmp_path,
    requested_disclosure_no,
    internal_document_id,
    ticker,
    period,
    quarter,
    document_type,
):
    if document_type != "attachment_xbrl":
        raise ValueError("This fixture helper supports attachment_xbrl only")

    import zipfile
    ticker_5 = f"{ticker}0" if len(ticker) == 4 else ticker
    xml_name = f"tse-aced-{ticker_5}-{internal_document_id}.xml"
    htm_name = f"Summary_{internal_document_id}.htm"
        
    zip_path = tmp_path / f"test_{requested_disclosure_no}.zip"
    q_val = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}.get(quarter, "4")
    
    htm_content = f"""
    <html>
      <body>
        <xbrli:endDate>{period}</xbrli:endDate>
        <QuarterlyPeriod>{q_val}</QuarterlyPeriod>
        <ix:nonNumeric scheme="http://example.com/sicc">{ticker_5}</ix:nonNumeric>
      </body>
    </html>
    """
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(xml_name, b"<dummy/>")
        zf.writestr(htm_name, htm_content.encode("utf-8"))
        
    return zip_path


def _make_identity_zip(tmp_path, disclosure_no: str, filename: str = None, quarter: str = "1Q"):
    ticker = "7601"
    if "9982" in disclosure_no or disclosure_no == "20260709590450" or (filename and "9982" in filename):
        ticker = "9982"
    elif "9999" in disclosure_no:
        ticker = "9999"
        
    return make_identity_test_zip(
        tmp_path=tmp_path,
        requested_disclosure_no=disclosure_no,
        internal_document_id=disclosure_no,
        ticker=ticker,
        period="2027-02-28",
        quarter=quarter,
        document_type="attachment_xbrl"
    )


def test_make_identity_test_zip_single(tmp_path):
    from src.segment.zip_identity_verifier import extract_actual_metadata_from_zip
    zip_path = make_identity_test_zip(
        tmp_path=tmp_path,
        requested_disclosure_no="20260709590505",
        internal_document_id="20260709590505",
        ticker="7601",
        period="2027-02-28",
        quarter="1Q",
        document_type="attachment_xbrl"
    )
    meta = extract_actual_metadata_from_zip(str(zip_path))
    assert meta["ticker"] == "7601"
    assert meta["period"] == "2027-02-28"
    assert meta["quarter"] == "1Q"
    assert meta["internal_document_id"] == "20260709590505"
    assert meta["document_type"] == "attachment_xbrl"


class TestRealtimeSegmentSync:
    @pytest.fixture
    def setup_db(self):
        conn = sqlite3.connect(":memory:")
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        ensure_earnings_summary_table(conn)
        return conn

    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
    @patch("src.events.earnings_production_pipeline.load_json")
    @patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
    def test_segment_sync_sequential_called(self, m_resolve, m_load, m_save, m_cache, m_sync_fin, m_sync_seg, setup_db, monkeypatch):
        # 8. 逐次ルートで両識別子が正しく渡る
        # 25. PL保存成功を維持
        # 27. ポプラ相当1Q
        from src.segment.segment_zip_resolver import ZipResolveResult
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        m_cache.return_value = "C:/xbrl_archive/20260709590505.zip"
        m_resolve.return_value = ZipResolveResult(
            zip_path="C:/xbrl_archive/20260709590505.zip",
            source="tdnet_cache",
            status="FOUND_CACHE",
            error_reason="",
            cache_hit=True,
            downloaded=False,
            requested_disclosure_no="20260709590505",
            zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
            trusted_provenance=None,
            resolution_kind="exact_cache"
        )
        m_save.return_value = {"action": "inserted"}

        e = EarningsSummaryData(sales_current=123, op_current=45)
        m_load.return_value = {
            "earnings": dataclasses.asdict(e),
            "company_name": "Test Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
        }

        doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"

        run_earnings_production([doc], setup_db, webhook_url="")

        assert m_sync_fin.call_count == 1
        assert m_sync_seg.call_count == 1
        call_args = m_sync_seg.call_args[1]
        assert call_args["ticker"] == "7601"
        assert call_args["period"] == "2027-02-28"
        assert call_args["quarter"] == "1Q"
        assert call_args["xbrl_path"] == "C:/xbrl_archive/20260709590505.zip"
        # 1. 64桁は canonical_filing_id へ
        assert call_args["canonical_filing_id"] == "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        # 2. 14桁は common_disclosure_no へ
        assert call_args["common_disclosure_no"] == "20260709590505"
        assert call_args["route"] == "sequential"

    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
    @patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
    def test_segment_sync_subprocess_called(self, m_resolve, m_save, m_sync_fin, m_sync_seg, setup_db, monkeypatch):
        # 9. サブプロセスルートで両識別子が正しく渡る
        # 10. サブプロセスでXBRLパスがNoneにならない正常例
        # 28. タキヒヨー相当1Q
        from src.segment.segment_zip_resolver import ZipResolveResult
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "9982")

        with patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run") as m_run, \
             patch("src.events.earnings_subprocess_runner.build_save_ready_payload") as m_payload, \
             patch("src.events.earnings_subprocess_runner.validate_save_ready_payload") as m_valid, \
             patch("src.events.earnings_subprocess_runner.build_save_call_plan") as m_plan, \
             patch("src.events.earnings_subprocess_runner.validate_save_call_plan") as m_cp_valid, \
             patch("src.events.earnings_subprocess_runner.build_discord_call_plan") as m_discord_plan, \
             patch("src.events.tdnet_event_store.save_event_to_supabase") as m_supa, \
             patch("src.events.earnings_production_pipeline._find_cached_xbrl") as m_cache:

            m_run.return_value = {"results": [{"ticker": "9982", "status": "ok"}]}
            m_valid.return_value = (True, "")
            m_cp_valid.return_value = (True, "")
            m_cache.return_value = "C:/xbrl_archive/20260709590450.zip"
            m_resolve.return_value = ZipResolveResult(
                zip_path="C:/xbrl_archive/20260709590450.zip",
                source="tdnet_cache",
                status="FOUND_CACHE",
                error_reason="",
                cache_hit=True,
                downloaded=False,
                requested_disclosure_no="20260709590450",
                zip_sha256="719f1592f98cd05c2a60601726e3635f5495af899c188693f85ce487dae0a5b5",
                trusted_provenance=None,
                resolution_kind="exact_cache"
            )
            m_payload.return_value = {
                "extracted": {
                    "period": "2027-02-28",
                    "guidance": {}
                }
            }
            m_plan.return_value = {
                "earnings_summary_args": {
                    "ticker": "9982",
                    "title": "2027年2月期 第1四半期決算短信（連結）",
                    "quarter": "1Q",
                    "sales_value": 100,
                    "op_value": 20,
                    "fingerprint": "7890",
                    "company_name": "Test Takihyo",
                    "fiscal_year": "2027",
                    "disclosure_date": "2026-07-10"
                },
                "tdnet_event_payload": {
                    "source_doc_id": "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c",
                    "source_url": "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
                }
            }
            m_discord_plan.return_value = {"discord_message": "test"}
            m_supa.return_value = {"action": "inserted"}

            doc = DummyDoc("9982", "2027年2月期 第1四半期決算短信（連結）", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
            doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
            run_earnings_production([doc], setup_db, webhook_url="")

        assert m_sync_fin.call_count == 1
        assert m_sync_seg.call_count == 1
        call_args = m_sync_seg.call_args[1]
        assert call_args["ticker"] == "9982"
        assert call_args["period"] == "2027-02-28"
        assert call_args["quarter"] == "1Q"
        assert call_args["xbrl_path"] == "C:/xbrl_archive/20260709590450.zip"
        assert call_args["canonical_filing_id"] == "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        assert call_args["common_disclosure_no"] == "20260709590450"
        assert call_args["route"] == "subprocess"

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_poplar(self, m_write, m_extract, tmp_path):
        # 16. 前年同期を除外することの確認
        # 27. ポプラ相当1Q
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            # 前年同期 (7.当期のみ保存、16.前年同期は除外)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2026-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1299, profit=-63, raw_json={"_context_evidence": {"context_start": "2025-03-01", "context_end": "2025-05-31"}}),
            # 当期 (90日duration)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1242, profit=-90, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Lawson Poplar", sales=1529, profit=248, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=167, profit=-5, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_extract.return_value = m_res
        m_write.return_value = {"written": 3, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert args["ticker"] == "7601"
        assert args["period"] == "2027-02-28"
        assert args["quarter"] == "1Q"
        assert len(args["segments"]) == 3

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_takihyo(self, m_write, m_extract, tmp_path):
        # 28. タキヒヨー相当1Q
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        zip_path = _make_identity_zip(tmp_path, "20260709590450")
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Apparel And Textile", sales=15388, profit=447, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Rental Business", sales=249, profit=140, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Material", sales=1775, profit=211, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=263, profit=16, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_extract.return_value = m_res
        m_write.return_value = {"written": 4, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("9982", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590450", str(zip_path), dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert len(args["segments"]) == 4

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_no_data(self, m_write, m_extract, tmp_path):
        # 24. セグメントなし開示を毎回再解析し続けない (正常終了判定)
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_res = MagicMock()
        m_res.status = "success_empty"
        m_res.segments = []
        m_extract.return_value = m_res
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_dry_run(self, m_write, m_extract, tmp_path):
        # 11. dry-runでwriter未呼び出し
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_extract.return_value = m_res
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), dry_run=True, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_extract_fail_handled(self, m_write, m_extract, tmp_path):
        # 26. セグメント失敗で通知・PLを巻き戻さない
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_extract.side_effect = Exception("Extract error")
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_writer_fail_handled(self, m_write, m_extract, tmp_path):
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_extract.return_value = m_res
        m_write.side_effect = Exception("Write error")
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), dry_run=False, route="seq")

    # ───── 新規必須テストケース ─────

    def test_zip_guards(self, monkeypatch):
        # 4. 書類ID一致ZIPは許可
        # 5. 書類ID不一致ZIPは拒否
        # 6. 書類ID不明ならwriter未呼び出し
        # 7. 別開示のキャッシュZIPを流用しない
        from src.events.earnings_production_pipeline import _sync_canonical_segments

        # 6. 書類ID不明
        with patch("lib.pipeline.canonical_writer.write_segments_canonical") as m_write:
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "invalid_no", "C:/xbrl_archive/20260709590505.zip", False, "seq")
            assert m_write.call_count == 0

        # 5/7. 書類ID不一致ZIP
        with patch("lib.pipeline.canonical_writer.write_segments_canonical") as m_write:
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590450.zip", False, "seq")
            assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_context_duration_selection(self, m_write, m_extract, tmp_path):
        # 12. 1Qの3か月累計を選択
        # 13. 2Qで6か月累計を選択し単独3か月を除外
        # 14. 3Qで9か月累計を選択し単独3か月を除外
        # 15. FYで通期を選択
        # 17. context順序を入れ替えても結果が変わらない
        # 18. 同一memberの複数contextで上書き混在しない
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        zip_path = _make_identity_zip(tmp_path, "20260709590505", quarter="2Q")
        # 13. 2Q累計(180日)優先、単独(90日)除外
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-06-01", "context_end": "2026-08-31"}}), # 単独3ヶ月 (90日)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=200, profit=20, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-08-31"}}), # 累計6ヶ月 (180日)
        ]
        m_extract.return_value = m_res
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "2Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), False, "seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert len(args["segments"]) == 1
        assert args["segments"][0]["sales"] == 200 # 累計が選択されていること

        # 17. 順序入れ替え
        m_write.reset_mock()
        m_res2 = MagicMock()
        m_res2.status = "success_with_rows"
        m_res2.segments = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=200, profit=20, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-08-31"}}), # 累計6ヶ月 (180日)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-06-01", "context_end": "2026-08-31"}}), # 単独3ヶ月 (90日)
        ]
        m_extract.return_value = m_res2
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "2Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", str(zip_path), False, "seq")

        args = m_write.call_args[1]
        assert args["segments"][0]["sales"] == 200 # 順序を入れ替えても累計が選ばれる

        args = m_write.call_args[1]
        assert args["segments"][0]["sales"] == 200 # 順序を入れ替えても累計が選ばれる

    def test_sync_retry_states(self, setup_db, monkeypatch):
        # 19. 通知保存済み・PL未保存ならPL同期を再実行
        # 20. 通知保存済み・セグメント未保存ならセグメント同期を再実行
        # 21. PL成功・セグメント失敗後の次回実行でセグメントだけ再試行
        # 22. canonical完全保存済みなら再同期をスキップ
        # 23. 同一source_row_keyで再試行しても重複行を作らない
        from src.events.earnings_production_pipeline import run_earnings_production

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")

        mock_client = MagicMock()
        mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client), \
             patch("src.events.earnings_production_pipeline._sync_canonical_financials") as m_fin, \
             patch("src.events.earnings_production_pipeline._sync_canonical_segments") as m_seg, \
             patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events") as m_save, \
             patch("src.events.earnings_production_pipeline._find_cached_xbrl", return_value="C:/xbrl_archive/20260709590505.zip"), \
             patch("src.events.earnings_production_pipeline.load_json") as m_load, \
             patch("src.segment.segment_zip_resolver.resolve_xbrl_zip") as m_resolve:

            from src.segment.segment_zip_resolver import ZipResolveResult
            m_resolve.return_value = ZipResolveResult(
                zip_path="C:/xbrl_archive/20260709590505.zip",
                source="tdnet_cache",
                status="FOUND_CACHE",
                error_reason="",
                cache_hit=True,
                downloaded=False,
                requested_disclosure_no="20260709590505",
                zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
                trusted_provenance=None,
                resolution_kind="exact_cache"
            )
            m_save.return_value = {"action": "dedup_skipped"}

            e = EarningsSummaryData(sales_current=123, op_current=45)
            m_load.return_value = {
                "earnings": dataclasses.asdict(e),
                "company_name": "Test Poplar",
                "fiscal_year": "2027",
                "quarter": "1Q",
            }

            doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
            doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"

            # 19/20. 同期が未保存なら重複検知されても再同期が走る
            run_earnings_production([doc], setup_db, webhook_url="")
            assert m_fin.call_count == 1
            assert m_seg.call_count == 1


class TestHTMLDecoder:
    def test_decode_utf8_japanese_html(self):
        # 29. UTF-8デコード既存テスト
        text = "<html><head><meta charset='utf-8'></head><body>アパレル・テキスタイル関連事業</body></html>"
        raw = text.encode("utf-8")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert "アパレル・テキスタイル関連事業" in decoded

    def test_decode_cp932_japanese_html(self):
        # 30. CP932デコード既存テスト
        text = "<html><head><meta charset='shift_jis'></head><body>アパレル・テキスタイル関連事業</body></html>"
        raw = text.encode("cp932")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert "アパレル・テキスタイル関連事業" in decoded

    def test_utf8_not_misidentified_as_cp932(self):
        text = "アパレル・テキスタイル関連事業"
        raw = text.encode("utf-8")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert decoded == text


class TestRealtimeSegmentIdentity:
    """Realtimeセグメント識別子分離・ZIP正本一致修正の検証テスト (Section 12要件に基づく)"""

    @pytest.fixture
    def setup_db(self):
        conn = sqlite3.connect(":memory:")
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        ensure_earnings_summary_table(conn)
        return conn

    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed")
    def test_common_function_scenarios(self, m_extract, m_cache, m_write, tmp_path):
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow
        import os

        # テスト用のZIPを作成
        zip_dir = tmp_path / "xbrl_cache"
        zip_dir.mkdir()
        zip_path = make_identity_test_zip(
            tmp_path=zip_dir,
            requested_disclosure_no="20260709590505",
            internal_document_id="20260709590505",
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            document_type="attachment_xbrl"
        )

        # extract_segments_from_xbrl_zip が返すモックデータ
        m_res = MagicMock()
        m_res.status = "success_with_rows"
        m_res.segments = [
            SegmentRawRow(
                source="xbrl",
                raw_ticker="7601",
                period="2027-02-28",
                quarter="1Q",
                raw_segment_name="Smartstore",
                sales=1242,
                profit=-90,
                raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31", "current_or_previous": "current"}}
            )
        ]
        m_extract.return_value = m_res
        m_write.return_value = {"written": 1, "skipped": 0, "errors": 0}

        # 1. 64桁filing_idがwriterへ渡る
        # 2. 14桁書類IDがZIP識別へ使われる (ZIP名の ID と common_disclosure_no の一致検証)
        # 3. 64桁IDがZIP検索へ渡らない (共通関数内で ZIP 名から 64 桁ハッシュを渡していないか検証)
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8", # 64桁
            common_disclosure_no="20260709590505", # 14桁
            xbrl_path=str(zip_path),
            dry_run=False,
            route="seq"
        )
        assert m_write.call_count == 1
        # call_argsの検証
        args, kwargs = m_write.call_args
        assert kwargs.get("filing_id") == "4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8"
        assert kwargs.get("filing_id") != "20260709590505"

        # 4. 不正な64桁filing_idを拒否 (不正な64桁 → 未呼び出し)
        m_write.reset_mock()
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="invalid_hash_value_12345", # 不正
            common_disclosure_no="20260709590505",
            xbrl_path=str(zip_path),
            dry_run=False,
            route="seq"
        )
        assert m_write.call_count == 0

        # 5. 不正な14桁書類IDを拒否 (数字以外など)
        m_write.reset_mock()
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8",
            common_disclosure_no="invalid_doc_id_", # 不正
            xbrl_path=str(zip_path),
            dry_run=False,
            route="seq"
        )
        assert m_write.call_count == 0

        # 6. ZIP書類ID不一致でwriter未呼び出し
        m_write.reset_mock()
        # zip_path を 9982 のものにする
        wrong_zip = make_identity_test_zip(
            tmp_path=zip_dir,
            requested_disclosure_no="20260709590450",
            internal_document_id="20260709590450",
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            document_type="attachment_xbrl"
        )
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8",
            common_disclosure_no="20260709590505", # 7601のIDを期待
            xbrl_path=str(wrong_zip), # 9982のZIP
            dry_run=False,
            route="seq"
        )
        assert m_write.call_count == 0

        # 7. ZIP未存在でwriter未呼び出し
        m_write.reset_mock()
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8",
            common_disclosure_no="20260709590505",
            xbrl_path="C:/non_existent_path.zip",
            dry_run=False,
            route="seq"
        )
        assert m_write.call_count == 0

        # 8. dry-runでwriter未呼び出し
        m_write.reset_mock()
        _sync_canonical_segments(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            canonical_filing_id="4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8",
            common_disclosure_no="20260709590505",
            xbrl_path=str(zip_path),
            dry_run=True, # dry-run
            route="seq"
        )
        assert m_write.call_count == 0


    def test_find_cached_xbrl_real_path_scenarios(self, tmp_path):
        from src.events.earnings_production_pipeline import _find_cached_xbrl

        # テスト用のダミーZIPファイル群の作成
        zip1 = tmp_path / "7601_20260709590505.zip"
        zip1.write_bytes(b"dummy")

        zip2 = tmp_path / "7601_20260709590450.zip"
        zip2.write_bytes(b"dummy")

        # 9. 一致ZIPが1件なら正しいパス
        res1 = _find_cached_xbrl(str(tmp_path), "7601", "20260709590505")
        assert res1 == str(zip1)

        # 10. 別書類IDZIPだけならNone (存在しないIDの指定)
        res2 = _find_cached_xbrl(str(tmp_path), "7601", "20260709590999")
        assert res2 is None

        # 11. ZIPなしならNone
        res3 = _find_cached_xbrl(str(tmp_path), "9982", "20260709590505")
        assert res3 is None

        # 12. 64桁ハッシュならNone (14桁ではないため弾かれること)
        res4 = _find_cached_xbrl(str(tmp_path), "7601", "4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8")
        assert res4 is None

        # 13. 不正値ならNone (文字混じりなど)
        res5 = _find_cached_xbrl(str(tmp_path), "7601", "invalid_no_1234")
        assert res5 is None

        # 14. 複数候補なら安全にNone (glob時に複数ヒットした場合)
        assert _find_cached_xbrl(str(tmp_path), "7601", "") is None


    def test_sequential_route_scenarios(self, setup_db, monkeypatch, tmp_path):
        from src.events.earnings_production_pipeline import run_earnings_production
        from src.events.earnings_production_pipeline import EarningsSummaryData
        import dataclasses

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")

        # 15. writerへ64桁filing_id
        # 16. ZIP検索へ14桁書類ID
        # 17. 9982相当の実引数
        # 18. 7601相当の実引数
        # 19. 14桁取得不能時にwriter未呼び出し

        mock_client = MagicMock()
        mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client), \
             patch("src.events.earnings_production_pipeline._find_cached_xbrl") as m_find, \
             patch("src.events.earnings_production_pipeline._sync_canonical_segments") as m_seg, \
             patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events") as m_save, \
             patch("src.events.earnings_production_pipeline.load_json") as m_load, \
             patch("src.events.earnings_production_pipeline._extract_expected_segment_names_from_xbrl", return_value=["Smartstore"]), \
             patch("os.path.exists", return_value=True):

            # テスト用の 9982 ZIP と 7601 ZIP を作成
            zip_9982 = make_identity_test_zip(
                tmp_path=tmp_path,
                requested_disclosure_no="20260709590450",
                internal_document_id="20260709590450",
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                document_type="attachment_xbrl"
            )
            zip_7601 = make_identity_test_zip(
                tmp_path=tmp_path,
                requested_disclosure_no="20260709590505",
                internal_document_id="20260709590505",
                ticker="7601",
                period="2027-02-28",
                quarter="1Q",
                document_type="attachment_xbrl"
            )
            
            def find_side_effect(xbrl_dir, ticker, doc_id=""):
                if ticker == "9982":
                    return str(zip_9982)
                return str(zip_7601)
            m_find.side_effect = find_side_effect

            from src.segment.segment_zip_resolver import ZipResolveResult
            def resolve_side_effect(doc_id, ticker, expected_quarter="", expected_period="", persist_provenance=True):
                z_path = str(zip_9982) if ticker == "9982" else str(zip_7601)
                sha = "719f1592f98cd05c2a60601726e3635f5495af899c188693f85ce487dae0a5b5" if ticker == "9982" else "c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4"
                return ZipResolveResult(
                    zip_path=z_path,
                    source="tdnet_cache",
                    status="FOUND_CACHE",
                    error_reason="",
                    cache_hit=True,
                    downloaded=False,
                    requested_disclosure_no=doc_id,
                    zip_sha256=sha,
                    trusted_provenance=None,
                    resolution_kind="exact_cache"
                )
            
            # autouse mock を一時的に上書きするための patch
            m_res_patch = patch("src.segment.segment_zip_resolver.resolve_xbrl_zip", side_effect=resolve_side_effect)
            m_res_patch.start()
            
            m_save.return_value = {"action": "dedup_skipped"}

            # 17. 9982相当の実引数の検証
            # 64桁filing_id
            h_9982 = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
            doc_9982 = DummyDoc("9982", "2027年2月期 第1四半期決算短信［日本基準］(連結)", h_9982)
            doc_9982.doc_url = "https://www.release.tdnet.info/inbs/140120260709590450.pdf" # 14桁ID: 20260709590450
            doc_9982.doc_id = "20260709590450"

            e = EarningsSummaryData(sales_current=15388, op_current=447)
            m_load.return_value = {
                "earnings": dataclasses.asdict(e),
                "company_name": "タキヒヨー",
                "fiscal_year": "2027",
                "quarter": "1Q",
            }

            run_earnings_production([doc_9982], setup_db, webhook_url="")

            # _find_cached_xbrl へ渡った実値を call_args で検証 (16. ZIP検索へ14桁書類ID)
            m_find.assert_called_with(ANY, "9982", doc_id="20260709590450")
            # 64桁ハッシュ値が _find_cached_xbrl に渡っていないことを検証
            for call in m_find.call_args_list:
                _, kwargs = call
                assert kwargs.get("doc_id") != h_9982

            # _sync_canonical_segments へ渡った実値を検証 (15. writerへ64桁、17. 9982相当)
            m_seg.assert_called_with(
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                canonical_filing_id=h_9982,
                common_disclosure_no="20260709590450",
                xbrl_path=ANY,
                dry_run=False,
                route="sequential",
                target_segs=ANY,
                trusted_provenance=None
            )

            # 18. 7601相当の実引数の検証
            m_find.reset_mock()
            m_seg.reset_mock()

            h_7601 = "4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8"
            doc_7601 = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", h_7601)
            doc_7601.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf" # 14桁ID: 20260709590505
            doc_7601.doc_id = "20260709590505"

            run_earnings_production([doc_7601], setup_db, webhook_url="")
            m_find.assert_called_with(ANY, "7601", doc_id="20260709590505")
            m_seg.assert_called_with(
                ticker="7601",
                period="2027-02-28",
                quarter="1Q",
                canonical_filing_id=h_7601,
                common_disclosure_no="20260709590505",
                xbrl_path=ANY,
                dry_run=False,
                route="sequential",
                target_segs=ANY,
                trusted_provenance=None
            )

            # 19. 14桁取得不能時にwriter未呼び出し (URLや明示IDを空にする)
            m_find.reset_mock()
            m_seg.reset_mock()

            doc_no_id = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", h_7601)
            doc_no_id.doc_url = ""
            doc_no_id.doc_id = "" # 空

            run_earnings_production([doc_no_id], setup_db, webhook_url="")
            # 14桁解決できず、_find_cached_xbrl への doc_id が空になることを検証
            m_find.assert_called_with(ANY, "7601", doc_id="")
            m_res_patch.stop()


    def test_subprocess_route_scenarios(self, setup_db, monkeypatch, tmp_path):
        from src.events.earnings_production_pipeline import run_earnings_production
        from src.events.earnings_production_pipeline import EarningsSummaryData
        import dataclasses

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "9982,7601")

        mock_client = MagicMock()
        mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        # 20. writerへ64桁filing_id
        # 21. ZIP検索へ14桁書類ID
        # 22. source_doc_idが64桁でもZIP検索へ流用しない
        # 23. URLから14桁番号を正しく取得
        # 24. 14桁取得不能時にwriter未呼び出し

        with patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run") as m_run, \
             patch("src.events.earnings_subprocess_runner.build_save_ready_payload") as m_payload, \
             patch("src.events.earnings_subprocess_runner.validate_save_ready_payload") as m_valid, \
             patch("src.events.earnings_subprocess_runner.build_save_call_plan") as m_plan, \
             patch("src.events.earnings_subprocess_runner.validate_save_call_plan") as m_cp_valid, \
             patch("src.events.earnings_subprocess_runner.build_discord_call_plan") as m_discord_plan, \
             patch("src.events.tdnet_event_store.save_event_to_supabase") as m_supa, \
             patch("src.events.earnings_production_pipeline._find_cached_xbrl") as m_find, \
             patch("src.events.earnings_production_pipeline._sync_canonical_segments") as m_seg, \
             patch("src.events.earnings_production_pipeline._extract_expected_segment_names_from_xbrl", return_value=["Smartstore"]), \
             patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client), \
             patch("os.path.exists", return_value=True):

            h_9982 = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
            m_run.return_value = {"results": [{"ticker": "9982", "status": "ok"}]}
            m_valid.return_value = (True, "")
            m_cp_valid.return_value = (True, "")
            m_discord_plan.return_value = {"discord_message": "dummy msg"}
            # テスト用の 9982 ZIP と 7601 ZIP を作成
            zip_9982 = make_identity_test_zip(
                tmp_path=tmp_path,
                requested_disclosure_no="20260709590450",
                internal_document_id="20260709590450",
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                document_type="attachment_xbrl"
            )
            zip_7601 = make_identity_test_zip(
                tmp_path=tmp_path,
                requested_disclosure_no="20260709590505",
                internal_document_id="20260709590505",
                ticker="7601",
                period="2027-02-28",
                quarter="1Q",
                document_type="attachment_xbrl"
            )
            
            def find_side_effect(xbrl_dir, ticker, doc_id=""):
                if ticker == "9982":
                    return str(zip_9982)
                return str(zip_7601)
            m_find.side_effect = find_side_effect

            from src.segment.segment_zip_resolver import ZipResolveResult
            def resolve_side_effect(doc_id, ticker, expected_quarter="", expected_period="", persist_provenance=True):
                z_path = str(zip_9982) if ticker == "9982" else str(zip_7601)
                sha = "719f1592f98cd05c2a60601726e3635f5495af899c188693f85ce487dae0a5b5" if ticker == "9982" else "c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4"
                return ZipResolveResult(
                    zip_path=z_path,
                    source="tdnet_cache",
                    status="FOUND_CACHE",
                    error_reason="",
                    cache_hit=True,
                    downloaded=False,
                    requested_disclosure_no=doc_id,
                    zip_sha256=sha,
                    trusted_provenance=None,
                    resolution_kind="exact_cache"
                )
            
            m_res_patch = patch("src.segment.segment_zip_resolver.resolve_xbrl_zip", side_effect=resolve_side_effect)
            m_res_patch.start()

            m_supa.return_value = {"action": "inserted"}

            # payload
            e = EarningsSummaryData(sales_current=15388, op_current=447)
            m_payload.return_value = {
                "extracted": {
                    "ticker": "9982",
                    "period": "2027-02-28",
                    "quarter": "1Q",
                    "guidance": {}
                }
            }
            m_plan.return_value = {
                "earnings_summary_args": {
                    "ticker": "9982",
                    "fiscal_year": "2027",
                    "quarter": "1Q",
                    "sales_value": 15388,
                    "op_value": 447,
                    "title": "2027年2月期 第1四半期決算短信［日本基準］(連結)",
                    "fingerprint": "dummy_fingerprint_9982",
                },
                "tdnet_event_payload": {
                    "ticker": "9982",
                    "source_doc_id": h_9982 # 64桁
                }
            }

            doc_9982 = DummyDoc("9982", "2027年2月期 第1四半期決算短信［日本基準］(連結)", h_9982)
            doc_9982.doc_url = "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
            doc_9982.doc_id = "20260709590450"

            run_earnings_production([doc_9982], setup_db, webhook_url="")

            # 21. ZIP検索へ14桁書類ID (22. source_doc_id 64桁を ZIP 検索に流用しないことの検証)
            m_find.assert_called_with(ANY, "9982", doc_id="20260709590450")
            for call in m_find.call_args_list:
                _, kwargs = call
                assert kwargs.get("doc_id") != h_9982

            # 20. writerへ64桁filing_id
            m_seg.assert_called_with(
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                canonical_filing_id=h_9982,
                common_disclosure_no="20260709590450",
                xbrl_path=ANY,
                dry_run=False,
                route="subprocess",
                target_segs=ANY,
                trusted_provenance=None
            )

            # 24. 14桁取得不能時にwriter未呼び出し
            m_find.reset_mock()
            m_seg.reset_mock()

            doc_no_id = DummyDoc("9982", "2027年2月期 第1四半期決算短信［日本基準］(連結)", h_9982)
            doc_no_id.doc_url = ""
            doc_no_id.doc_id = ""

            run_earnings_production([doc_no_id], setup_db, webhook_url="")
            m_find.assert_called_with(ANY, "9982", doc_id="")
            m_res_patch.stop()


class TestCanonicalCompletionStrictness:
    """Phase 3 canonical保存完了判定の開示単位・指標単位厳格化の検証テスト"""

    # ──── PL 完了判定の 10 件の必須テスト ────
    def test_pl_case1_both_exist(self):
        # 1. 今回のfiling_idでsales・operating_profitが両方存在 -> 完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}, {"metric": "operating_profit"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is True

    def test_pl_case2_sales_only(self):
        # 2. 今回のfiling_idでsalesだけ存在 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is False

    def test_pl_case3_op_only(self):
        # 3. 今回のfiling_idでoperating_profitだけ存在 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "operating_profit"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is False

    def test_pl_case4_other_filing_id_only(self):
        # 4. 別filing_idで両方存在 -> 未完了 (DB検索のeq("filing_id")条件により空が返る)
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        # 今回の filing_id の検索結果は空にする
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is False

    def test_pl_case5_mixed_filing_id(self):
        # 5. 別filing_idと今回filing_idの行が混在 -> 今回filing_idの全期待metricがなければ未完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        # DBからは今回filing_idに紐づくデータ（salesのみ）が返る
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is False

    def test_pl_case6_single_expected_metric(self):
        # 6. 非nullの期待metricが1種類だけ -> その1種類が存在すれば完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales"]) is True

    def test_pl_case7_empty_expected_metrics(self):
        # 7. 期待metric集合が空 -> 未完了 (仕様: 空の場合は完了扱いにしない)
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, []) is False

    def test_pl_case8_invalid_filing_id(self):
        # 8. filing_idが空または不正 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", "", ["sales"]) is False
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", "invalid_length_123", ["sales"]) is False

    def test_pl_case9_order_independence(self):
        # 9. DB取得順序を反転 -> 結果不変
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        fid = "a" * 64

        # 順序1
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "operating_profit"}, {"metric": "sales"}
        ]
        res1 = _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"])

        # 順序2
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}, {"metric": "operating_profit"}
        ]
        res2 = _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"])

        assert res1 is True and res2 is True

    def test_pl_case10_metric_duplicates(self):
        # 10. 同一metricの重複行 -> 期待集合判定は正しく完了
        from src.events.earnings_production_pipeline import _check_canonical_financials_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"metric": "sales"}, {"metric": "sales"}, {"metric": "operating_profit"}
        ]
        fid = "a" * 64
        assert _check_canonical_financials_saved(client, "7601", "2027-02-28", "1Q", fid, ["sales", "operating_profit"]) is True

    # ──── セグメント完了判定の 13 件の必須テスト ────
    def test_seg_case1_all_exist(self):
        # 1. 今回のfiling_idで全(segment_key, metric)が存在 -> 完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"},
            {"segment_key": "apparel", "metric": "operating_profit"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales"), ("apparel", "operating_profit")]) is True

    def test_seg_case2_sales_missing(self):
        # 2. 1つのsegmentのsalesだけ欠落 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "operating_profit"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales"), ("apparel", "operating_profit")]) is False

    def test_seg_case3_op_missing(self):
        # 3. 1つのsegment of operating_profitだけ欠落 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales"), ("apparel", "operating_profit")]) is False

    def test_seg_case4_segment_key_mismatch(self):
        # 4. segment_nameだけ一致しsegment_keyが不一致 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "other_key", "metric": "sales"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales")]) is False

    def test_seg_case5_other_filing_id_only(self):
        # 5. 別filing_idに全行が存在 -> 未完了 (eq("filing_id")条件により空)
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales")]) is False

    def test_seg_case6_partial_segments_exist(self):
        # 6. 今回filing_idに一部segmentだけ存在 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"}
        ]
        fid = "b" * 64
        expected = [("apparel", "sales"), ("rental", "sales")]
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected) is False

    def test_seg_case7_sales_only_expected(self):
        # 7. salesのみを期待するsegment -> salesが存在すれば、そのsegmentは充足
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "sales")]) is True

    def test_seg_case8_op_only_expected(self):
        # 8. operating_profitのみを期待するsegment -> operating_profitが存在すれば、そのsegmentは充足
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "operating_profit"}
        ]
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, [("apparel", "operating_profit")]) is True

    def test_seg_case9_empty_expected(self):
        # 9. 期待集合が空 -> 未完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        fid = "b" * 64
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, []) is False

    def test_seg_case10_order_independence(self):
        # 10. DB取得順序を反転 -> 結果不変
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        fid = "b" * 64
        expected = [("apparel", "sales"), ("apparel", "operating_profit")]

        # 順序1
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "operating_profit"},
            {"segment_key": "apparel", "metric": "sales"}
        ]
        res1 = _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected)

        # 順序2
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"},
            {"segment_key": "apparel", "metric": "operating_profit"}
        ]
        res2 = _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected)
        assert res1 is True and res2 is True

    def test_seg_case11_duplicates(self):
        # 11. 同一(segment_key, metric)の重複行 -> 期待集合判定は正しく完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel", "metric": "sales"},
            {"segment_key": "apparel", "metric": "sales"},
            {"segment_key": "apparel", "metric": "operating_profit"}
        ]
        fid = "b" * 64
        expected = [("apparel", "sales"), ("apparel", "operating_profit")]
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected) is True

    def test_seg_case12_poplar_scenarios(self):
        # 12. Poplar(7601)相当の複数segment・2metric -> 全行が揃う場合だけ完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        fid = "b" * 64
        expected = [
            ("retail", "sales"), ("retail", "operating_profit"),
            ("rental", "sales"), ("rental", "operating_profit")
        ]
        # 不足状態 (rental の profit がない)
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "retail", "metric": "sales"},
            {"segment_key": "retail", "metric": "operating_profit"},
            {"segment_key": "rental", "metric": "sales"}
        ]
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected) is False

        # 揃った状態
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data.append(
            {"segment_key": "rental", "metric": "operating_profit"}
        )
        assert _check_canonical_segments_saved(client, "7601", "2027-02-28", "1Q", fid, expected) is True

    def test_seg_case13_takihyo_scenarios(self):
        # 13. Takihyo(9982)相当の複数segment・2metric -> 全行が揃う場合だけ完了
        from src.events.earnings_production_pipeline import _check_canonical_segments_saved
        client = MagicMock()
        fid = "b" * 64
        expected = [
            ("apparel_wholesale", "sales"), ("apparel_wholesale", "operating_profit"),
            ("rental", "sales"), ("rental", "operating_profit")
        ]
        # 揃った状態
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"segment_key": "apparel_wholesale", "metric": "sales"},
            {"segment_key": "apparel_wholesale", "metric": "operating_profit"},
            {"segment_key": "rental", "metric": "sales"},
            {"segment_key": "rental", "metric": "operating_profit"}
        ]
        assert _check_canonical_segments_saved(client, "9982", "2027-02-28", "1Q", fid, expected) is True

    # ──── キー一致・期待集合検証テスト ────
    def test_key_alignment_scenarios(self):
        from src.events.earnings_production_pipeline import _build_expected_segment_metrics_from_canonical_rows
        from lib.pipeline.canonical_writer import expand_segments_rows

        fid = "a" * 64

        # 1. アパレル事業 (期待キーと writer 生成キーが完全一致)
        segs1 = [{"segment_name": "アパレル事業", "sales": 100, "profit": 10}]
        exp1 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs1, "xbrl", fid)
        rows1, _ = expand_segments_rows(ticker="7601", period="2027-02-28", quarter="1Q", segments=segs1, source="xbrl", filing_id=fid, unit="millions_jpy")
        assert len(exp1) == 2
        for r in rows1:
            assert (r["segment_key"], r["metric"]) in exp1

        # 2. ローソン・ポプラ事業 (記号を含む場合)
        segs2 = [{"segment_name": "ローソン・ポプラ事業", "sales": 200, "profit": 20}]
        exp2 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs2, "xbrl", fid)
        rows2, _ = expand_segments_rows(ticker="7601", period="2027-02-28", quarter="1Q", segments=segs2, source="xbrl", filing_id=fid, unit="millions_jpy")
        assert len(exp2) == 2
        for r in rows2:
            assert (r["segment_key"], r["metric"]) in exp2

        # 3. 英語セグメント名 (大文字・空白を含む場合)
        segs3 = [{"segment_name": "Apparel And Textile Sector", "sales": 300, "profit": 30}]
        exp3 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs3, "xbrl", fid)
        rows3, _ = expand_segments_rows(ticker="7601", period="2027-02-28", quarter="1Q", segments=segs3, source="xbrl", filing_id=fid, unit="millions_jpy")
        assert len(exp3) == 2
        for r in rows3:
            assert (r["segment_key"], r["metric"]) in exp3

        # 4. metricがsalesとprofitの両方存在する場合
        assert ("アパレル事業", "sales") in exp1
        assert ("アパレル事業", "profit") in exp1

        # 5. salesだけ非null
        segs5 = [{"segment_name": "アパレル事業", "sales": 100, "profit": None}]
        exp5 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs5, "xbrl", fid)
        assert len(exp5) == 1
        assert ("アパレル事業", "sales") in exp5
        assert ("アパレル事業", "profit") not in exp5

        # 6. profitだけ非null
        segs6 = [{"segment_name": "アパレル事業", "sales": None, "profit": 10}]
        exp6 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs6, "xbrl", fid)
        assert len(exp6) == 1
        assert ("アパレル事業", "sales") not in exp6
        assert ("アパレル事業", "profit") in exp6

        # 7. 両方null
        segs7 = [{"segment_name": "アパレル事業", "sales": None, "profit": None}]
        exp7 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs7, "xbrl", fid)
        assert len(exp7) == 0

        # 8. 重複する保存予定行
        segs8 = [
            {"segment_name": "アパレル事業", "sales": 100, "profit": 10},
            {"segment_name": "アパレル事業", "sales": 100, "profit": 10},
        ]
        exp8 = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs8, "xbrl", fid)
        assert len(exp8) == 2  # 重複排除されている

    def test_key_real_poplar_scenarios(self):
        from src.events.earnings_production_pipeline import _build_expected_segment_metrics_from_canonical_rows
        from lib.pipeline.canonical_writer import expand_segments_rows
        fid = "a" * 64
        segs = [
            {"segment_name": "Smartstore", "sales": 500, "profit": 50},
            {"segment_name": "Lawson Poplar", "sales": 300, "profit": 30},
            {"segment_name": "Other", "sales": 50, "profit": 5},
        ]
        exp = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", segs, "xbrl", fid)
        rows, _ = expand_segments_rows(ticker="7601", period="2027-02-28", quarter="1Q", segments=segs, source="xbrl", filing_id=fid, unit="millions_jpy")
        assert len(exp) == 6
        for r in rows:
            assert (r["segment_key"], r["metric"]) in exp

    def test_key_real_takihyo_scenarios(self):
        from src.events.earnings_production_pipeline import _build_expected_segment_metrics_from_canonical_rows
        from lib.pipeline.canonical_writer import expand_segments_rows
        fid = "a" * 64
        segs = [
            {"segment_name": "Apparel And Textile", "sales": 1000, "profit": 100},
            {"segment_name": "Rental Business", "sales": 400, "profit": 40},
            {"segment_name": "Material", "sales": 200, "profit": 20},
            {"segment_name": "Other", "sales": 100, "profit": 10},
        ]
        exp = _build_expected_segment_metrics_from_canonical_rows("9982", "2027-02-28", "1Q", segs, "xbrl", fid)
        rows, _ = expand_segments_rows(ticker="9982", period="2027-02-28", quarter="1Q", segments=segs, source="xbrl", filing_id=fid, unit="millions_jpy")
        assert len(exp) == 8
        for r in rows:
            assert (r["segment_key"], r["metric"]) in exp

    # ──── 呼出元個別検証テスト ────
    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_caller_pl_call_args(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_fin, m_check_seg,
        m_supa, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        # モック設定
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        m_find.return_value = "C:/xbrl_cache/7601_20260709590505.zip"
        m_check_fin.return_value = True
        m_check_seg.return_value = True
        m_supa.return_value = {"action": "inserted"}

        # subprocess runner モック設定
        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": "2027年2月期 第1四半期決算短信",
                "fingerprint": "dummy_fingerprint_7601",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信", h_64)
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "7601")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        run_earnings_production([doc], conn, webhook_url="")

        assert m_check_fin.call_count == 1
        args, kwargs = m_check_fin.call_args
        assert kwargs.get("filing_id") == h_64
        assert kwargs.get("filing_id") != "20260709590505"  # 14桁書類IDが渡らない

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_caller_segment_call_args(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_fin, m_check_seg,
        m_supa, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        # モック設定
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        m_find.return_value = "C:/xbrl_cache/7601_20260709590505.zip"
        m_check_fin.return_value = True
        m_check_seg.return_value = True
        m_supa.return_value = {"action": "inserted"}
        m_extract_and_filter.return_value = [
            {"segment_name": "アパレル事業", "sales": 100, "profit": 10}
        ]

        # subprocess runner モック設定
        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": "2027年2月期 第1四半期決算短信",
                "fingerprint": "dummy_fingerprint_7601",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信", h_64)
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "7601")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        run_earnings_production([doc], conn, webhook_url="")

        assert m_check_seg.call_count == 1
        args, kwargs = m_check_seg.call_args
        assert kwargs.get("filing_id") == h_64
        assert kwargs.get("filing_id") != "20260709590505"  # 14桁書類IDが渡らない
        # キー一致の検証 (writerが保存するキーアパレル事業になっていること)
        assert set(kwargs.get("expected_segment_metrics")) == {("アパレル事業", "sales"), ("アパレル事業", "profit")}

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_caller_subprocess_route(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_sync_seg,
        m_supa, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        # モック設定
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        m_find.return_value = "C:/xbrl_cache/7601_20260709590505.zip"
        m_check_seg.return_value = False  # 同期処理を実行させる
        m_supa.return_value = {"action": "inserted"}
        target_segs = [{"segment_name": "アパレル事業", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        # subprocess runner モック設定
        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": "2027年2月期 第1四半期決算短信",
                "fingerprint": "dummy_fingerprint_7601",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信", h_64)
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "7601")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        run_earnings_production([doc], conn, webhook_url="")

        # 完了判定の target_segs と、同期処理に渡った target_segs が同一内容であることを call_args で検証
        assert m_check_seg.call_count == 1
        _, check_kwargs = m_check_seg.call_args

        assert m_sync_seg.call_count == 1
        _, sync_kwargs = m_sync_seg.call_args

        # 同期の target_segs が正しいことの検証
        assert sync_kwargs.get("target_segs") == target_segs

        # 完了判定用の expected_segment_metrics が、同期に渡した target_segs からヘルパーで一貫して構築されたものであることの検証
        from src.events.earnings_production_pipeline import _build_expected_segment_metrics_from_canonical_rows
        expected = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", target_segs, "xbrl", h_64)
        assert set(check_kwargs.get("expected_segment_metrics")) == expected

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_caller_sequential_route(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_sync_seg,
        m_supa, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        # モック設定
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        m_find.return_value = "C:/xbrl_cache/7601_20260709590505.zip"
        m_check_seg.return_value = False  # 同期処理を実行させる
        m_supa.return_value = {"action": "inserted"}
        target_segs = [{"segment_name": "\u30a2\u30d1\u30ec\u30eb\u4e8b\u696d", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        # subprocess runner モック設定 (シーケンシャルルートでも一応セットしておく)
        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": "2027\u5e742\u6708\u671f \u7b2c1\u56db\u534a\u671f\u6c7a\u7b97\u77ed\u4fe1",
                "fingerprint": "dummy_fingerprint_7601",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        doc = DummyDoc("7601", "2027\u5e742\u6708\u671f \u7b2c1\u56db\u534a\u671f\u6c7a\u7b97\u77ed\u4fe1", h_64)
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        # シーケンシャルルート（非サブプロセス）で実行
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "")
        run_earnings_production([doc], conn, webhook_url="")

        # 完了判定の target_segs と、同期処理に渡った target_segs が同一内容であることを call_args で検証
        assert m_check_seg.call_count == 1
        _, check_kwargs = m_check_seg.call_args

        assert m_sync_seg.call_count == 1
        _, sync_kwargs = m_sync_seg.call_args

        # 同期の target_segs が正しいことの検証
        assert sync_kwargs.get("target_segs") == target_segs

        # 完了判定用の expected_segment_metrics が、同期に渡した target_segs からヘルパーで一貫して構築されたものであることの検証
        from src.events.earnings_production_pipeline import _build_expected_segment_metrics_from_canonical_rows
        expected = _build_expected_segment_metrics_from_canonical_rows("7601", "2027-02-28", "1Q", target_segs, "xbrl", h_64)
        assert set(check_kwargs.get("expected_segment_metrics")) == expected


class TestCanonicalRetryOnDuplicate:
    """Phase 4: 重複スキップ時の canonical 不足分限定再同期の独立検証"""

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_and_segment_both_complete(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = True
        m_check_seg.return_value = True
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        assert m_sync_pl.call_count == 0
        assert m_sync_seg.call_count == 0

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_incomplete_seg_complete(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, True]
        m_check_seg.return_value = True
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        assert m_sync_pl.call_count == 1
        assert m_sync_seg.call_count == 0

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_complete_seg_incomplete(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = True
        m_check_seg.side_effect = [False, True]
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        assert m_sync_pl.call_count == 0
        assert m_sync_seg.call_count == 1

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_zip_missing_seg_skipped_pl_continues(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, True]
        m_check_seg.return_value = False
        m_find.return_value = None

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=None,
            pl_values=pl_vals,
            dry_run=False,
            target_segs=None,
        )
        assert m_sync_pl.call_count == 1
        assert m_sync_seg.call_count == 0

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._retry_incomplete_canonical_for_duplicate")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("os.path.exists", return_value=True)
    def test_subprocess_route_retry_calling_and_exact_gate(
        self, m_exists, m_get_supabase, m_retry, m_supa_save, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        # \u6c7a\u7b97\u77ed\u4fe1 = 決算短信
        title = "2027 Q1 Earning Report " + "\u6c7a\u7b97\u77ed\u4fe1"
        doc = DummyDoc("7601", title, h_64)
        doc.published_at = "2026-07-09T15:00:00Z"
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        from src.events.earnings_summary_storage import save_earnings_summary
        existing_data = {
            "ticker": "7601",
            "company_name": "Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
            "title": doc.title,
            "disclosure_date": "2026-07-09",
            "fingerprint": "existing_fp",
        }
        save_earnings_summary(conn, existing_data)

        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client

        mock_select = mock_client.table.return_value.select
        mock_eq = mock_select.return_value.eq
        mock_exec = mock_eq.return_value.execute
        mock_exec.return_value.data = [{"id": "some_existing_event_id"}]

        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": doc.title,
                "fingerprint": "dummy_fingerprint_7601",
                "disclosure_date": "2026-07-09",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "7601")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        run_earnings_production([doc], conn, webhook_url="")

        assert m_retry.call_count == 1
        assert m_supa_save.call_count == 0

        # --- シナリオ2: Exact Gate 不一致 (既存通知が存在しない場合、再同期しない) ---
        m_retry.reset_mock()
        mock_exec.return_value.data = []

        run_earnings_production([doc], conn, webhook_url="")
        assert m_retry.call_count == 0

        # --- シナリオ3: Exact Gate select エラー (fail-closed, 再同期しない) ---
        m_retry.reset_mock()
        mock_exec.side_effect = Exception("Supabase read error")

        run_earnings_production([doc], conn, webhook_url="")
        assert m_retry.call_count == 0

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._retry_incomplete_canonical_for_duplicate")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.earnings_production_pipeline.load_json")
    @patch("os.path.exists", return_value=True)
    def test_sequential_route_retry_calling_and_side_effects(
        self, m_exists, m_load_json, m_find, m_get_supabase, m_retry, m_supa_save, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"

        # 1. docを最優先で初期化し、日本語タイトルを一箇所に限定（エスケープ化）
        title = "2027 Q1 Earning Report " + "\u6c7a\u7b97\u77ed\u4fe1"
        doc = DummyDoc("7601", title, h_64)
        doc.published_at = "2026-07-09T15:00:00Z"
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        from src.events.earnings_summary_storage import save_earnings_summary
        from src.events.earnings_production_pipeline import _compute_earnings_fingerprint
        fp = _compute_earnings_fingerprint("7601", doc.title, h_64)

        existing_data = {
            "ticker": "7601",
            "company_name": "Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
            "title": doc.title,
            "disclosure_date": "2026-07-09",
            "fingerprint": fp,
        }
        save_earnings_summary(conn, existing_data)

        count_before = conn.execute("SELECT count(*) FROM earnings_summaries").fetchone()[0]

        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": doc.title,
                "fingerprint": fp,
                "disclosure_date": "2026-07-09",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        # _find_cached_xbrl をモック化してダミーパスを返しダウンロード処理をバイパス
        m_find.return_value = "C:/xbrl_cache/7601_20260709590505.zip"

        # load_json をモック化してダミーのパース済み結果を返し重い抽出処理をバイパス
        cached_parsed = {
            "earnings": {
                "sales_current": 110,
                "sales_prior": 100,
                "op_current": 120,
                "op_prior": 100,
                "sales_q_current": 110,
                "sales_q_prior": 100,
                "op_q_current": 120,
                "op_q_prior": 100,
            },
            "company_name": "Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
            "summary_line": "sales 500M (+10%), op 50M (+20%)",
            "segment_lines": [],
            "company_reasons": [],
            "segment_reasons": [],
            "full_message": "dummy message",
            "guidance": {},
            "is_4q": False,
            "fy_reason": "",
        }
        m_load_json.return_value = cached_parsed

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "")
        run_earnings_production([doc], conn, webhook_url="")

        assert m_retry.call_count == 1

        count_after = conn.execute("SELECT count(*) FROM earnings_summaries").fetchone()[0]
        assert count_before == count_after
        assert m_supa_save.call_count == 0

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_values_empty(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {}

        m_check_seg.side_effect = [False, True]
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_pl.assert_not_called()
        m_sync_seg.assert_called_once()
        m_check_pl.assert_not_called()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_still_incomplete(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, False]
        m_check_seg.return_value = True

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=None,
        )
        m_sync_pl.assert_called_once()
        assert m_check_pl.call_count == 2
        m_sync_seg.assert_not_called()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_segment_still_incomplete(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = True
        m_check_seg.side_effect = [False, False]
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_seg.assert_called_once()
        assert m_check_seg.call_count == 2
        m_sync_pl.assert_not_called()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_dry_run(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = False
        m_check_seg.return_value = False
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=True,
            target_segs=target_segs,
        )
        m_sync_pl.assert_not_called()
        m_sync_seg.assert_not_called()
        assert m_check_pl.call_count == 1
        assert m_check_seg.call_count == 1

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_zip_id_mismatch(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "99999999999999")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, True]
        m_check_seg.return_value = False

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=None,
        )
        m_sync_pl.assert_called_once()
        m_sync_seg.assert_not_called()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_segment_extraction_empty(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = True
        m_check_seg.return_value = False
        m_extract_and_filter.return_value = []

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=None,
        )
        m_sync_seg.assert_not_called()
        m_extract_and_filter.assert_called_once()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_target_segs_reused(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.return_value = True
        m_check_seg.side_effect = [False, True]
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_extract_and_filter.assert_not_called()
        m_sync_seg.assert_called_once()
        passed_segs = m_sync_seg.call_args[1].get("target_segs")
        assert passed_segs is target_segs

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_pl_exception_segment_continues(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, True]
        m_sync_pl.side_effect = Exception("PL Sync error")
        m_check_seg.side_effect = [False, True]
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_pl.assert_called_once()
        m_sync_seg.assert_called_once()
        assert m_check_seg.call_count == 2

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_segment_exception_safe_exit(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "a" * 64
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 100, "op": 10}

        m_check_pl.side_effect = [False, True]
        m_check_seg.side_effect = [False, True]
        m_sync_seg.side_effect = Exception("Segment Sync Error")
        target_segs = [{"segment_name": "Apparel", "sales": 100, "profit": 10}]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_pl.assert_called_once()
        m_sync_seg.assert_called_once()

    @patch("src.events.earnings_subprocess_runner.build_discord_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.build_save_call_plan")
    @patch("src.events.earnings_subprocess_runner.validate_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.build_save_ready_payload")
    @patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run")
    @patch("src.events.tdnet_event_store.save_event_to_supabase")
    @patch("src.events.earnings_production_pipeline._retry_incomplete_canonical_for_duplicate")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("os.path.exists", return_value=True)
    def test_strict_side_effects_zero_subprocess(
        self, m_exists, m_get_supabase, m_retry, m_supa_save, m_run, m_payload, m_valid, m_plan, m_cp_valid, m_discord_plan, monkeypatch
    ):
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        from tests.test_earnings_canonical_sync import DummyDoc

        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        h_64 = "c1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        title = "2027 Q1 Earning Report " + "\u6c7a\u7b97\u77ed\u4fe1"
        doc = DummyDoc("7601", title, h_64)
        doc.published_at = "2026-07-09T15:00:00Z"
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
        doc.xbrl_url = "https://www.release.tdnet.info/xbrl/140120260709590505.zip"
        doc.pdf_url = ""
        doc.doc_id = "20260709590505"
        doc.source_doc_id = h_64

        from src.events.earnings_summary_storage import save_earnings_summary
        existing_data = {
            "ticker": "7601",
            "company_name": "Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
            "title": doc.title,
            "disclosure_date": "2026-07-09",
            "fingerprint": "existing_fp",
        }
        save_earnings_summary(conn, existing_data)

        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client

        mock_select = mock_client.table.return_value.select
        mock_eq = mock_select.return_value.eq
        mock_exec = mock_eq.return_value.execute
        mock_exec.return_value.data = [{"id": "some_existing_event_id"}]

        m_run.return_value = {"results": [{"ticker": "7601", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_discord_plan.return_value = {"discord_message": "dummy msg"}
        m_payload.return_value = {
            "extracted": {
                "ticker": "7601",
                "period": "2027-02-28",
                "quarter": "1Q",
                "guidance": {}
            }
        }
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "7601",
                "fiscal_year": "2027",
                "quarter": "1Q",
                "sales_value": 500_000_000,
                "op_value": 50_000_000,
                "title": doc.title,
                "fingerprint": "dummy_fingerprint_7601",
                "disclosure_date": "2026-07-09",
            },
            "tdnet_event_payload": {
                "ticker": "7601",
                "source_doc_id": h_64
            }
        }

        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "1")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ALLOWLIST", "7601")
        monkeypatch.setenv("EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE", "1")
        run_earnings_production([doc], conn, webhook_url="")

        m_supa_save.assert_not_called()
        mock_client.table.return_value.insert.assert_not_called()
        mock_client.table.return_value.update.assert_not_called()
        mock_client.table.return_value.upsert.assert_not_called()

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_9982_model(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590450")
        m_find.return_value = str(zip_path)

        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        disclosure_no = "20260709590450"
        pl_vals = {"sales": 17656, "op": 815}

        m_check_pl.return_value = True
        m_check_seg.side_effect = [False, True]
        target_segs = [
            {"segment_name": "SegA", "sales": 10, "profit": 1},
            {"segment_name": "SegB", "sales": 20, "profit": 2},
            {"segment_name": "SegC", "sales": 30, "profit": 3},
            {"segment_name": "SegD", "sales": 40, "profit": 4},
        ]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_pl.assert_not_called()
        m_sync_seg.assert_called_once()
        check_pl_args = m_check_pl.call_args[1]
        assert check_pl_args.get("filing_id") == fid
        assert check_pl_args.get("filing_id") != disclosure_no

        sync_seg_args = m_sync_seg.call_args[1]
        assert sync_seg_args.get("canonical_filing_id") == fid
        assert sync_seg_args.get("common_disclosure_no") == disclosure_no

    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved")
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("os.path.exists", return_value=True)
    def test_helper_7601_model(
        self, m_exists, m_find, m_get_supabase, m_extract_and_filter, m_check_seg, m_check_pl, m_sync_seg, m_sync_pl, tmp_path
    ):
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        m_get_supabase.return_value = mock_client
        zip_path = _make_identity_zip(tmp_path, "20260709590505")
        m_find.return_value = str(zip_path)

        fid = "4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8"
        disclosure_no = "20260709590505"
        pl_vals = {"sales": 500, "op": 50}

        m_check_pl.return_value = True
        m_check_seg.side_effect = [False, True]
        target_segs = [
            {"segment_name": "Seg1", "sales": 100, "profit": 10},
            {"segment_name": "Seg2", "sales": 200, "profit": 20},
            {"segment_name": "Seg3", "sales": 300, "profit": 30},
        ]
        m_extract_and_filter.return_value = target_segs

        _retry_incomplete_canonical_for_duplicate(
            ticker="7601",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=disclosure_no,
            xbrl_path=str(zip_path),
            pl_values=pl_vals,
            dry_run=False,
            target_segs=target_segs,
        )
        m_sync_pl.assert_not_called()
        m_sync_seg.assert_called_once()

        sync_seg_args = m_sync_seg.call_args[1]
        assert sync_seg_args.get("canonical_filing_id") == fid
        assert sync_seg_args.get("common_disclosure_no") == disclosure_no


class TestNoSegmentInfoStateManagement:
    """Phase 5: no_segment_info 状態管理の動作確認テストクラス (37件 of 独立したテストメソッド)"""

    def _setup_mock_supabase(self, mock_client, select_data):
        mock_query = MagicMock()
        mock_query.eq.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.execute.return_value.data = select_data

        mock_client.table.return_value.select.return_value = mock_query
        mock_client.table.return_value.update.return_value = mock_query
        return mock_query

    def _create_dummy_zip_with_internal_id(self, zip_path, disclosure_no):
        ticker = "9982"
        basename = zip_path.name
        if "7601" in basename or "7601" in disclosure_no:
            ticker = "7601"
        elif "9999" in basename or "9999" in disclosure_no:
            ticker = "9999"
            
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        import zipfile
        ticker_5 = f"{ticker}0" if len(ticker) == 4 else ticker
        xml_name = f"tse-aced-{ticker_5}-{disclosure_no}.xml"
        htm_name = f"Summary_{disclosure_no}.htm"
        q_val = "1"
        period = "2027-02-28"
        
        htm_content = f"""
        <html>
          <body>
            <xbrli:endDate>{period}</xbrli:endDate>
            <QuarterlyPeriod>{q_val}</QuarterlyPeriod>
            <ix:nonNumeric scheme="http://example.com/sicc">{ticker_5}</ix:nonNumeric>
          </body>
        </html>
        """
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(xml_name, b"<dummy/>")
            zf.writestr(htm_name, htm_content.encode("utf-8"))

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_1_valid_no_segment_info_prevents_retry(self, m_get_supabase, m_find, m_check_pl):
        """1. 有効なno_segment_info状態 -> ZIP検索0、抽出0、segment writer 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {
                "canonical_sync_state": {
                    "segments": {
                        "status": "no_segment_info",
                        "version": 1,
                        "filing_id": fid,
                        "disclosure_no": d_no,
                        "period": "2027-02-28",
                        "quarter": "1Q",
                        "source": "exact_xbrl_zero_rows",
                    }
                }
            }
        }])
        m_get_supabase.return_value = mock_client

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=None,
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        m_find.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_2_version_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """2. version不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 999}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_3_filing_id_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """3. filing_id不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "filing_id": "wrong_fid"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_4_disclosure_no_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """4. disclosure_no不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "disclosure_no": "wrong_d_no"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_5_period_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """5. period不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "period": "wrong_period"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_6_quarter_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """6. quarter不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "quarter": "wrong_q"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_7_source_mismatch(self, m_get_supabase, m_find, m_check_pl):
        """7. source不一致 -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "source": "wrong_source"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_8_raw_payload_non_dict(self, m_get_supabase, m_find, m_check_pl):
        """8. raw_payload が辞書型でない -> 通常抽出に進む"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"canonical_sync_state": {"segments": {"status": "no_segment_info", "version": 1, "source": "exact_xbrl_zero_rows"}}}
        }])
        m_get_supabase.return_value = mock_client
        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=None, pl_values={"sales": 100, "op": 10}, dry_run=False)
        m_find.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_9_success_empty_duplicate_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """9. 正しいZIP＋success_empty＋重複ルート -> raw_payload UPDATE 1回"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        mock_client.table.return_value.update.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved", return_value=False)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_10_success_with_rows_no_state_save(self, m_get_supabase, m_extract, m_check_seg, m_sync_seg, m_check_pl, tmp_path):
        """10. success_with_rows -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_with_rows"
            dummy_res.segments = [{"segment_name": "SegA", "sales": 100, "profit": 10}]
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return dummy_res.segments
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_11_parse_error_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """11. parse_error -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "parse_error"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_12_context_unresolved_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """12. context_unresolved -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "context_unresolved"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_13_date_guard_skip_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """13. date_guard_skip -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "date_guard_skip"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_14_zip_not_found_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """14. zip_not_found -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "zip_not_found"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_15_quarter_unresolved_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """15. quarter_unresolved -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "quarter_unresolved"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_16_segment_source_unavailable_no_state_save(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """16. segment_source_unavailable -> 状態保存なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "uuid_x", "source_doc_id": fid, "ticker": "9982", "disclosed_at": "2026-07-10T15:00:00+09:00", "raw_payload": {"title": "dummy title"}}])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "segment_source_unavailable"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_17_dry_run_no_write(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """17. dry-run＋success_empty -> UPDATE 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"title": "dummy title"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=True,
        )
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_18_disclosed_before_boundary(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """18. 2026-07-05以前 -> UPDATE 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-04T15:00:00+09:00",
            "raw_payload": {"title": "dummy title"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_19_invalid_disclosed_at(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """19. 日時解析失敗 -> UPDATE 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "invalid_date_format_string",
            "raw_payload": {"title": "dummy title"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_20_exact_event_zero_rows(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """20. 対象通知が0件 -> UPDATE 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [])
        m_get_supabase.return_value = mock_client

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_21_exact_event_multiple_rows(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """21. 対象通知が複数件 -> UPDATE 0"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        mock_client = MagicMock()
        self._setup_mock_supabase(mock_client, [{"id": "id1"}, {"id": "id2"}])
        m_get_supabase.return_value = mock_client

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_22_update_exception_isolated(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """22. update例外 -> 例外伝播なし"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "raw_payload": {"title": "dummy title"}
        }])
        mock_client.table.return_value.update.side_effect = RuntimeError("Supabase error simulation")
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        try:
            _retry_incomplete_canonical_for_duplicate(
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                filing_id=fid,
                disclosure_no=d_no,
                xbrl_path=str(zip_path),
                pl_values={"sales": 100, "op": 10},
                dry_run=False,
            )
        except Exception as e:
            pytest.fail(f"Exceptions should be caught internally: {e}")

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_23_identical_state_idempotent(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """23. 同一状態が既に存在 -> UPDATE 0 (冪等性)"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {
                "canonical_sync_state": {
                    "segments": {
                        "status": "no_segment_info",
                        "version": 1,
                        "filing_id": fid,
                        "disclosure_no": d_no,
                        "period": "2027-02-28",
                        "quarter": "1Q",
                        "source": "exact_xbrl_zero_rows",
                    }
                }
            }
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_24_raw_payload_other_keys_preserved(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """24. raw_payloadの他の既存キーが完全に保持されること"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "dummy title", "other_key_to_keep": "keep_me"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        update_args = mock_client.table.return_value.update.call_args[0][0]
        assert update_args["raw_payload"]["other_key_to_keep"] == "keep_me"

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_25_canonical_sync_state_other_keys_preserved(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """25. canonical_sync_state内の他キーが保持されること"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {
                "title": "dummy title",
                "canonical_sync_state": {"other_sync_state": "do_not_overwrite_me"}
            }
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        update_args = mock_client.table.return_value.update.call_args[0][0]
        assert update_args["raw_payload"]["canonical_sync_state"]["other_sync_state"] == "do_not_overwrite_me"

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_26_raw_payload_only_update(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """26. raw_payloadカラムのみがUPDATEの引数として渡されること"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "uuid_x",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "dummy title"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        update_args = mock_client.table.return_value.update.call_args[0][0]
        assert list(update_args.keys()) == ["raw_payload"]

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_27_subprocess_duplicate_real_entry(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """27. サブプロセス重複判定からの _retry_incomplete_canonical_for_duplicate 実行"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        mock_client.table.return_value.update.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_28_sequential_duplicate_real_entry(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """28. シーケンシャル重複判定からの _retry_incomplete_canonical_for_duplicate 実行"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        mock_client.table.return_value.update.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_segments_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_29_new_normal_route_inserts_merged_state(self, m_get_supabase, m_extract, m_check_pl, m_check_seg, tmp_path):
        """29. 新規通常経路の INSERT 前に no_segment_info がマージされること (64桁ID分離の検証)"""
        from src.events.earnings_production_pipeline import run_earnings_production
        import sqlite3
        import os
        mock_client = MagicMock()
        pdf_url = "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
        filing_id = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c" # 64桁ハッシュ値
        external_document_id = "140120260709590450" # 18桁外部文書ID
        disclosure_no = "20260709590450" # 14桁

        zip_dir = tmp_path / "data" / "xbrl_archive"
        zip_dir.mkdir(parents=True, exist_ok=True)
        zip_path = zip_dir / f"9982_{disclosure_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, disclosure_no)

        self._setup_mock_supabase(mock_client, [])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        doc = DummyDoc("9982", "2027年2月期 第1四半期決算短信", filing_id)
        doc.disclosure_id = filing_id
        doc.source_doc_id = external_document_id
        doc.doc_id = external_document_id
        doc.doc_url = pdf_url
        doc.pdf_url = pdf_url
        doc.disclosure_no = disclosure_no
        doc.disclosed_at = "2026-07-10T15:00:00+09:00"
        doc.company_name = "株式会社ダミー"
        doc.disclosure_datetime = "2026-07-10T15:00:00+09:00"
        doc.published_at = "2026-07-10T15:00:00+09:00"

        conn = sqlite3.connect(":memory:")

        env_patches = {
            "USE_SUBPROCESS_WORKER": "1",
            "EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE": "1",
            "EARNINGS_SUBPROCESS_ALLOWLIST": "9982"
        }

        with patch.dict(os.environ, env_patches):
            with patch("src.events.env_loader.get_project_root", return_value=tmp_path):
                with patch("src.events.earnings_subprocess_runner._PROJECT_ROOT", tmp_path):
                    with patch("src.events.tdnet_event_store.save_event_to_supabase") as m_save_sb:
                        m_save_sb.return_value = {"action": "inserted", "id": "new_uuid"}
                        run_earnings_production(
                            [doc],
                            conn,
                            webhook_url="",
                            dry_run=False,
                        )
                        m_save_sb.assert_called_once()
                        inserted_rec = m_save_sb.call_args[0][0]

                        # ID分離の厳格なアサーション
                        assert inserted_rec.source_doc_id == filing_id # Supabase EventRecord の Exact Gate 用 ID
                        import json
                        raw_payload = json.loads(inserted_rec.raw_payload_json)
                        assert raw_payload["canonical_sync_state"]["segments"]["status"] == "no_segment_info"
                        assert raw_payload["canonical_sync_state"]["segments"]["filing_id"] == filing_id # 64桁であることを検証
                        assert raw_payload["canonical_sync_state"]["segments"]["disclosure_no"] == disclosure_no # 14桁であることを検証

                        # 完了判定に 64桁の filing_id が正しく伝播しているかを call_args で検証
                        m_check_pl.assert_called()
                        m_check_seg.assert_called()
                        pl_call_kwargs = m_check_pl.call_args[1]
                        seg_call_kwargs = m_check_seg.call_args[1]
                        assert pl_call_kwargs.get("filing_id") == filing_id
                        assert seg_call_kwargs.get("filing_id") == filing_id
        conn.close()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved")
    @patch("src.events.earnings_production_pipeline._find_cached_xbrl")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_30_valid_state_plus_PL_incomplete(self, m_get_supabase, m_find, m_check_pl):
        """30. 有効状態＋PL不足 -> セグメント0回、PL writer継続"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"

        m_check_pl.return_value = False

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {
                "canonical_sync_state": {
                    "segments": {
                        "status": "no_segment_info",
                        "version": 1,
                        "filing_id": fid,
                        "disclosure_no": d_no,
                        "period": "2027-02-28",
                        "quarter": "1Q",
                        "source": "exact_xbrl_zero_rows",
                    }
                }
            }
        }])
        m_get_supabase.return_value = mock_client

        with patch("src.events.earnings_production_pipeline._sync_canonical_financials") as m_sync_pl:
            _retry_incomplete_canonical_for_duplicate(
                ticker="9982",
                period="2027-02-28",
                quarter="1Q",
                filing_id=fid,
                disclosure_no=d_no,
                xbrl_path=None,
                pl_values={"sales": 100, "op": 10},
                dry_run=False,
            )
            m_sync_pl.assert_called_once()
            m_find.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_31_target_segs_empty_without_detailed_result(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """31. target_segs は空だが、詳細結果オブジェクトがない -> 保存0回"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            src.events.earnings_production_pipeline._last_detailed_result = None
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_32_success_with_rows_filtered_to_zero(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """32. success_with_rows だがフィルタによって抽出数が0件になった -> 保存0回"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_with_rows"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_33_9982_model(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """33. 9982モデル相当の検証 (success_with_rows -> 状態保存0、writer実行)"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_with_rows"
            dummy_res.segments = [{"segment_name": "SegA", "sales": 100, "profit": 10}]
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return [{"segment_name": "SegA", "sales": 100, "profit": 10}]
        m_extract.side_effect = side_effect

        with patch("src.events.earnings_production_pipeline._sync_canonical_segments") as m_sync_seg:
            _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
            mock_client.table.return_value.update.assert_not_called()
            m_sync_seg.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_34_7601_model(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """34. 7601モデル相当の検証 (success_with_rows, ZIP内部書類ID一致 -> 状態保存0、writer実行)"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "4836e8c1953047daf09850a4b7f86ef0186f8ab85a348e41355323f2c3bf1da8" # ポプラ 64桁ハッシュ値
        d_no = "20260709590505" # ポプラ 14桁開示番号
        zip_path = tmp_path / f"7601_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "7601",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client
        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_with_rows"
            dummy_res.segments = [{"segment_name": "SegB", "sales": 200, "profit": 20}]
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return [{"segment_name": "SegB", "sales": 200, "profit": 20}]
        m_extract.side_effect = side_effect

        with patch("src.events.earnings_production_pipeline._sync_canonical_segments") as m_sync_seg:
            _retry_incomplete_canonical_for_duplicate(ticker="7601", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
            mock_client.table.return_value.update.assert_not_called()
            m_sync_seg.assert_called_once()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_35_filename_match_internal_mismatch(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """35. ファイル名は一致するが、ZIP内部書類IDが不一致 -> 状態保存0回"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"

        # 内部に無関係なファイル名のみを書き込む
        import zipfile
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("unrelated_file.txt", b"dummy")

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_36_internal_id_retrieval_failure_broken_zip(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """36. ZIPファイル破損のため内部ID取得失敗 -> 状態保存0回"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"broken zip binary")  # 破損ZIP

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client

        _retry_incomplete_canonical_for_duplicate(ticker="9982", period="2027-02-28", quarter="1Q", filing_id=fid, disclosure_no=d_no, xbrl_path=str(zip_path), pl_values={"sales": 100, "op": 10}, dry_run=False)
        mock_client.table.return_value.update.assert_not_called()

    @patch("src.events.earnings_production_pipeline._check_canonical_financials_saved", return_value=True)
    @patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
    @patch("src.events.tdnet_event_store._get_supabase")
    def test_37_internal_id_match_success_empty_saves(self, m_get_supabase, m_extract, m_check_pl, tmp_path):
        """37. 内部ID一致 + success_empty -> 状態保存成功"""
        from src.events.earnings_production_pipeline import _retry_incomplete_canonical_for_duplicate
        mock_client = MagicMock()
        fid = "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c"
        d_no = "20260709590450"
        zip_path = tmp_path / f"9982_{d_no}.zip"
        self._create_dummy_zip_with_internal_id(zip_path, d_no)

        self._setup_mock_supabase(mock_client, [{
            "id": "event_uuid_1",
            "source_doc_id": fid,
            "ticker": "9982",
            "disclosed_at": "2026-07-10T15:00:00+09:00",
            "dedupe_key": "dedupe_key_1",
            "pdf_url": "https://example.com/pdf",
            "raw_payload": {"title": "2027年2月期 第1四半期決算短信"}
        }])
        m_get_supabase.return_value = mock_client

        def side_effect(*args, **kwargs):
            import src.events.earnings_production_pipeline
            dummy_res = MagicMock()
            dummy_res.status = "success_empty"
            dummy_res.segments = []
            src.events.earnings_production_pipeline._last_detailed_result = dummy_res
            return []
        m_extract.side_effect = side_effect

        _retry_incomplete_canonical_for_duplicate(
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=fid,
            disclosure_no=d_no,
            xbrl_path=str(zip_path),
            pl_values={"sales": 100, "op": 10},
            dry_run=False,
        )
        mock_client.table.return_value.update.assert_called_once()

    # ───── 数値 filing_id 拒否テスト（Phase 5 復旧監査） ─────

    @pytest.mark.parametrize("invalid_id", [
        "20260709590450", # 14桁数値
        "140120260709590450", # 18桁数値
        "invalid_id_format_not_64_chars", # 64桁以外の文字列
    ])
    def test_canonical_check_rejects_invalid_filing_id(self, invalid_id):
        """数値IDおよび不正フォーマットIDが完了判定で即座に拒否され、SELECTを実行しないこと"""
        from src.events.earnings_production_pipeline import (
            _check_canonical_financials_saved,
            _check_canonical_segments_saved
        )
        mock_client = MagicMock()

        # Financials 判定
        res_pl = _check_canonical_financials_saved(
            client=mock_client,
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=invalid_id,
            expected_metrics=["sales"]
        )
        assert res_pl is False
        mock_client.table.assert_not_called() # SELECTを実行していないこと

        # Segments 判定
        res_seg = _check_canonical_segments_saved(
            client=mock_client,
            ticker="9982",
            period="2027-02-28",
            quarter="1Q",
            filing_id=invalid_id,
            expected_segment_metrics=[("Apparel", "sales")]
        )
        assert res_seg is False
        mock_client.table.assert_not_called() # SELECTを実行していないこと

    # ───── strict ZIP検証テスト（Phase 5 復旧監査） ─────

    def test_strict_zip_verify_internal_document_id_scenarios(self, tmp_path):
        """_verify_zip_internal_document_id の厳格な fail-closed 挙動をテスト"""
        from src.events.earnings_production_pipeline import _verify_zip_internal_document_id
        import zipfile
        import os

        # 1. 正常ZIP＋内部ID一致
        ok_zip_path = tmp_path / "ok.zip"
        with zipfile.ZipFile(ok_zip_path, "w") as zf:
            zf.writestr("tse-aced-99820-20260709590450.xml", b"dummy")
        assert _verify_zip_internal_document_id(str(ok_zip_path), "20260709590450") is True

        # 2. 正常ZIP＋内部ID不一致
        assert _verify_zip_internal_document_id(str(ok_zip_path), "20260709590505") is False

        # 3. 破損ZIP（basenameが一致していてもFalseを返すこと）
        broken_zip_path = tmp_path / "9982_20260709590450.zip"
        broken_zip_path.write_bytes(b"invalid zip content")
        assert _verify_zip_internal_document_id(str(broken_zip_path), "20260709590450") is False

        # 4. ZIP内部ID取得不能
        no_id_zip_path = tmp_path / "noid.zip"
        with zipfile.ZipFile(no_id_zip_path, "w") as zf:
            zf.writestr("unrelated_file.txt", b"dummy")
        assert _verify_zip_internal_document_id(str(no_id_zip_path), "20260709590450") is False

        # 5. 無効なdisclosure_no
        assert _verify_zip_internal_document_id(str(ok_zip_path), "invalid_no") is False
