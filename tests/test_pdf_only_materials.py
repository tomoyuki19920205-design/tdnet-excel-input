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

    def test_earnings_briefing_transcript_summary_qa_and_faq_variants(self):
        cases = (
            ("2026年12月期 第２四半期 決算説明会 エグゼクティブサマリー", "2Q決算説明会 要約"),
            ("2026年12月期 第２四半期 決算説明会 書き起こし要約", "2Q決算説明会 書き起こし"),
            ("2026年６月期 決算説明会（書き起こし）", "決算説明会 書き起こし"),
            ("第１四半期 決算説明会　質疑応答・Ｑ＆Ａ", "1Q決算説明会 Q&A"),
            ("よくある質問と回答（2026年８月）", "IR FAQ"),
            ("よくあるご質問", "IR FAQ"),
            ("ＦＡＱ", "IR FAQ"),
        )
        for title, label in cases:
            self.assertMatch(title, "earnings_material", label)

    def test_earnings_near_negatives(self):
        titles = (
            "2027年3月期第1四半期決算短信〔日本基準〕(連結)",
            "決算説明会を開催いたしました",
            "第3四半期決算説明会 書き起こし公開のお知らせ",
            "（訂正）第1四半期決算説明資料の一部訂正について",
            "会社説明のお知らせ",
            "資料",
            "お知らせ",
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

    def test_management_strategy_must_match(self):
        titles = (
            "中期経営計画2029策定のお知らせ",
            "新中期経営計画について",
            "中期経営計画の更新に関するお知らせ",
            "中期経営計画説明会資料",
            "中期事業計画策定について",
            "事業計画及び成長可能性に関する事項",
            "事業計画および成長可能性に関する事項",
            "成長可能性に関する説明資料",
            "資本コストや株価を意識した経営の実現に向けた対応について",
            "資本コストと株価を意識した経営について",
            "ＰＢＲ 改善に向けた取組み",
            "PBRの向上に向けた対応",
        )
        for title in titles:
            self.assertMatch(title, "management_strategy", "中期経営・戦略")

    def test_management_strategy_safe_compound_rules(self):
        positives = (
            "今後の成長戦略についての説明資料",
            "事業成長戦略資料に関するお知らせ",
            "中期経営戦略 Vision2030の進捗状況について",
            "今後の企業価値向上に向けた取組みについて",
            "長期ビジョン En-Vision2035策定に関するお知らせ",
        )
        negatives = (
            "ニッポン国家成長戦略ファンド募集開始のお知らせ",
            "海外成長戦略の中核拠点となる製造所建設を推進",
            "大学経営戦略セミナーに当社社員が登壇",
            "企業価値向上委員会の設置に関するお知らせ",
            "企業価値向上に向けた役員報酬制度の改定",
        )
        for title in positives:
            self.assertMatch(title, "management_strategy", "中期経営・戦略")
        for title in negatives:
            self.assertIsNone(classify_pdf_only_material(title, PDF), title)

    def test_earnings_material_keeps_primary_type_for_combined_title(self):
        self.assertMatch(
            "2027年3月期決算説明資料・中期経営計画",
            "earnings_material",
            "決算説明資料",
        )

    def test_jquants_title_fallback_reaches_prefetch_filter(self):
        from src.fetcher import classify_disclosure
        from src.jquants.classifier import classify_disclosure_jquants

        title = "中期経営計画2029策定のお知らせ"
        self.assertEqual(classify_disclosure(title), "management_strategy")
        self.assertEqual(classify_disclosure_jquants([], title), "management_strategy")

    def test_jquants_broad_disc_item_does_not_override_material_semantics(self):
        from src.jquants.classifier import classify_disclosure_jquants

        self.assertEqual(
            classify_disclosure_jquants(
                ["11322"],
                "2026年12月期 第２四半期 決算説明会 エグゼクティブサマリー",
            ),
            "earnings_material",
        )

    def test_rollout_boundary_blocks_same_day_catchup(self):
        self.assertFalse(is_after_pdf_only_material_activation("2026-07-21 16:59:00"))
        self.assertTrue(is_after_pdf_only_material_activation("2026-07-21T16:59:01+09:00"))
        self.assertTrue(is_after_pdf_only_material_activation("2026-07-21T08:00:00Z"))
        self.assertFalse(is_after_pdf_only_material_activation(""))
        self.assertFalse(is_after_pdf_only_material_activation(
            "2026-08-21 19:53:12", "management_strategy"
        ))
        self.assertTrue(is_after_pdf_only_material_activation(
            "2026-08-21 19:53:13", "management_strategy"
        ))


class TestPdfOnlyMaterialPipeline(unittest.TestCase):
    def _doc(self):
        return DocumentMeta(
            doc_id="material-doc-1", ticker="7545", company_name="西松屋チェーン",
            title="2027年２月期売上高前年比速報（７月度）",
            disclosure_datetime="2026-07-21 17:00", doc_url=PDF,
            source_doc_id="jquants-native-id-1",
            link_validated=True,
        )

    def _strategy_doc(self, title="中期経営計画2029策定のお知らせ"):
        return DocumentMeta(
            doc_id="strategy-doc-1", ticker="1234", company_name="戦略株式会社",
            title=title, disclosure_datetime="2026-08-21 20:00", doc_url=PDF,
            source_doc_id="jquants-strategy-id-1",
            link_validated=True,
        )

    @patch("src.events.event_pipeline._get_text_and_pdf", side_effect=AssertionError("PDF must not be fetched"))
    def test_unvalidated_material_never_becomes_event(self, _fetch):
        doc = self._doc()
        doc.link_validated = False
        result = process_documents([doc], ":memory:", dry_run=True)
        self.assertEqual(result.detected, 0)
        self.assertEqual(result.skipped_all_doc_ids, [doc.doc_id])

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
        self.assertEqual(raw["text_extract_status"], "empty")

    def test_distinct_same_time_materials_keep_distinct_document_keys(self):
        first = self._doc()
        first.ticker = "3928"
        first.title = "2026年12月期 第２四半期 決算説明会 エグゼクティブサマリー"
        first.source_doc_id = "20260831528661"
        first.doc_url = "https://www.release.tdnet.info/inbs/140120260831528661.pdf"
        second = self._doc()
        second.ticker = "3928"
        second.title = "2026年12月期 第２四半期 決算説明会 書き起こし要約"
        second.source_doc_id = "20260831528656"
        second.doc_url = "https://www.release.tdnet.info/inbs/140120260831528656.pdf"
        first_record = process_documents([first], ":memory:", dry_run=True).details[0]["_event_record"]
        second_record = process_documents([second], ":memory:", dry_run=True).details[0]["_event_record"]
        self.assertNotEqual(build_dedupe_key(first_record), build_dedupe_key(second_record))

    def test_same_disclosure_retry_keeps_same_document_key(self):
        record = process_documents([self._doc()], ":memory:", dry_run=True).details[0]["_event_record"]
        first = build_dedupe_key(record)
        record.title = "表記だけが変わっても同じ資料"
        record.disclosure_datetime = "2026-07-21 17:01"
        self.assertEqual(first, build_dedupe_key(record))

    @patch("src.events.event_pipeline._get_text_and_pdf", side_effect=AssertionError("PDF must not be fetched"))
    def test_management_strategy_realtime_path_reaches_viewer_row(self, _fetch):
        result = process_documents([self._strategy_doc()], ":memory:", dry_run=True)
        self.assertEqual(result.detected, 1)
        self.assertEqual(len(result.details), 1)
        record = result.details[0]["_event_record"]
        self.assertEqual(record.event_type, EventType.MANAGEMENT_STRATEGY)
        row, payload, _, category, _ = build_supabase_row(record)
        self.assertEqual(category, "management_strategy")
        self.assertEqual(row["event_type"], "management_strategy")
        self.assertEqual(row["pdf_url"], PDF)
        self.assertEqual(row["display_summary"], "中期経営・戦略")
        self.assertFalse(row["notify_to_discord"])
        self.assertEqual(payload["extracted"]["notification_type"], "management_strategy")

    @patch("src.events.event_pipeline._get_text_and_pdf", return_value=("", ""))
    def test_mixed_forecast_and_strategy_does_not_create_strategy_duplicate(self, _fetch):
        doc = self._strategy_doc("業績予想及び中期経営計画の修正に関するお知らせ")
        result = process_documents([doc], ":memory:", dry_run=True)
        self.assertEqual(result.detected, 0)
        self.assertEqual(len(result.details), 1)
        self.assertEqual(result.details[0]["event_type"], EventType.FORECAST_REVISION)
        self.assertEqual(result.details[0]["action"], "no_change_detected")


if __name__ == "__main__":
    unittest.main()
