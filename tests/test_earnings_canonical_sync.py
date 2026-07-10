import pytest
import sqlite3
import dataclasses
from unittest.mock import patch, MagicMock
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
    def test_segment_sync_sequential_called(self, m_load, m_save, m_cache, m_sync_fin, m_sync_seg, setup_db, monkeypatch):
        # 8. 逐次ルートで両識別子が正しく渡る
        # 25. PL保存成功を維持
        # 27. ポプラ相当1Q
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        m_cache.return_value = "C:/xbrl_archive/20260709590505.zip"
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
    def test_segment_sync_subprocess_called(self, m_save, m_sync_fin, m_sync_seg, setup_db, monkeypatch):
        # 9. サブプロセスルートで両識別子が正しく渡る
        # 10. サブプロセスでXBRLパスがNoneにならない正常例
        # 28. タキヒヨー相当1Q
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

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_poplar(self, m_write, m_extract):
        # 16. 前年同期を除外することの確認
        # 27. ポプラ相当1Q
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            # 前年同期 (7.当期のみ保存、16.前年同期は除外)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2026-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1299, profit=-63, raw_json={"_context_evidence": {"context_start": "2025-03-01", "context_end": "2025-05-31"}}),
            # 当期 (90日duration)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1242, profit=-90, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Lawson Poplar", sales=1529, profit=248, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=167, profit=-5, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_write.return_value = {"written": 3, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert args["ticker"] == "7601"
        assert args["period"] == "2027-02-28"
        assert args["quarter"] == "1Q"
        assert len(args["segments"]) == 3

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_takihyo(self, m_write, m_extract):
        # 28. タキヒヨー相当1Q
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Apparel And Textile", sales=15388, profit=447, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Rental Business", sales=249, profit=140, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Material", sales=1775, profit=211, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=263, profit=16, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_write.return_value = {"written": 4, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("9982", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590450", "C:/xbrl_archive/20260709590450.zip", dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert len(args["segments"]) == 4

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_no_data(self, m_write, m_extract):
        # 24. セグメントなし開示を毎回再解析し続けない (正常終了判定)
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        m_extract.return_value = []
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_dry_run(self, m_write, m_extract):
        # 11. dry-runでwriter未呼び出し
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", dry_run=True, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_extract_fail_handled(self, m_write, m_extract):
        # 26. セグメント失敗で通知・PLを巻き戻さない
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        m_extract.side_effect = Exception("Extract error")
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_writer_fail_handled(self, m_write, m_extract):
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow
        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-05-31"}}),
        ]
        m_write.side_effect = Exception("Write error")
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", dry_run=False, route="seq")

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

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_context_duration_selection(self, m_write, m_extract):
        # 12. 1Qの3か月累計を選択
        # 13. 2Qで6か月累計を選択し単独3か月を除外
        # 14. 3Qで9か月累計を選択し単独3か月を除外
        # 15. FYで通期を選択
        # 17. context順序を入れ替えても結果が変わらない
        # 18. 同一memberの複数contextで上書き混在しない
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        # 13. 2Q累計(180日)優先、単独(90日)除外
        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-06-01", "context_end": "2026-08-31"}}), # 単独3ヶ月 (90日)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=200, profit=20, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-08-31"}}), # 累計6ヶ月 (180日)
        ]
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "2Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", False, "seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert len(args["segments"]) == 1
        assert args["segments"][0]["sales"] == 200 # 累計が選択されていること

        # 17. 順序入れ替え
        m_write.reset_mock()
        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=200, profit=20, raw_json={"_context_evidence": {"context_start": "2026-03-01", "context_end": "2026-08-31"}}), # 累計6ヶ月 (180日)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="2Q", raw_segment_name="Smartstore", sales=100, profit=10, raw_json={"_context_evidence": {"context_start": "2026-06-01", "context_end": "2026-08-31"}}), # 単独3ヶ月 (90日)
        ]
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "2Q", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c", "20260709590505", "C:/xbrl_archive/20260709590505.zip", False, "seq")

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
             patch("src.events.earnings_production_pipeline.load_json") as m_load:

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
