"""tests/test_xbrl_url_inference.py — XBRL URL 推定ヘルパーのテスト"""
from unittest import TestCase

from lib.backfill.xbrl_url_inference import (
    infer_xbrl_url_from_pdf,
    has_inferred_xbrl_url,
)


class TestInferXbrlUrlFromPdf(TestCase):
    """infer_xbrl_url_from_pdf のユニットテスト。"""

    def test_standard_pdf_url(self):
        """標準的な TDnet PDF URL → XBRL ZIP URL に変換。"""
        url = "https://www.release.tdnet.info/inbs/140120260313581653.pdf"
        expected = "https://www.release.tdnet.info/inbs/081220260313581653.zip"
        self.assertEqual(infer_xbrl_url_from_pdf(url), expected)

    def test_another_pdf_url(self):
        """別パターンの PDF URL。"""
        url = "https://www.release.tdnet.info/inbs/140120260311576030.pdf"
        expected = "https://www.release.tdnet.info/inbs/081220260311576030.zip"
        self.assertEqual(infer_xbrl_url_from_pdf(url), expected)

    def test_non_1401_prefix(self):
        """1401 以外のプレフィックスは None。"""
        url = "https://www.release.tdnet.info/inbs/999920260313581653.pdf"
        self.assertIsNone(infer_xbrl_url_from_pdf(url))

    def test_empty_url(self):
        """空文字列は None。"""
        self.assertIsNone(infer_xbrl_url_from_pdf(""))

    def test_none_url(self):
        """None は None。"""
        self.assertIsNone(infer_xbrl_url_from_pdf(None))

    def test_non_pdf_extension(self):
        """PDF 以外の拡張子は None。"""
        url = "https://www.release.tdnet.info/inbs/140120260313581653.zip"
        self.assertIsNone(infer_xbrl_url_from_pdf(url))

    def test_relative_url(self):
        """相対パスでも 1401 プレフィックスが見つかれば変換可能。"""
        url = "140120260313581653.pdf"
        expected = "https://www.release.tdnet.info/inbs/081220260313581653.zip"
        self.assertEqual(infer_xbrl_url_from_pdf(url), expected)


class TestHasInferredXbrlUrl(TestCase):
    """has_inferred_xbrl_url のテスト。"""

    def test_valid_url(self):
        url = "https://www.release.tdnet.info/inbs/140120260313581653.pdf"
        self.assertTrue(has_inferred_xbrl_url(url))

    def test_invalid_url(self):
        self.assertFalse(has_inferred_xbrl_url("https://example.com/file.pdf"))

    def test_empty(self):
        self.assertFalse(has_inferred_xbrl_url(""))
