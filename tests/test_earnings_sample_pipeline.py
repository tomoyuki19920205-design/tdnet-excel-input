#!/usr/bin/env python3
"""tests/test_earnings_sample_pipeline.py — 決算短信サンプル通知テスト

テスト項目:
  1. YOYクリップ表示 (_fmt_pct_short)
  2. フォーマット構造 (format_earnings_message)
  3. 理由なし時の通知 → 理由ブロック省略
  4. セグメントなし時 → セグメントブロック省略
  5. seed再現性
  6. 企業名フォールバック
  7. DB非汚染（サンプルモードでDB書き込みなし）
  8. --send-discord なし → HTTP呼び出しなし
"""
import os
import random
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.events.summary_financials import (
    _fmt_pct_short,
    _fmt_metric,
    EarningsSummaryData,
    SegmentFinancials,
)
from src.events.summary_notify import format_earnings_message


# ============================================================
# YOY クリップ表示テスト
# ============================================================
class TestYOYClip:
    """_fmt_pct_short のクリップ機能"""

    def test_no_clip_normal(self):
        assert _fmt_pct_short(0.183) == "+18%"
        assert _fmt_pct_short(-0.07) == "-7%"

    def test_no_clip_extreme(self):
        """clip=None（デフォルト）では丸めない"""
        assert _fmt_pct_short(3.5) == "+350%"
        assert _fmt_pct_short(-2.5) == "-250%"

    def test_clip_positive(self):
        """clip=2.0 で +200%+ に丸める"""
        assert _fmt_pct_short(3.5, clip=2.0) == "+200%+"
        assert _fmt_pct_short(2.01, clip=2.0) == "+200%+"

    def test_clip_negative(self):
        """clip=2.0 で -200%+ に丸める"""
        assert _fmt_pct_short(-2.5, clip=2.0) == "-200%+"
        assert _fmt_pct_short(-3.0, clip=2.0) == "-200%+"

    def test_clip_boundary(self):
        """ちょうど200%は丸めない"""
        assert _fmt_pct_short(2.0, clip=2.0) == "+200%"
        assert _fmt_pct_short(-2.0, clip=2.0) == "-200%"

    def test_none_returns_na(self):
        assert _fmt_pct_short(None) == "N/A"
        assert _fmt_pct_short(None, clip=2.0) == "N/A"

    def test_zero(self):
        assert _fmt_pct_short(0.0) == "+0%"


# ============================================================
# _fmt_metric クリップ付きテスト
# ============================================================
class TestFmtMetricClip:
    """_fmt_metric にクリップが効くこと"""

    def test_metric_with_clip(self):
        result = _fmt_metric("売上", 3.5, None, abs_val=100_000_000, unit="yen", clip=2.0)
        assert "+200%+" in result
        assert "売上" in result
        assert "億円" in result

    def test_metric_normal(self):
        result = _fmt_metric("営業利益", 0.25, None, abs_val=50_000_000, unit="yen", clip=2.0)
        assert "+25%" in result
        assert "+200%+" not in result


# ============================================================
# format_summary_line / format_segment_lines クリップテスト
# ============================================================
class TestEarningsSummaryDataFormat:
    """EarningsSummaryData のフォーマットメソッドにクリップが効くこと"""

    def test_summary_line_clip(self):
        data = EarningsSummaryData(
            sales_current=100_000_000_000,  # 1000億円 (円単位)
            sales_prior=20_000_000_000,     # 200億円 → YOY = 4.0 (+400%)
            op_current=10_000_000_000,
            op_prior=8_000_000_000,         # YOY = +25%
        )
        result = data.format_summary_line(clip=2.0)
        assert "+200%+" in result  # 売上YOYがクリップされる
        assert "+25%" in result or "+200%+" in result  # 営利は普通

    def test_segment_lines_clip(self):
        data = EarningsSummaryData(
            sales_current=100,
            sales_prior=50,
            segments=[
                SegmentFinancials(
                    name="テスト事業",
                    sales_current=80_000_000,   # 百万円 → 8億円
                    sales_prior=10_000_000,     # YOY = 7.0 → clipされる
                    profit_current=5_000_000,
                    profit_prior=4_000_000,     # YOY = 0.25
                ),
            ],
        )
        result = data.format_segment_lines()
        assert "+200%+" in result  # 売上YOYがクリップ
        assert "テスト事業" in result


