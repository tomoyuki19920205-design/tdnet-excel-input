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
