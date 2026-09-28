"""Disclosure identity regressions for direct and batched notification saves."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.events.common_models import EventRecord
from src.events import tdnet_event_store as store


class DisclosureIdentityTests(unittest.TestCase):
    def event(self, **changes):
        values = dict(ticker="7552", company_name="Happinet",
                      event_type="forecast_revision", subtype="upward",
                      title="業績予想の修正に関するお知らせ",
                      disclosure_datetime="2026-09-28 15:30",
                      doc_url="https://www.release.tdnet.info/inbs/140120260928541008.pdf",
                      source_doc_id="20260928541008",
                      extracted_payload_json='{"revised_net_income":9000}',
                      raw_payload_json='{}')
        values.update(changes)
        return EventRecord(**values)

    def row(self, event):
        return dict(store.build_supabase_row(event)[0], id="existing-id")

    def decide(self, event, existing, direct=False, **kwargs):
        with patch.object(store, "_get_supabase", return_value=MagicMock()), \
             patch.object(store, "_supabase_execute", return_value=SimpleNamespace(data=existing)), \
             patch("src.security_eligibility.classify_security_eligibility",
                   return_value=SimpleNamespace(is_etf_like=False, authoritative=True)):
            return store.save_event_to_supabase(event, _skip_db_write=True,
                    prefetched_existing_rows=None if direct else existing, **kwargs)

    def test_batch_never_matches_another_company_with_same_headline(self):
        old = self.row(self.event(ticker="1444", disclosure_datetime="2026-09-11 15:30",
                                 doc_url="https://www.release.tdnet.info/inbs/140120260910534609.pdf"))
        self.assertEqual(self.decide(self.event(), [old])["action"], "would_insert")

    def test_distinct_days_and_document_ids_keep_independent_notifications(self):
        for kind in ["forecast_revision", "dividend_revision", "buyback"]:
            for direct in [False, True]:
                with self.subTest(kind=kind, direct=direct):
                    new = self.event(event_type=kind)
                    old = self.row(self.event(event_type=kind, source_doc_id="old-doc",
                        disclosure_datetime="2026-09-11 15:30",
                        doc_url="https://www.release.tdnet.info/inbs/140120260910534609.pdf"))
                    self.assertEqual(self.decide(new, [old], direct)["action"], "would_insert")

    def test_distinct_dates_do_not_merge_legacy_rows_without_ids(self):
        old = self.row(self.event(source_doc_id="", doc_url="", disclosure_datetime="2026-09-11 15:30"))
        self.assertEqual(self.decide(self.event(source_doc_id="", doc_url=""), [old])["action"], "would_insert")

    def test_distinct_documents_on_same_day_do_not_match_by_title(self):
        old = self.row(self.event(source_doc_id="old-doc", doc_url="https://www.release.tdnet.info/inbs/old.pdf"))
        self.assertEqual(self.decide(self.event(), [old])["action"], "would_insert")

    def test_same_disclosure_retrieval_keeps_id(self):
        for direct in [False, True]:
            with self.subTest(direct=direct):
                event = self.event()
                result = self.decide(event, [self.row(event)], direct)
                self.assertEqual(result["action"], "dedup_skipped")
                self.assertEqual(result["id"], "existing-id")

    def test_refined_content_of_same_disclosure_updates_existing_id(self):
        old = self.row(self.event())
        new = self.event(extracted_payload_json='{"revised_net_income":9500}')
        for direct in [False, True]:
            with self.subTest(direct=direct):
                result = self.decide(new, [old], direct, discord_sent_at="2026-09-28T06:35:57Z")
                self.assertEqual(result["action"], "would_update")
                self.assertEqual(result["id"], "existing-id")
                self.assertEqual(result["_row"]["discord_sent_at"], "2026-09-28T06:35:57Z")

    def test_document_id_match_allows_same_disclosure_url_alias(self):
        old = self.row(self.event(raw_payload_json='{"source_doc_id":"20260928541008"}', doc_url="https://issuer.example/a.pdf"))
        self.assertEqual(self.decide(self.event(), [old])["action"], "would_update")

    def test_legacy_same_jst_day_is_independent_of_host_timezone(self):
        event = self.event(source_doc_id="", doc_url="", disclosure_datetime="2026-09-28 00:30")
        old = self.row(event);old["disclosed_at"]="2026-09-27T15:30:00Z"
        self.assertEqual(self.decide(event, [old])["action"], "dedup_skipped")

    def test_other_company_yoy_does_not_suppress_earnings(self):
        event = self.event(event_type="earnings", extracted_payload_json="{}")
        old = self.row(event);old.update(ticker="1444", primary_metric_yoy="10%")
        self.assertEqual(self.decide(event, [old])["action"], "would_insert")


if __name__ == "__main__":
    unittest.main()
