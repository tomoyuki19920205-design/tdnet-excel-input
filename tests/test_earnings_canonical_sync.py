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

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch:
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

        with patch("src.events.earnings_production_pipeline.load_json") as m_fetch:
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
        # 1. 逐次ルートでセグメント同期が呼び出されること
        # 3. 1Q・日本基準
        monkeypatch.setenv("USE_SUBPROCESS_WORKER", "0")
        m_cache.return_value = "dummy.zip"
        m_save.return_value = {"action": "inserted"}

        e = EarningsSummaryData(sales_current=123, op_current=45)
        m_load.return_value = {
            "earnings": dataclasses.asdict(e),
            "company_name": "Test Poplar",
            "fiscal_year": "2027",
            "quarter": "1Q",
        }

        doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", "doc123")
        run_earnings_production([doc], setup_db, webhook_url="")

        # 12. PLのcanonical同期が維持される
        assert m_sync_fin.call_count == 1
        # 1. 逐次ルートでセグメントが呼ばれる
        assert m_sync_seg.call_count == 1
        call_args = m_sync_seg.call_args[1]
        assert call_args["ticker"] == "7601"
        assert call_args["period"] == "2027-02-28"
        assert call_args["quarter"] == "1Q"
        assert call_args["xbrl_path"] == "dummy.zip"
        assert call_args["filing_id"] == "doc123"
        assert call_args["route"] == "sequential"

    @patch("src.events.earnings_production_pipeline._sync_canonical_segments")
    @patch("src.events.earnings_production_pipeline._sync_canonical_financials")
    @patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
    def test_segment_sync_subprocess_called(self, m_save, m_sync_fin, m_sync_seg, setup_db, monkeypatch):
        # 2. サブプロセスルートでセグメント同期が呼び出されること
        # 4. 3QまたはFY
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
            m_cache.return_value = "dummy.zip"
            m_payload.return_value = {
                "extracted": {
                    "period": "2027-02-28",
                    "guidance": {}
                }
            }
            m_plan.return_value = {
                "earnings_summary_args": {
                    "ticker": "9982",
                    "title": "2027年2月期 通期決算短信（連結）",
                    "quarter": "FY",
                    "sales_value": 100,
                    "op_value": 20,
                    "fingerprint": "7890",
                    "company_name": "Test Takihyo",
                    "fiscal_year": "2027",
                    "disclosure_date": "2026-07-10"
                },
                "tdnet_event_payload": {"source_doc_id": "doc456"}
            }
            m_discord_plan.return_value = {"discord_message": "test"}
            m_supa.return_value = {"action": "inserted"}

            doc = DummyDoc("9982", "2027年2月期 通期決算短信（連結）", "doc456")
            run_earnings_production([doc], setup_db, webhook_url="")

        assert m_sync_fin.call_count == 1
        assert m_sync_seg.call_count == 1
        call_args = m_sync_seg.call_args[1]
        assert call_args["ticker"] == "9982"
        assert call_args["period"] == "2027-02-28"
        assert call_args["quarter"] == "FY"
        assert call_args["route"] == "subprocess"

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_poplar(self, m_write, m_extract):
        # 14. ポプラ相当の3セグメント
        # 7. 当期contextだけを保存する
        # 8. 前年同期contextを保存しない
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            # 前年同期 (7.当期のみ保存、8.前年同期は除外すること)
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2026-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1299, profit=-63),
            # 当期
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Smartstore", sales=1242, profit=-90),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Lawson Poplar", sales=1529, profit=248),
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=167, profit=-5),
        ]
        m_write.return_value = {"written": 3, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert args["ticker"] == "7601"
        assert args["period"] == "2027-02-28"
        assert args["quarter"] == "1Q"
        assert len(args["segments"]) == 3
        # 前年同期が除外され、当期のみになっていること
        for seg in args["segments"]:
            if seg["segment_name"] == "Smartstore":
                assert seg["sales"] == 1242
                assert seg["profit"] == -90
            elif seg["segment_name"] == "Lawson Poplar":
                assert seg["sales"] == 1529
                assert seg["profit"] == 248
            elif seg["segment_name"] == "Other":
                assert seg["sales"] == 167
                assert seg["profit"] == -5

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_takihyo(self, m_write, m_extract):
        # 15. タキヒヨー相当の4セグメント
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Apparel And Textile", sales=15388, profit=447),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Rental Business", sales=249, profit=140),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Material", sales=1775, profit=211),
            SegmentRawRow(source="xbrl", raw_ticker="9982", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=263, profit=16),
        ]
        m_write.return_value = {"written": 4, "skipped": 0, "errors": 0}

        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("9982", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=False, route="seq")

        assert m_write.call_count == 1
        args = m_write.call_args[1]
        assert len(args["segments"]) == 4

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_no_data(self, m_write, m_extract):
        # 5. セグメント情報なし (正常スキップ)
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        m_extract.return_value = []
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_dry_run(self, m_write, m_extract):
        # 10. dry-runでwriter未呼び出し
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow

        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10),
        ]
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=True, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_extract_fail_handled(self, m_write, m_extract):
        # 8. 抽出失敗時にエラーログ、13. セグメント失敗でもPL成功を維持
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        m_extract.side_effect = Exception("Extract error")
        with patch("os.path.exists", return_value=True):
            # 例外がハンドリングされて処理が継続すること
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=False, route="seq")
        assert m_write.call_count == 0

    @patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip")
    @patch("lib.pipeline.canonical_writer.write_segments_canonical")
    def test_sync_canonical_segments_writer_fail_handled(self, m_write, m_extract):
        # 9. writer失敗時にエラーログ
        from src.events.earnings_production_pipeline import _sync_canonical_segments
        from src.segment.models import SegmentRawRow
        m_extract.return_value = [
            SegmentRawRow(source="xbrl", raw_ticker="7601", period="2027-02-28", quarter="1Q", raw_segment_name="Other", sales=100, profit=10),
        ]
        m_write.side_effect = Exception("Write error")
        with patch("os.path.exists", return_value=True):
            _sync_canonical_segments("7601", "2027-02-28", "1Q", "dummy.zip", "doc123", dry_run=False, route="seq")


class TestHTMLDecoder:
    def test_decode_utf8_japanese_html(self):
        # 16. UTF-8日本語HTMLを文字化けさせない
        text = "<html><head><meta charset='utf-8'></head><body>アパレル・テキスタイル関連事業</body></html>"
        raw = text.encode("utf-8")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert "アパレル・テキスタイル関連事業" in decoded

    def test_decode_cp932_japanese_html(self):
        # 17. CP932日本語HTMLを正常にデコード
        text = "<html><head><meta charset='shift_jis'></head><body>アパレル・テキスタイル関連事業</body></html>"
        raw = text.encode("cp932")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert "アパレル・テキスタイル関連事業" in decoded

    def test_utf8_not_misidentified_as_cp932(self):
        # 18. UTF-8をCP932として誤採用しない (メタcharsetがない場合)
        text = "アパレル・テキスタイル関連事業"
        raw = text.encode("utf-8")
        from src.events.summary_financials import _decode_html_bytes
        decoded = _decode_html_bytes(raw)
        assert decoded == text
