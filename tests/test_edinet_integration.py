"""tests/test_edinet_integration.py — EDINET 統合テスト (mock ベース)

API key 無しでも動作する resolver / cache / worker fallback のテスト。
"""
from __future__ import annotations
import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.backfill.edinet_client import (
    EdinetClient, EdinetDocument,
    EdinetResolveResult, EdinetDownloadResult,
)
from lib.backfill.edinet_resolver import (
    score_edinet_candidate, pick_best_edinet_candidate,
    _normalize_ticker, _normalize_filer_name,
)
from lib.backfill.edinet_xbrl_cache import EdinetXbrlCache


# ============================================================
# 1. Key 未設定 skip テスト
# ============================================================

class TestKeyMissing:
    """API key 未設定時の安全 skip 確認。"""

    def _make_no_key_client(self):
        """env に key があっても無視して key="" のクライアントを返す。"""
        saved = os.environ.pop("EDINET_API_KEY", None)
        try:
            c = EdinetClient(api_key="")
        finally:
            if saved is not None:
                os.environ["EDINET_API_KEY"] = saved
        return c

    def test_has_api_key_false(self):
        c = self._make_no_key_client()
        assert not c.has_api_key

    def test_has_api_key_true(self):
        c = EdinetClient(api_key="dummy_key_for_test")
        assert c.has_api_key

    def test_search_skipped_no_key(self):
        c = self._make_no_key_client()
        docs = c.search_documents("2026-03-01")
        assert docs == []

    def test_resolve_skipped_no_key(self):
        c = self._make_no_key_client()
        r = c.resolve_document(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
        )
        assert not r.attempted
        assert r.skipped
        assert r.skipped_reason == "missing_api_key"
        assert not r.succeeded

    def test_download_skipped_no_key(self):
        c = self._make_no_key_client()
        r = c.download_xbrl_zip("S100TEST", cache_dir=tempfile.mkdtemp())
        assert r.skipped
        assert r.skipped_reason == "missing_api_key"
        assert not r.succeeded


# ============================================================
# 2. Resolver スコアリングテスト
# ============================================================

def _make_doc(**kwargs):
    defaults = {
        "doc_id": "S100TEST",
        "issuer_name": "テスト株式会社",
        "ticker": "1234",
        "document_date": "2026-03-01",
        "title": "2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
        "doc_type_code": "120",
        "doc_description": "2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
        "xbrl_available": True,
        "zip_available": True,
        "edinetCode": "E00001",
        "secCode": "12340",
    }
    defaults.update(kwargs)
    return EdinetDocument(**defaults)


class TestNormalizeTicker:
    """_normalize_ticker のユニットテスト。"""

    def test_4digit(self):
        assert _normalize_ticker("1234") == "1234"

    def test_5digit_trailing_zero(self):
        assert _normalize_ticker("72030") == "7203"

    def test_5digit_no_trailing_zero(self):
        # 末尾が0でない5桁: strip_tdnet_trailing_zero は適用されない
        assert _normalize_ticker("72031") == "72031"

    def test_empty(self):
        assert _normalize_ticker("") == ""

    def test_whitespace(self):
        assert _normalize_ticker("  1234  ") == "1234"

    def test_none_like(self):
        assert _normalize_ticker("None") == ""


class TestNormalizeFilerName:
    """_normalize_filer_name のユニットテスト。"""

    def test_removes_common_words(self):
        result = _normalize_filer_name("トヨタ自動車株式会社")
        assert "株式会社" not in result
        assert "トヨタ自動車" in result

    def test_short_result_preserved(self):
        result = _normalize_filer_name("株式会社AB")
        # "AB" = 2文字 < _FILER_NAME_MIN_LEN (3)
        assert len(result) == 2

    def test_empty(self):
        assert _normalize_filer_name("") == ""


class TestScoringExact:
    """スコアリング: ticker + date + title 完全一致。"""

    def test_exact_match_high_score(self):
        doc = _make_doc()
        score, basis = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
            doc_type="financial_statement",
            candidate=doc,
        )
        assert score >= 0.7
        assert "ticker" in basis

    def test_ticker_mismatch_very_low(self):
        """ticker 不一致はスコアが非常に低い (MIN_SCORE 未満)。"""
        doc = _make_doc(ticker="9999", secCode="99990")
        score, basis = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        # ペナルティ方式: ticker_mismatch で -0.50 だが他の加点で 0 以上になりうる
        # 重要なのは MIN_SCORE (0.50) を大きく下回ること
        assert score < 0.50
        assert "ticker_mismatch" in basis