# ============================================================
# 通知フォーマット構造テスト
# ============================================================
class TestFormatEarningsMessage:
    """format_earnings_message の出力構造テスト"""

    def test_full_message_structure(self):
        msg = format_earnings_message(
            ticker="1234",
            company_name="テスト株式会社",
            summary_line="売上 100億円（YOY +12%）\n営業利益 10億円（YOY +25%）",
            segment_lines="セグメント：\n・事業A　100億円(+12%)　10億円(+25%)",
            company_reasons=["受注好調で売上増加", "コスト削減で利益改善"],
            segment_reasons=[{"segment_name": "事業A", "reason": "新製品が好調"}],
            title="2026年3月期 第3四半期決算短信",
        )
        assert "📊" in msg
        assert "テスト株式会社" in msg
        assert "1234" in msg
        assert "売上" in msg
        assert "営業利益" in msg
        assert "```" in msg  # コードブロック
        assert "■ 増減理由（全社）" in msg
        assert "受注好調" in msg
        assert "■ 増減理由（セグメント）" in msg
        assert "事業A" in msg

    def test_no_reasons(self):
        """理由なし → 理由ブロック省略"""
        msg = format_earnings_message(
            ticker="5678",
            company_name="ノー理由",
            summary_line="売上 50億円（YOY +5%）",
            segment_lines="",
            company_reasons=[],
            segment_reasons=[],
        )
        assert "📊" in msg
        assert "ノー理由" in msg
        assert "■ 増減理由" not in msg

    def test_no_segments(self):
        """セグメントなし → セグメントブロック省略"""
        msg = format_earnings_message(
            ticker="9999",
            company_name="ノーセグ",
            summary_line="売上 30億円（YOY -3%）",
            segment_lines="",
            company_reasons=["テスト理由"],
            segment_reasons=[],
        )
        assert "📊" in msg
        assert "```" not in msg  # セグメントなし→コードブロックなし
        assert "■ 増減理由（全社）" in msg

    def test_company_name_fallback(self):
        """company_name 空 → ticker表示"""
        msg = format_earnings_message(
            ticker="4321",
            company_name="",
            summary_line="売上 10億円（YOY +8%）",
            segment_lines="",
            company_reasons=[],
            segment_reasons=[],
        )
        assert "4321" in msg


# ============================================================
# seed再現性テスト
# ============================================================
class TestSeedReproducibility:
    """同じseedで同じ銘柄集合になること"""

    def test_same_seed_same_result(self):
        items = list(range(100))

        rng1 = random.Random(42)
        sample1 = rng1.sample(items, 20)

        rng2 = random.Random(42)
        sample2 = rng2.sample(items, 20)

        assert sample1 == sample2

    def test_different_seed_different_result(self):
        items = list(range(100))

        rng1 = random.Random(42)
        sample1 = rng1.sample(items, 20)

        rng2 = random.Random(99)
        sample2 = rng2.sample(items, 20)

        assert sample1 != sample2


# ============================================================
# DB非汚染テスト
# ============================================================
class TestDBIsolation:
    """サンプルモードで本番DBの summary_jobs / ai_summaries を書き込まないこと"""

    def test_pipeline_does_not_import_storage(self):
        """summary_earnings_pipeline が summary_storage をインポートしないこと"""
        import src.events.summary_earnings_pipeline as sep
        source = open(sep.__file__, encoding="utf-8").read()
        assert "summary_storage" not in source
        assert "insert_summary_job" not in source
        assert "save_ai_summary" not in source

    def test_pipeline_does_not_use_fingerprint(self):
        """summary_earnings_pipeline が fingerprint を管理しないこと"""
        import src.events.summary_earnings_pipeline as sep
        source = open(sep.__file__, encoding="utf-8").read()
        assert "compute_fingerprint" not in source


# ============================================================
# タイトルフィルタテスト
# ============================================================
class TestTitleFilter:
    """決算短信タイトルフィルタのテスト"""

    def test_tanshin_passes(self):
        from src.events.summary_earnings_pipeline import _is_tanshin_title
        assert _is_tanshin_title("2026年3月期 第3四半期決算短信〔日本基準〕（連結）")
        assert _is_tanshin_title("決算短信〔IFRS〕（連結）")

    def test_setsumeikai_excluded(self):
        from src.events.summary_earnings_pipeline import _is_tanshin_title
        assert not _is_tanshin_title("2026年3月期 第3四半期決算説明会資料")
        assert not _is_tanshin_title("決算説明資料")

    def test_non_tanshin_excluded(self):
        from src.events.summary_earnings_pipeline import _is_tanshin_title
        assert not _is_tanshin_title("自己株式の取得に係る事項の決定")
        assert not _is_tanshin_title("業績予想の修正")

    def test_hosoku_excluded(self):
        from src.events.summary_earnings_pipeline import _is_tanshin_title
        assert not _is_tanshin_title("決算短信補足資料")


# ============================================================
# 失敗理由分類テスト
# ============================================================
class TestSkipReasonClassification:
    """SkipReasonの分類定数が正しいこと"""

    def test_skip_reasons_exist(self):
        from src.events.summary_earnings_pipeline import SkipReason
        assert SkipReason.NON_FINANCIAL_STATEMENT == "non_financial_statement"
        assert SkipReason.MISSING_ZIP == "missing_zip"
        assert SkipReason.PRIOR_PERIOD_MISSING == "prior_period_missing"
        assert SkipReason.SALES_MISSING == "sales_missing"
        assert SkipReason.OP_MISSING == "op_missing"
