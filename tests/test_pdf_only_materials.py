import json
import sqlite3
import unittest
from unittest.mock import patch

from src.events.common_models import DocumentMeta, EventRecord, EventType
from src.events.event_pipeline import process_documents
from src.events.notify_rules import should_notify_event
from src.events.tdnet_event_store import build_dedupe_key, build_supabase_row
from src.pdf_only_materials import classify_pdf_only_material, is_after_pdf_only_material_activation


PDF = "https://www.release.tdnet.info/inbs/140120260721596728.pdf"


class TestPdfOnlyMaterialClassifier(unittest.TestCase):
    def assertMatch(self, title, event_type, label):
        match = classify_pdf_only_material(title, PDF)
        self.assertIsNotNone(match)
        self.assertEqual(match.event_type, event_type)
        self.assertEqual(match.short_label, label)

    def test_earnings_quarter_and_full_width(self):
        self.assertMatch("2027年３月期第１四半期決算補足説明資料", "earnings_material", "1Q決算説明資料")

    def test_earnings_full_year_english(self):
        self.assertMatch("FY2026 Financial Results Presentation", "earnings_material", "FY決算説明資料")

    def test_earnings_uncertain_period_is_omitted(self):
        self.assertMatch("決算説明会資料", "earnings_material", "決算説明資料")
        self.assertMatch("2026年4月期決算説明補足資料", "earnings_material", "決算説明資料")

    def test_full_year_material_is_not_financial_statement(self):
        from src.fetcher import classify_disclosure
        self.assertEqual(classify_disclosure("2026年5月期通期決算説明資料"), "earnings_material")

    def test_earnings_near_negatives(self):
        titles = (
            "2027年3月期第1四半期決算短信〔日本基準〕(連結)",
            "決算説明会を開催いたしました",
            "第1四半期決算説明会 質疑応答要旨",
            "第3四半期決算説明会 書き起こし公開のお知らせ",
            "（訂正）第1四半期決算説明資料の一部訂正について",
        )
        for title in titles:
            self.assertIsNone(classify_pdf_only_material(title, PDF), title)

    def test_month_priority_avoids_fiscal_year_month(self):
        self.assertMatch("2027年２月期売上高前年比速報（７月度）", "monthly_update", "7月月次")
        self.assertMatch("月次KPI（2027年2月期）", "monthly_update", "月次")

    def test_monthly_variants(self):
        cases = (
            ("2026年６月度 月次概況（速報）のお知らせ", "6月月次"),
            ("月次業績速報 2026年6月", "6月月次"),
            ("2026年６月月次に関するお知らせ", "6月月次"),
            ("2027年３月期 月次前年比速報に関するお知らせ", "月次"),
            ("Monthly KPI Business Update", "月次"),
        )
        for title, label in cases:
            self.assertMatch(title, "monthly_update", label)

    def test_monthly_near_negatives_and_non_pdf(self):
        for title in ("ETF 月次レポート", "J-REIT 月次報告", "IRカレンダー（月次）"):
            self.assertIsNone(classify_pdf_only_material(title, PDF), title)
        self.assertIsNone(classify_pdf_only_material("2026年6月度 月次売上高", "https://example.com/a.html"))

    def test_rollout_boundary_blocks_same_day_catchup(self):
        self.assertFalse(is_after_pdf_only_material_activation("2026-07-21 16:59:00"))
        self.assertTrue(is_after_pdf_only_material_activation("2026-07-21T16:59:01+09:00"))
        self.assertTrue(is_after_pdf_only_material_activation("2026-07-21T08:00:00Z"))
        self.assertFalse(is_after_pdf_only_material_activation(""))


class TestPdfOnlyMaterialPipeline(unittest.TestCase):
    def _doc(self):
        return DocumentMeta(
            doc_id="material-doc-1", ticker="7545", company_name="西松屋チェーン",
            title="2027年２月期売上高前年比速報（７月度）",
            disclosure_datetime="2026-07-21 17:00", doc_url=PDF,
            source_doc_id="jquants-native-id-1",
        )

    @patch("src.events.event_pipeline._get_text_and_pdf", side_effect=AssertionError("PDF must not be fetched"))
    def test_pipeline_creates_event_without_pdf_fetch(self, _fetch):
        result = process_documents([self._doc()], ":memory:", dry_run=True)
        self.assertEqual(result.detected, 1)
        record = result.details[0]["_event_record"]
        self.assertEqual(record.event_type, EventType.MONTHLY_UPDATE)
        self.assertEqual(record.summary_text, "7月月次")
        self.assertFalse(should_notify_event(record))

    @patch("src.events.event_pipeline._get_text_and_pdf", side_effect=AssertionError("PDF must not be fetched"))
    def test_pipeline_marks_pre_activation_document_non_target(self, _fetch):
        doc = self._doc()
        doc.disclosure_datetime = "2026-07-21 16:59:00"
        result = process_documents([doc], ":memory:", dry_run=True)
        self.assertEqual(result.detected, 0)
        self.assertEqual(result.skipped_all_doc_ids, [doc.doc_id])

    @patch("src.events.tdnet_event_store._get_supabase", return_value=None)
    def test_duplicate_realtime_retry_saves_once(self, _supabase):
        conn = sqlite3.connect(":memory:")
        conn.close()
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), "pdf_only_material_events_test.db")
        try:
            if os.path.exists(path):
                os.remove(path)
            first = process_documents([self._doc()], path, dry_run=False)
            second = process_documents([self._doc()], path, dry_run=False)
            self.assertEqual(first.saved, 1)
            self.assertEqual(second.saved, 0)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_store_row_preserves_metadata_and_blocks_discord(self):
        result = process_documents([self._doc()], ":memory:", dry_run=True)
        record = result.details[0]["_event_record"]
        row, payload, dedupe, category, _ = build_supabase_row(record)
        self.assertEqual(category, "monthly_update")
        self.assertEqual(row["pdf_url"], PDF)
        self.assertEqual(row["headline"], self._doc().title)
        self.assertEqual(row["summary"], "7月月次")
        self.assertFalse(row["notify_to_discord"])
        self.assertEqual(dedupe, build_dedupe_key(record))
        raw = json.loads(row["raw_payload"])
        self.assertEqual(raw["extracted"]["source_doc_id"], "jquants-native-id-1")


if __name__ == "__main__":
    unittest.main()