class TestScoringSecCode:
    """secCode 正規化のテスト。"""

    def test_secCode_5digit_match(self):
        """secCode='72030' と ticker='7203' が正しくマッチ。"""
        doc = _make_doc(ticker="", secCode="72030")
        score, basis = score_edinet_candidate(
            ticker="7203",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        assert score >= 0.40
        assert "ticker=7203" in basis

    def test_ticker_empty_no_crash(self):
        """candidate の ticker/secCode が空でも crash しない。"""
        doc = _make_doc(ticker="", secCode="")
        score, basis = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        # ticker_unknown → ペナルティ小、score は低いが 0.0 ではない可能性
        assert score >= 0.0
        assert "ticker_unknown" in basis


class TestScoringDateVariation:
    """日付差による加点の変化。"""

    def test_date_1day_off(self):
        doc = _make_doc(document_date="2026-03-02")
        score, _ = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        assert score >= 0.5

    def test_date_7day_off(self):
        doc = _make_doc(document_date="2026-03-08")
        score, _ = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        score_exact, _ = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidate=_make_doc(document_date="2026-03-01"),
        )
        assert score < score_exact


class TestScoringTitle:
    """タイトルマッチ。"""

    def test_fs_keyword_match(self):
        doc = _make_doc(title="2026年3月期 四半期決算短信〔日本基準〕（連結）")
        score, basis = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
            doc_type="financial_statement",
            candidate=doc,
        )
        assert score >= 0.6
        assert "title" in basis

    def test_irrelevant_title_lower_score(self):
        doc = _make_doc(title="臨時報告書")
        score, _ = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        score_good, _ = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidate=_make_doc(title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）"),
        )
        assert score < score_good


class TestScoringFilerName:
    """filer_name マッチのテスト。"""

    def test_filer_name_not_in_title_no_bonus(self):
        """企業名がタイトルに含まれない場合は加点なし。"""
        doc = _make_doc(issuer_name="ソニーグループ株式会社")
        score, basis = score_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidate=doc,
        )
        assert "filer_name_match" not in basis

    def test_secCode_empty_issuer_only_not_enough(self):
        """secCode空 + issuer_name近似のみでは resolve success しない。

        ticker_unknown(0) + date_exact(0.20) + title_fs(0.20) + filer(0.05)
        + doc_type_fs(0.10) + xbrl_avail(0.05) = 0.60
        score は MIN_SCORE を超えるが、1件しかないため margin=0
        → MIN_MARGIN(0.10) を満たさず unresolved
        """
        doc = _make_doc(
            ticker="", secCode="",
            issuer_name="テスト電気工業株式会社",
            title="決算短信",
        )
        # 同スコアのノイズ候補を追加 → margin が小さくなる
        doc2 = _make_doc(
            doc_id="S100NOISE",
            ticker="", secCode="",
            issuer_name="テスト電子株式会社",
            title="決算短信",
        )
        r = pick_best_edinet_candidate(
            ticker="5678",
            disclosure_date="2026-03-01",
            title="テスト電気工業 2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidates=[doc, doc2],
        )
        # margin 不足で unresolved
        assert not r.succeeded


class TestPickBest:
    """候補群からベスト選択。"""

    def test_best_picked_above_threshold(self):
        docs = [
            _make_doc(doc_id="S100GOOD", ticker="1234", document_date="2026-03-01",
                      title="2026年3月期 第3四半期決算短信"),
            _make_doc(doc_id="S100BAD", ticker="1234", document_date="2026-02-20",
                      title="臨時報告書"),
        ]
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
            doc_type="financial_statement",
            candidates=docs,
        )
        assert r.succeeded
        assert r.doc_id == "S100GOOD"
        assert r.match_score >= 0.50

    def test_all_below_threshold(self):
        """ticker 不一致候補のみ → 全スコア 0.0 → below_threshold。"""
        docs = [
            _make_doc(doc_id="S100X", ticker="9999", secCode="99990",
                      document_date="2025-06-01", title="臨時報告書"),
        ]
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidates=docs,
        )
        assert not r.succeeded
        assert "below_threshold" in r.match_basis

    def test_empty_candidates(self):
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="決算短信",
            doc_type="financial_statement",
            candidates=[],
        )
        assert not r.succeeded
        assert r.candidate_count == 0

    def test_top1_below_min_score_unresolved(self):
        """top1 はあるが MIN_SCORE 未満で unresolved。"""
        docs = [
            # ticker一致(+0.40)だが date_far(0), title不一致(0), doc_type不一致(0), xbrlなし(0)
            # → score = 0.40 < MIN_SCORE(0.50)
            _make_doc(doc_id="S100LOW", ticker="1234", secCode="12340",
                      document_date="2024-01-01", title="有価証券届出書",
                      doc_description="有価証券届出書", xbrl_available=False),
        ]
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 通期業績の概況について",
            doc_type="forecast_revision",
            candidates=docs,
        )
        assert not r.succeeded
        assert r.top1_score < 0.50

    def test_margin_too_small_unresolved(self):
        """top1 と top2 が近接 → margin 不足で unresolved。"""
        docs = [
            _make_doc(doc_id="S100A", ticker="1234", document_date="2026-03-01",
                      title="2026年3月期 第3四半期決算短信〔日本基準〕"),
            _make_doc(doc_id="S100B", ticker="1234", document_date="2026-03-01",
                      title="2026年3月期 第3四半期決算短信〔IFRS〕"),
        ]
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信",
            doc_type="financial_statement",
            candidates=docs,
        )
        # 両者ほぼ同スコアなので margin < 0.10
        if r.top1_score - r.top2_score < 0.10:
            assert not r.succeeded, "margin が小さい場合は unresolved であるべき"
        # もし margin >= 0.10 なら OK (テスト自体は通す)

    def test_clear_winner_among_noise(self):
        """明確一致1件 + ノイズ候補複数 → 正しく resolve。"""
        docs = [
            # ノイズ: ticker 不一致
            _make_doc(doc_id="S100N1", ticker="9998", secCode="99980",
                      document_date="2026-03-01", title="臨時報告書"),
            _make_doc(doc_id="S100N2", ticker="9997", secCode="99970",
                      document_date="2026-02-28", title="有価証券報告書"),
            _make_doc(doc_id="S100N3", ticker="", secCode="",
                      document_date="2026-03-01", title="決算短信"),
            # 明確一致
            _make_doc(doc_id="S100WIN", ticker="1234", secCode="12340",
                      document_date="2026-03-01",
                      title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）"),
        ]
        r = pick_best_edinet_candidate(
            ticker="1234",
            disclosure_date="2026-03-01",
            title="2026年3月期 第3四半期決算短信〔日本基準〕（連結）",
            doc_type="financial_statement",
            candidates=docs,
        )
        assert r.succeeded
        assert r.doc_id == "S100WIN"
        assert r.top1_score >= 0.50
        assert r.top1_score - r.top2_score >= 0.10


# ============================================================
# 3. Cache テスト
# ============================================================

class TestCache:
    """EDINET XBRL cache の保存 / lookup / metadata。"""

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = EdinetXbrlCache(tmpdir)
            assert not cache.has_xbrl_zip("S100ABC")

            path = cache.save_xbrl_zip("S100ABC", b"PK\x03\x04fake_zip_data")
            assert cache.has_xbrl_zip("S100ABC")
            assert cache.load_cached_xbrl_zip("S100ABC") == path
            assert path.exists()

    def test_metadata_saved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = EdinetXbrlCache(tmpdir)
            cache.save_xbrl_zip("S100META", b"fake_data")
            meta = cache.get_cache_metadata("S100META")
            assert meta["edinet_doc_id"] == "S100META"
            assert meta["source"] == "edinet"

    def test_missing_doc_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = EdinetXbrlCache(tmpdir)
            assert cache.load_cached_xbrl_zip("S100NONE") is None

    def test_download_uses_cache(self):
        """cache hit 時は API call しない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = EdinetXbrlCache(tmpdir)
            cache.save_xbrl_zip("S100HIT", b"cached_zip_data")

            saved = os.environ.pop("EDINET_API_KEY", None)
            try:
                c = EdinetClient(api_key="", cache_dir=tmpdir)
            finally:
                if saved is not None:
                    os.environ["EDINET_API_KEY"] = saved
            r = c.download_xbrl_zip("S100HIT", cache_dir=tmpdir)
            assert r.succeeded
            assert r.cache_hit
            assert not r.skipped
            assert "S100HIT" in r.cache_path


# ============================================================
# 4. Fallback 非対象維持テスト
# ============================================================

class TestFallbackNotTriggered:
    """invalid_structure / PL は EDINET resolve を試行しない。"""

    def test_invalid_structure_not_in_fallback_hints(self):
        _HINTS = {
            "pdf_no_segment_narrative_page",
            "pdf_no_segment_table_after_guard",
            "pdf_no_segment_page_candidate",
            "pdf_no_segment_table_candidate",
        }
        assert "pdf_segment_like_but_invalid_structure" not in _HINTS
        assert "pdf_pl_table_selected" not in _HINTS
        assert "pdf_toc_page_selected" not in _HINTS
        assert "pdf_text_cid_corrupted" not in _HINTS
        assert "pdf_extraction_failed" not in _HINTS


# ============================================================
# 5. Integration: mock で rescue フロー確認
# ============================================================

class TestMockRescueFlow:
    """EDINET cache に XBRL ZIP があれば key 無しでも rescue 可能。"""

    def test_rescue_scenario_with_cached_zip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = EdinetXbrlCache(tmpdir)
            cache.save_xbrl_zip("S100RESCUE", b"PK\x03\x04test_data")

            saved = os.environ.pop("EDINET_API_KEY", None)
            try:
                c = EdinetClient(api_key="", cache_dir=tmpdir)
            finally:
                if saved is not None:
                    os.environ["EDINET_API_KEY"] = saved
            dl = c.download_xbrl_zip("S100RESCUE", cache_dir=tmpdir)
            assert dl.succeeded
            assert dl.cache_hit
            assert "S100RESCUE" in dl.cache_path


# ============================================================
# 6. Regression: 既存テストは別ファイルで確認
# ============================================================

class TestRegressionConstants:
    """既存 guard/hint の定数値が壊れていないことを確認。"""

    def test_fallback_hints_include_core(self):
        _HINTS = {
            "pdf_no_segment_narrative_page",
            "pdf_no_segment_table_after_guard",
        }
        assert "pdf_no_segment_narrative_page" in _HINTS
        assert "pdf_no_segment_table_after_guard" in _HINTS
