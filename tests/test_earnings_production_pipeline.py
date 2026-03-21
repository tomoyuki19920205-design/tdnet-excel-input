#!/usr/bin/env python3
"""test_earnings_production_pipeline.py — 決算短信V2 本番パイプライン テスト"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest


# ============================================================
# earnings_summary_storage テスト
# ============================================================
class TestEarningsSummaryStorage:
    """DB操作のテスト"""

    def _get_conn(self):
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)
        return conn

    def test_ensure_table_idempotent(self):
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)
        ensure_earnings_summary_table(conn)  # 2回目もエラーなし
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "earnings_summaries" in tables

    def test_save_and_retrieve(self):
        from src.events.earnings_summary_storage import (
            save_earnings_summary, get_earnings_summaries_by_ticker,
        )
        conn = self._get_conn()
        data = {
            "ticker": "1928",
            "company_name": "積水ハウス",
            "fiscal_year": "2026-01-31",
            "quarter": "4Q",
            "title": "2026年1月期 決算短信",
            "disclosure_date": "2026-03-13",
            "sales_value": 100000000000,
            "sales_yoy": 0.31,
            "op_value": 10000000000,
            "op_yoy": 1.39,
            "segment_summary_json": "[]",
            "overall_reason_summary": "増収増益",
            "segment_reason_summary": "",
            "summary_short": "売上 +31%, 営利 +139%",
            "summary_full": "📊 積水ハウス（1928）\n売上 1,000億円 YOY +31%",
            "fingerprint": "test_fp_001",
            "source_url": "",
            "archive_path": "",
        }
        action = save_earnings_summary(conn, data)
        assert action == "inserted"

        results = get_earnings_summaries_by_ticker(conn, "1928")
        assert len(results) == 1
        assert results[0]["ticker"] == "1928"
        assert results[0]["sales_yoy"] == 0.31

    def test_fingerprint_dedup(self):
        from src.events.earnings_summary_storage import save_earnings_summary
        conn = self._get_conn()
        data = {
            "ticker": "1928",
            "company_name": "積水ハウス",
            "fingerprint": "dup_fp_001",
        }
        assert save_earnings_summary(conn, data) == "inserted"
        assert save_earnings_summary(conn, data) == "already_exists"

    def test_mark_notified(self):
        from src.events.earnings_summary_storage import (
            save_earnings_summary, mark_earnings_notified,
            get_unnotified_earnings_summaries,
        )
        conn = self._get_conn()
        save_earnings_summary(conn, {"ticker": "1928", "fingerprint": "notify_fp"})

        unnotified = get_unnotified_earnings_summaries(conn)
        assert len(unnotified) == 1

        mark_earnings_notified(conn, "notify_fp")
        unnotified = get_unnotified_earnings_summaries(conn)
        assert len(unnotified) == 0


# ============================================================
# 通知条件テスト
# ============================================================
class TestNotifyCondition:
    """通知条件: sales_yoy >= 25% or op_yoy >= 25% (内部実値で判定)"""

    def test_sales_yoy_high(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.30, 0.10) is True

    def test_op_yoy_high(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.05, 0.30) is True

    def test_both_high(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.50, 0.50) is True

    def test_both_low(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.10, 0.10) is False

    def test_boundary_exact(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.25, 0.0) is True
        assert should_notify_earnings(0.24, 0.0) is False

    def test_none_sales(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(None, 0.30) is True

    def test_none_op(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(0.30, None) is True

    def test_both_none(self):
        from src.events.earnings_summary_storage import should_notify_earnings
        assert should_notify_earnings(None, None) is False


# ============================================================
# タイトルフィルタテスト
# ============================================================
class TestProductionTitleFilter:
    def test_tanshin(self):
        from src.events.earnings_production_pipeline import _is_tanshin_title
        assert _is_tanshin_title("2026年3月期 第3四半期決算短信〔日本基準〕（連結）")

    def test_setsumeikai_excluded(self):
        from src.events.earnings_production_pipeline import _is_tanshin_title
        assert not _is_tanshin_title("決算説明会資料")

    def test_hosoku_excluded(self):
        from src.events.earnings_production_pipeline import _is_tanshin_title
        assert not _is_tanshin_title("決算短信補足資料")


# ============================================================
# fiscal_year / quarter 解析テスト
# ============================================================
class TestFiscalInfoParsing:
    def test_from_earnings_data(self):
        from src.events.earnings_production_pipeline import _parse_fiscal_info
        earnings = SimpleNamespace(period="2026-01-31", quarter="4Q")
        fy, q = _parse_fiscal_info("2026年1月期 決算短信", earnings)
        assert fy == "2026-01-31"
        assert q == "4Q"

    def test_from_title_quarter(self):
        from src.events.earnings_production_pipeline import _parse_fiscal_info
        earnings = SimpleNamespace(period="2026-03-31", quarter="")
        fy, q = _parse_fiscal_info("第3四半期決算短信", earnings)
        assert q == "3Q"


# ============================================================
# fingerprint テスト
# ============================================================
class TestFingerprint:
    def test_deterministic(self):
        from src.events.earnings_production_pipeline import _compute_earnings_fingerprint
        fp1 = _compute_earnings_fingerprint("1928", "決算短信", "doc1")
        fp2 = _compute_earnings_fingerprint("1928", "決算短信", "doc1")
        assert fp1 == fp2

    def test_different_tickers(self):
        from src.events.earnings_production_pipeline import _compute_earnings_fingerprint
        fp1 = _compute_earnings_fingerprint("1928", "決算短信", "doc1")
        fp2 = _compute_earnings_fingerprint("7203", "決算短信", "doc1")
        assert fp1 != fp2


# ============================================================
# 本番/サンプルの分離テスト
# ============================================================
class TestSeparation:
    """sample_test と本番ロジックが分離されていること"""

    def test_production_does_not_import_sample(self):
        import src.events.earnings_production_pipeline as epp
        source = open(epp.__file__, encoding="utf-8").read()
        assert "run_earnings_sample_test" not in source
        assert "summary_earnings_pipeline" not in source

    def test_sample_does_not_import_production(self):
        import src.events.summary_earnings_pipeline as sep
        source = open(sep.__file__, encoding="utf-8").read()
        assert "earnings_production_pipeline" not in source

    def test_production_does_not_import_sample_storage(self):
        """本番はearnings_summary_storageを使い、sample_testは使わない"""
        import src.events.earnings_production_pipeline as epp
        source = open(epp.__file__, encoding="utf-8").read()
        assert "earnings_summary_storage" in source

    def test_sample_does_not_use_earnings_storage(self):
        import src.events.summary_earnings_pipeline as sep
        source = open(sep.__file__, encoding="utf-8").read()
        assert "earnings_summary_storage" not in source


# ============================================================
# SummaryPipelineResult V2フィールドテスト
# ============================================================
class TestPipelineResultFields:
    def test_v2_fields_exist(self):
        from src.events.summary_pipeline import SummaryPipelineResult
        r = SummaryPipelineResult()
        assert r.earnings_generated == 0
        assert r.earnings_saved == 0
        assert r.earnings_notified == 0
        assert r.earnings_filtered == 0
        assert r.earnings_no_yoy == 0
        assert r.earnings_already_exists == 0


# ============================================================
# 4Q専用拡張テスト
# ============================================================
class TestFyOr4QJudgment:
    """4Q/FY 判定ロジック — 返り値は tuple[bool, str]"""

    def test_quarter_fy(self):
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="FY")
        is_fy, reason = _is_fy_or_4q(e, "2026年3月期 決算短信")
        assert is_fy is True
        assert "quarter=FY" in reason

    def test_quarter_4q(self):
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="4Q")
        is_fy, reason = _is_fy_or_4q(e, "2026年1月期 決算短信")
        assert is_fy is True
        assert "quarter=4Q" in reason

    def test_quarter_3q_not_fy(self):
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="3Q")
        is_fy, reason = _is_fy_or_4q(e, "第3四半期決算短信")
        assert is_fy is False

    def test_quarter_3q_with_tsuuki_title(self):
        """quarter=3Qが主判定 → title '通期' は無視"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="3Q")
        is_fy, reason = _is_fy_or_4q(e, "通期決算短信")
        assert is_fy is False
        assert "quarter=3Q" in reason

    def test_no_quarter_with_tsuuki_title(self):
        """quarter なし + title '通期' → 補助判定で True"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "通期決算短信")
        assert is_fy is True
        assert "tsuuki" in reason

    def test_no_quarter_no_title(self):
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "決算短信")
        assert is_fy is False
        assert "no_fy_indicator" in reason

    # ---- 新規テストケース: タイトルベースFY判定 ----
    def test_title_fy_tanshin_january(self):
        """1月期 決算短信 → True (dry-runで漏れていた主因パターン)"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年1月期 決算短信〔日本基準〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_fy_tanshin_fullwidth(self):
        """全角数字: ２０２６年１月期 → NFKC正規化でマッチ"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "２０２６年１月期　決算短信〔日本基準〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_fy_tanshin_no_space(self):
        """スペースなし: 2026年1月期決算短信"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年1月期決算短信〔日本基準〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_fy_tanshin_fullwidth_space(self):
        """全角スペース: 2026年１月期　決算短信"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年１月期\u3000決算短信〔日本基準〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_ifrs(self):
        """IFRS（全角ＩＦＲＳ）: 2026年１月期 決算短信〔ＩＦＲＳ〕"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年１月期 決算短信〔ＩＦＲＳ〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_q3_exclude(self):
        """第3四半期を含む → False"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年1月期 第3四半期決算短信〔日本基準〕（連結）")
        assert is_fy is False
        assert "quarter_keyword" in reason

    def test_title_chuukan_exclude(self):
        """中間を含む → False"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "2026年1月期 中間決算短信〔日本基準〕（連結）")
        assert is_fy is False
        assert "quarter_keyword" in reason

    def test_title_teisei_fy_tanshin(self):
        """（訂正）2026年1月期 決算短信 → True（訂正は除外パターンに含まれない）"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(e, "（訂正）2026年1月期 決算短信〔日本基準〕（連結）")
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason

    def test_title_teisei_suuchi_fy(self):
        """（訂正・数値データ訂正）「2025年12月期決算短信」→ True"""
        from src.events.earnings_production_pipeline import _is_fy_or_4q
        e = SimpleNamespace(quarter="")
        is_fy, reason = _is_fy_or_4q(
            e,
            "（訂正・数値データ訂正）「2025年12月期決算短信〔日本基準〕（連結）」の一部",
        )
        assert is_fy is True
        assert "title_fy_tanshin_pattern" in reason


class TestFiscalInfoFYFallback:
    """_parse_fiscal_info の FY fallback テスト"""

    def test_quarter_fallback_fy(self):
        """quarter='' + FYタイトル → quarter='FY' に補完"""
        from src.events.earnings_production_pipeline import _parse_fiscal_info
        earnings = SimpleNamespace(period="", quarter="")
        _, q = _parse_fiscal_info("2026年1月期 決算短信〔日本基準〕（連結）", earnings)
        assert q == "FY"

    def test_quarter_fallback_fy_fullwidth(self):
        """全角数字+全角スペースでもFY"""
        from src.events.earnings_production_pipeline import _parse_fiscal_info
        earnings = SimpleNamespace(period="", quarter="")
        _, q = _parse_fiscal_info("２０２６年１月期\u3000決算短信〔日本基準〕（連結）", earnings)
        assert q == "FY"

    def test_quarter_no_fallback_for_q3(self):
        """第3四半期を含むタイトルではFY fallbackしない"""
        from src.events.earnings_production_pipeline import _parse_fiscal_info
        earnings = SimpleNamespace(period="", quarter="")
        _, q = _parse_fiscal_info("2026年1月期 第3四半期決算短信", earnings)
        assert q == "3Q"


class TestGuidanceData:
    """GuidanceData プロパティとフォーマット"""

    def test_yoy_calculation(self):
        from src.events.earnings_guidance_extractor import GuidanceData
        g = GuidanceData(
            sales_forecast=110_000_000_000,
            sales_actual=100_000_000_000,
            op_forecast=15_000_000_000,
            op_actual=10_000_000_000,
            eps_forecast=120.0,
            eps_actual=100.0,
        )
        assert g.has_guidance is True
        assert abs(g.sales_yoy - 0.10) < 0.001
        assert abs(g.op_yoy - 0.50) < 0.001
        assert abs(g.eps_yoy - 0.20) < 0.001

    def test_no_guidance(self):
        from src.events.earnings_guidance_extractor import GuidanceData
        g = GuidanceData()
        assert g.has_guidance is False
        assert g.has_outlook is False

    def test_guidance_with_none_actual(self):
        from src.events.earnings_guidance_extractor import GuidanceData
        g = GuidanceData(sales_forecast=100_000_000_000)
        assert g.has_guidance is True
        assert g.sales_yoy is None  # actual がないので YOY算出不可

    def test_format_guidance_section(self):
        from src.events.earnings_guidance_extractor import GuidanceData, format_guidance_section
        g = GuidanceData(
            sales_forecast=123_400_000_000,
            sales_actual=100_000_000_000,
            op_forecast=12_300_000_000,
            op_actual=10_000_000_000,
            eps_forecast=234.5,
            eps_actual=200.0,
            outlook_summary="セグメントA拡大と為替影響で増収増益を見込む",
        )
        text = format_guidance_section(g)
        assert "■ 来期ガイダンス" in text
        assert "売上:" in text
        assert "OP:" in text
        assert "EPS:" in text
        assert "■ 見通し" in text
        assert "セグメントA拡大" in text

    def test_format_no_guidance_no_outlook(self):
        from src.events.earnings_guidance_extractor import GuidanceData, format_guidance_section
        g = GuidanceData()
        text = format_guidance_section(g)
        assert text == ""


class TestGuidanceDBColumns:
    """DBにガイダンスカラムが追加されていること"""

    def test_guidance_columns_exist(self):
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)

        cursor = conn.execute("PRAGMA table_info(earnings_summaries)")
        cols = {row[1] for row in cursor.fetchall()}
        for expected in [
            "guidance_sales", "guidance_op", "guidance_eps",
            "guidance_sales_yoy", "guidance_op_yoy", "guidance_eps_yoy",
            "outlook_summary",
        ]:
            assert expected in cols, f"カラム {expected} が見つかりません"

    def test_save_with_guidance(self):
        from src.events.earnings_summary_storage import (
            ensure_earnings_summary_table, save_earnings_summary,
            get_earnings_summaries_by_ticker,
        )
        conn = sqlite3.connect(":memory:")
        ensure_earnings_summary_table(conn)
        data = {
            "ticker": "7203",
            "company_name": "トヨタ",
            "fingerprint": "guid_fp_001",
            "guidance_sales": 40_000_000_000_000,
            "guidance_op": 5_000_000_000_000,
            "guidance_eps": 350.0,
            "guidance_sales_yoy": 0.05,
            "guidance_op_yoy": 0.12,
            "guidance_eps_yoy": 0.08,
            "outlook_summary": "増収増益見通し",
        }
        assert save_earnings_summary(conn, data) == "inserted"

        rows = get_earnings_summaries_by_ticker(conn, "7203")
        assert len(rows) == 1
        assert rows[0]["guidance_sales"] == 40_000_000_000_000
        assert rows[0]["guidance_eps"] == 350.0
        assert rows[0]["outlook_summary"] == "増収増益見通し"

    def test_alter_table_migration(self):
        """既存DBでもALTER TABLEでカラムが追加されること"""
        from src.events.earnings_summary_storage import ensure_earnings_summary_table
        conn = sqlite3.connect(":memory:")
        # 旧スキーマ（ガイダンスカラムなし）を手動作成
        conn.execute("""
            CREATE TABLE earnings_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                fiscal_year TEXT,
                quarter TEXT,
                disclosure_date TEXT,
                notified_at TEXT,
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()

        # マイグレーション実行
        ensure_earnings_summary_table(conn)

        cursor = conn.execute("PRAGMA table_info(earnings_summaries)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "guidance_sales" in cols
        assert "outlook_summary" in cols


# ============================================================
# ガイダンス抽出精度改善 テスト
# ============================================================
class TestGuidanceExtractionImprovement:
    """4Qガイダンス抽出精度改善のユニットテスト"""

    # --- A. 見通しテキスト切り出し ---

    def test_outlook_truncation_at_keizoku(self):
        """「今後の見通し」の後に「継続企業の前提」→ 打ち切り"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "決算概要 略\n"
            "３．今後の見通しについて\n"
            "当社グループの来期の連結業績予想については、"
            "売上高は前期比10%増の100億円を見込んでおります。"
            "営業利益は原材料高の影響を受けつつも増収効果により前期比5%増の10億円を計画しております。\n"
            "（5）継続企業の前提に関する重要事象等\n"
            "該当事項はありません。\n"
        )
        text = _extract_outlook_text(html)
        assert "継続企業" not in text
        assert "売上高" in text

    def test_outlook_toc_only_rejected(self):
        """目次断片のみ → outlook_text_found=False (品質ガード)"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "…… 5\n"
            "…… 7\n"
            "- 3 -\n"
            "以上\n"
        )
        text = _extract_outlook_text(html)
        # 品質ガードにより空文字
        assert text == ""

    def test_eps_none_preserves_others(self):
        """EPS なしでも sales/op は保持"""
        from src.events.earnings_guidance_extractor import GuidanceData
        gd = GuidanceData(
            sales_forecast=10000,
            op_forecast=500,
            eps_forecast=None,
            sales_actual=9000,
            op_actual=400,
        )
        assert gd.has_guidance is True
        assert gd.sales_forecast == 10000
        assert gd.eps_forecast is None

    def test_eps_diluted_fallback(self):
        """Diluted EPS のみ → 代替採用される"""
        from src.events.earnings_guidance_extractor import (
            _EPS_BASIC_TAGS, _EPS_DILUTED_TAGS, _EPS_ALL_TAGS,
        )
        # Diluted タグが ALL に含まれること
        assert "DilutedEarningsPerShare" in _EPS_ALL_TAGS
        assert "DilutedEarningsLossPerShare" in _EPS_ALL_TAGS
        # Basic タグと Diluted タグが分離されていること
        assert _EPS_BASIC_TAGS & _EPS_DILUTED_TAGS == set()

    def test_op_priority_over_ordinary(self):
        """OperatingIncome と OrdinaryIncome が両方 _FORECAST_TAG_MAP に含まれること"""
        from src.events.earnings_guidance_extractor import _FORECAST_TAG_MAP
        assert "OrdinaryIncome" in _FORECAST_TAG_MAP
        assert _FORECAST_TAG_MAP["OrdinaryIncome"] == "ordinary_profit"
        assert "OperatingIncome" in _FORECAST_TAG_MAP
        assert _FORECAST_TAG_MAP["OperatingIncome"] == "operating_profit"

    def test_absurd_eps_excluded(self):
        """異常EPS値 (>10,000) のフィルタ閾値が設定されていること"""
        from src.events.earnings_guidance_extractor import _EPS_ABSURD_THRESHOLD
        assert _EPS_ABSURD_THRESHOLD == 10_000

    def test_fallback_summary_no_noise(self):
        """fallback_summary にページ番号・罫線が含まれないこと"""
        from src.events.earnings_guidance_extractor import make_fallback_summary
        text = (
            "今後の見通し\n"
            "当社グループの来期業績については、売上高は拡大を見込んでおります。\n"
            "━━━━━━━━━\n"
            "- 5 -\n"
            "営業利益は原材料コスト上昇の影響を受けるものの増益を計画しております。\n"
        )
        summary = make_fallback_summary(text)
        assert "━━" not in summary
        assert "- 5 -" not in summary
        assert "見通し" not in summary  # 見出し行は除去
        assert "売上高" in summary

    def test_outlook_quality_guard(self):
        """短すぎる/句点なし → 品質ガード不合格"""
        from src.events.earnings_guidance_extractor import _is_outlook_quality_ok
        # 短すぎ
        assert _is_outlook_quality_ok("短い文") is False
        # 十分な長さだが句点なし・1行のみ
        assert _is_outlook_quality_ok("a" * 50) is False
        # 句点ありで十分な長さ → OK
        assert _is_outlook_quality_ok(
            "当社グループの来期の連結業績予想については、売上高は前期比10%増の100億円を見込んでおります。"
            "営業利益は原材料高の影響を受けつつも増収効果により前期比5%増の10億円を計画しております。"
        ) is True
        # 句点なしだが複数行 → OK
        long_text = (
            "当社グループの来期業績予想については以下のとおりです\n"
            "売上高は前期比10パーセント増の100億円を見込んでおります"
        )
        assert _is_outlook_quality_ok(long_text) is True


# ============================================================
# 通期優先ロジック + 見通し通知テスト
# ============================================================
class TestFullYearPriority:
    """4Qガイダンスの通期優先ロジックと見通し通知テスト"""

    def test_classify_period_type_full_year(self):
        """通期コンテキストを full_year 判定"""
        from src.events.earnings_guidance_extractor import _classify_period_type
        assert _classify_period_type(
            "CurrentYearDuration_ConsolidatedMember_ForecastMember"
        ) == "full_year"
        assert _classify_period_type(
            "NextYearDuration_ConsolidatedMember_ForecastMember"
        ) == "full_year"
        assert _classify_period_type(
            "NextYearDuration_NonConsolidatedMember_ForecastMember"
        ) == "full_year"

    def test_classify_period_type_q2_cumulative(self):
        """2Q累計コンテキストを q2_cumulative 判定"""
        from src.events.earnings_guidance_extractor import _classify_period_type
        assert _classify_period_type(
            "CurrentAccumulatedQ2Duration_ConsolidatedMember_ForecastMember"
        ) == "q2_cumulative"
        assert _classify_period_type(
            "NextAccumulatedQ2Duration_ConsolidatedMember_ForecastMember"
        ) == "q2_cumulative"
        assert _classify_period_type(
            "CurrentYearDuration_SecondQuarterMember_NonConsolidatedMember_ForecastMember"
        ) == "q2_cumulative"

    def test_classify_period_type_unknown(self):
        """未知コンテキストを unknown 判定"""
        from src.events.earnings_guidance_extractor import _classify_period_type
        assert _classify_period_type("SomeRandomContext_ForecastMember") == "unknown"

    def test_classify_horizon(self):
        """来期/当期判定"""
        from src.events.earnings_guidance_extractor import _classify_horizon
        assert _classify_horizon(
            "NextYearDuration_ConsolidatedMember_ForecastMember"
        ) == "next_year"
        assert _classify_horizon(
            "CurrentYearDuration_ConsolidatedMember_ForecastMember"
        ) == "current_year"
        assert _classify_horizon(
            "NextAccumulatedQ2Duration_ConsolidatedMember_ForecastMember"
        ) == "next_year"
        assert _classify_horizon("RandomContext") == "unknown"

    def test_select_full_year_over_q2(self):
        """full_year と q2_cumulative 両方 → full_year 採用"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "sales", "value": 19500000000,
             "period_type": "q2_cumulative", "horizon": "current_year",
             "is_consol": True, "is_basic": True,
             "ctx": "AccumQ2", "tag": "NetSales", "source": "ixbrl"},
            {"metric": "sales", "value": 42000000000,
             "period_type": "full_year", "horizon": "current_year",
             "is_consol": True, "is_basic": True,
             "ctx": "CYDuration", "tag": "NetSales", "source": "ixbrl"},
        ]
        result = _select_best_candidates(candidates)
        assert result["sales"] == 42000000000

    def test_q2_only_returns_none(self):
        """q2_cumulative のみ → None（不採用）"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "sales", "value": 19500000000,
             "period_type": "q2_cumulative", "horizon": "current_year",
             "is_consol": True, "is_basic": True,
             "ctx": "AccumQ2", "tag": "NetSales", "source": "ixbrl"},
        ]
        result = _select_best_candidates(candidates)
        assert result["sales"] is None

    def test_unknown_and_q2_picks_unknown(self):
        """unknown + q2_cumulative → unknown を採用"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "op", "value": 5000000000,
             "period_type": "q2_cumulative", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "AccumQ2", "tag": "OperatingIncome", "source": "ixbrl"},
            {"metric": "op", "value": 12000000000,
             "period_type": "unknown", "horizon": "unknown",
             "is_consol": False, "is_basic": True,
             "ctx": "SomeCtx", "tag": "OperatingIncome", "source": "ixbrl"},
        ]
        result = _select_best_candidates(candidates)
        assert result["op"] == 12000000000

    def test_next_year_preferred_over_current(self):
        """同じ full_year でも next_year を current_year より優先"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "sales", "value": 10000000000,
             "period_type": "full_year", "horizon": "current_year",
             "is_consol": True, "is_basic": True,
             "ctx": "CY", "tag": "NetSales", "source": "ixbrl"},
            {"metric": "sales", "value": 15000000000,
             "period_type": "full_year", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "NY", "tag": "NetSales", "source": "ixbrl"},
        ]
        result = _select_best_candidates(candidates)
        assert result["sales"] == 15000000000

    def test_guidance_section_includes_outlook(self):
        """outlook_summary ありのとき ■ 見通し が含まれる"""
        from src.events.earnings_guidance_extractor import (
            GuidanceData, format_guidance_section,
        )
        g = GuidanceData(
            sales_forecast=10000000000,
            outlook_summary="来期は増収増益を見込んでおります。",
        )
        section = format_guidance_section(g)
        assert "■ 来期ガイダンス" in section
        assert "■ 見通し" in section
        assert "増収増益" in section

    def test_guidance_section_no_outlook_when_empty(self):
        """outlook_summary 空のとき ■ 見通し が含まれない"""
        from src.events.earnings_guidance_extractor import (
            GuidanceData, format_guidance_section,
        )
        g = GuidanceData(
            sales_forecast=10000000000,
            outlook_summary="",
        )
        section = format_guidance_section(g)
        assert "■ 来期ガイダンス" in section
        assert "■ 見通し" not in section


# ============================================================
# EPS テキスト抽出テスト
# ============================================================

class TestEpsTextExtraction:
    """EPS テキスト補完抽出のテスト"""

    # --- A. _normalize_eps_text ---

    def test_normalize_plain_number(self):
        """237.98 → 237.98"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("237.98") == 237.98

    def test_normalize_yen_sen(self):
        """120円50銭 → 120.5"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("120円50銭") == 120.5

    def test_normalize_negative_triangle(self):
        """△10.5円 → -10.5"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("△10.5円") == -10.5

    def test_normalize_yen_only(self):
        """120円 → 120.0"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("120円") == 120.0

    def test_normalize_comma_number(self):
        """1,234.56 → 1234.56"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("1,234.56") == 1234.56

    def test_normalize_dash_negative(self):
        """-10.5 → -10.5"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("-10.5") == -10.5

    def test_normalize_empty(self):
        """空白 → None"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("") is None
        assert _normalize_eps_text("   ") is None

    # --- B. _classify_eps_text_period ---

    def test_classify_eps_full_year(self):
        """通期 → full_year"""
        from src.events.earnings_guidance_extractor import _classify_eps_text_period
        assert _classify_eps_text_period("通期 14,700 1.3 588") == "full_year"

    def test_classify_eps_q2_cumulative(self):
        """第2四半期累計 → q2_cumulative"""
        from src.events.earnings_guidance_extractor import _classify_eps_text_period
        assert _classify_eps_text_period("第2四半期累計 8,000") == "q2_cumulative"

    def test_classify_eps_chuukan(self):
        """中間 → q2_cumulative"""
        from src.events.earnings_guidance_extractor import _classify_eps_text_period
        assert _classify_eps_text_period("中間 5,000 10.5") == "q2_cumulative"

    def test_classify_eps_unknown(self):
        """キーワードなし → unknown"""
        from src.events.earnings_guidance_extractor import _classify_eps_text_period
        assert _classify_eps_text_period("14,700 1.3 588") == "unknown"

    # --- C. _extract_eps_from_forecast_table ---

    def test_forecast_table_full_year(self):
        """通期行からEPS値を見出し列ベースで抽出"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり当期純利益\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭\n"
            "通期 14,700 1.3 588 6.7 664 7.8 475 2.6 237.98\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        assert len(cands) >= 1
        fy_cands = [c for c in cands if c["period_type"] == "full_year"]
        assert len(fy_cands) >= 1
        assert fy_cands[0]["value"] == 237.98

    def test_forecast_table_q2_not_mixed_with_fy(self):
        """通期行と2Q行が両方あるテーブルで通期が取れる"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり当期純利益\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭\n"
            "第2四半期累計 8,000 5.0 400 10.0 380 8.0 250 3.0 55.00\n"
            "通期 16,000 6.0 800 12.0 760 9.0 500 4.0 110.00\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        q2 = [c for c in cands if c["period_type"] == "q2_cumulative"]
        assert len(fy) >= 1
        assert fy[0]["value"] == 110.0
        assert len(q2) >= 1
        assert q2[0]["value"] == 55.0

    def test_forecast_table_extra_column_right(self):
        """EPS列の右に注記列がある場合"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        # 「配当」列がEPSの右にある場合:
        # 見出し行で「配当」があればhas_more_columnsで-2番目の数値
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり当期純利益 配当\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭 円\n"
            "通期 14,700 1.3 588 6.7 664 7.8 475 2.6 237.98 50\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        # EPS=237.98 (最後から2番目)、配当=50 (最後)
        assert len(fy) >= 1
        # 237.98 should be extracted, not 50
        assert fy[0]["value"] == 237.98

    # --- D. select_best_candidates with EPS ---

    def test_eps_select_full_year_only(self):
        """EPS: full_year のみ採用（q2_cumulative 不採用）"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "eps", "value": 55.0,
             "period_type": "q2_cumulative", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "text", "tag": "text_eps", "source": "text"},
            {"metric": "eps", "value": 110.0,
             "period_type": "full_year", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "text", "tag": "text_eps", "source": "text"},
        ]
        result = _select_best_candidates(candidates)
        assert result["eps"] == 110.0

    def test_eps_q2_only_returns_none(self):
        """EPS: q2_cumulative のみ → None"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "eps", "value": 55.0,
             "period_type": "q2_cumulative", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "text", "tag": "text_eps", "source": "text"},
        ]
        result = _select_best_candidates(candidates)
        assert result["eps"] is None

    def test_eps_unknown_returns_none(self):
        """EPS: unknown のみ → None（EPSはfull_year必須）"""
        from src.events.earnings_guidance_extractor import _select_best_candidates
        candidates = [
            {"metric": "eps", "value": 100.0,
             "period_type": "unknown", "horizon": "next_year",
             "is_consol": True, "is_basic": True,
             "ctx": "text", "tag": "text_eps", "source": "text"},
        ]
        result = _select_best_candidates(candidates)
        assert result["eps"] is None

    def test_eps_absurd_value_excluded(self):
        """abs(EPS) > 100000 → 除外"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり当期純利益\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭\n"
            "通期 14,700 1.3 588 6.7 664 7.8 475 2.6 999999\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        assert len(fy) == 0  # 異常値で除外

    def test_eps_notification_with_text_source(self):
        """EPS が取れた場合に通知にEPS行が含まれる"""
        from src.events.earnings_guidance_extractor import (
            GuidanceData, format_guidance_section,
        )
        g = GuidanceData(
            sales_forecast=14700000000,
            op_forecast=588000000,
            eps_forecast=237.98,
        )
        section = format_guidance_section(g)
        assert "EPS:" in section
        assert "237.98" in section or "238.0" in section

    def test_eps_1kabu_junrieki_variant(self):
        """「１株当たり純利益」表記（当期 なし）でも抽出可能"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり純利益\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭\n"
            "通期 5,000 3.0 200 5.0 180 4.0 120 2.0 85.50\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        assert len(fy) >= 1
        assert fy[0]["value"] == 85.5

    def test_eps_ensen_separate_style(self):
        """「237円98銭」形式 → 237.98"""
        from src.events.earnings_guidance_extractor import _normalize_eps_text
        assert _normalize_eps_text("237円98銭") == 237.98

    def test_eps_not_dividend_column(self):
        """配当金列がEPSとして誤採用されないこと"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 経常利益 当期純利益 １株当たり当期純利益 １株当たり配当金\n"
            "百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ 円 銭 円\n"
            "通期 10,000 5.0 500 10.0 480 8.0 300 4.0 150.00 40\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        assert len(fy) >= 1
        # EPS=150.00 であるべき。配当金=40 を採用してはいけない
        assert fy[0]["value"] == 150.0

    def test_eps_dividend_paragraph_not_matched(self):
        """本文中の配当段落で「1株当たり」がヒットしないこと"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "これにより、当期の期末配当につきましては、１株当たり42円と"
            "させていただきたいと存じます。この結果、1株当たりの年間配当金は"
            "84円となる予定です。なお、次期の配当金につきましては、中間配当を"
            "１株当たり43円、期末配当を１株当たり43円とし\n"
            "\n"
            "２．会計基準の選択に関する基本的な考え方\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        assert len(cands) == 0, f"配当段落から候補が出てはいけない: {cands}"

    def test_eps_suspicious_value_flagged(self):
        """EPS > 1000 は suspicious フラグ付き"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 当期純利益 １株当たり当期純利益\n"
            "百万円 百万円 百万円 円\n"
            "通期 65,000 3,000 2,000 6500.00\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        fy = [c for c in cands if c["period_type"] == "full_year"]
        assert len(fy) >= 1
        assert fy[0]["suspicious"] is True

    def test_eps_table_boundary_stops_at_double_blank(self):
        """空行2連続でテーブル終了 → 後続の無関係数値を拾わない"""
        from src.events.earnings_guidance_extractor import _extract_eps_from_forecast_table
        text = (
            "売上高 営業利益 当期純利益 １株当たり当期純利益\n"
            "百万円 百万円 百万円 円\n"
            "通期 10,000 500 300 85.50\n"
            "\n"
            "\n"
            "（注）上記は参考値であり確定値ではありません 9999.99\n"
        )
        cands = _extract_eps_from_forecast_table(text)
        # 9999.99 を拾っていないこと
        assert all(c["value"] != 9999.99 for c in cands), "テーブル後の数値を拾ってはいけない"
        assert len(cands) >= 1
        assert cands[0]["value"] == 85.5


# ============================================================
# 見通し抽出強化テスト
# ============================================================
class TestOutlookEnhancement:
    """outlook 抽出強化のユニットテスト"""

    # --- A. 新見出しパターン ---

    def test_heading_gyouseki_yosou(self):
        """「業績予想に関する説明」見出しで outlook 抽出可"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "決算概要 略\n"
            "３．業績予想に関する説明\n"
            "来期の連結業績予想については、売上高は為替影響を見込み"
            "前期比5%増の500億円を想定しております。"
            "原材料価格の上昇が影響するものの、増収効果により営業利益は拡大を予想しております。\n"
            "４．配当予想に関する説明\n"
            "１株当たり配当金は50円を予定しています。\n"
        )
        text = _extract_outlook_text(html)
        assert "売上高" in text
        assert "配当" not in text

    def test_heading_keiei_seiseki_with_future(self):
        """「経営成績に関する分析」+未来表現あり → 採用"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "経営成績に関する分析・検討\n"
            "当期の売上高は100億円で前期比10%の増収となりました。\n\n"
            "今後の需要拡大を見込み、来期の売上高は120億円を想定しております。\n"
            "配当の状況\n"
        )
        text = _extract_outlook_text(html)
        assert "来期" in text or "見込" in text
        # 過去実績のみの行は除外される
        assert "増収となりました" not in text

    def test_heading_keiei_seiseki_past_only_rejected(self):
        """「経営成績に関する分析」だが過去実績説明のみなら outlook 不採用"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "経営成績に関する分析\n"
            "当期の売上高は100億円で前期比10%の増収となりました。\n"
            "営業利益は15億円で前期比5%の増益となりました。\n"
            "配当の状況\n"
        )
        text = _extract_outlook_text(html)
        assert text == ""

    # --- B. 段落 fallback ---

    def test_fallback_no_heading(self):
        """見出しなし本文段落から見通し語で outlook 抽出可"""
        from src.events.earnings_guidance_extractor import _extract_outlook_paragraphs_fallback
        text = (
            "第1四半期の業績は堅調に推移しました。\n\n"
            "当社グループは今後の需要拡大を見込み、"
            "原材料価格の改善と為替の影響を想定した上で、"
            "来期も増収増益の計画としております。\n\n"
            "以上\n"
        )
        result = _extract_outlook_paragraphs_fallback(text)
        assert "需要拡大" in result
        assert "見込" in result

    def test_fallback_strong_keyword_single(self):
        """見通し語1つでも強語を含む短文は採用"""
        from src.events.earnings_guidance_extractor import _extract_outlook_paragraphs_fallback
        text = (
            "第1四半期の業績は堅調に推移しました。\n\n"
            "来期の連結業績については為替の影響を考慮した業績計画としております。\n\n"
        )
        result = _extract_outlook_paragraphs_fallback(text)
        assert "為替" in result or "影響" in result

    def test_fallback_quality_guard(self):
        """fallback でもノイズ段落は品質ガードで除外"""
        from src.events.earnings_guidance_extractor import _extract_outlook_paragraphs_fallback
        text = "a\n\nb\n\nc\n\n"
        result = _extract_outlook_paragraphs_fallback(text)
        assert result == ""

    # --- C. 打ち切り語 ---

    def test_stop_at_chuuki_jikou(self):
        """「注記事項」で打ち切り"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "来期の売上高は拡大を見込んでおります。\n"
            "注記事項\n"
            "この文は含めてはいけません。\n"
        )
        text = _extract_outlook_text(html)
        assert "拡大" in text
        assert "含めて" not in text

    def test_stop_at_yakuin_idou(self):
        """「役員異動」で打ち切り"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "当社は来期も増収を計画しております。営業利益についても改善を見込んでおります。\n"
            "役員異動について\n"
            "代表取締役の異動\n"
        )
        text = _extract_outlook_text(html)
        assert "増収" in text
        assert "代表取締役" not in text

    def test_stop_at_corporate_governance(self):
        """「コーポレートガバナンス」で打ち切り"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "来期は需要回復を想定し改善を見込んでおります。\n"
            "コーポレートガバナンスに関する報告\n"
            "この文は含めてはいけません。\n"
        )
        text = _extract_outlook_text(html)
        assert "回復" in text
        assert "報告" not in text

    def test_stop_at_rieki_haibun(self):
        """「利益配分」で打ち切り"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "為替の影響を考慮し、来期の売上高は拡大を見込んでおります。\n"
            "利益配分に関する基本方針\n"
            "利益は株主に還元\n"
        )
        text = _extract_outlook_text(html)
        assert "拡大" in text
        assert "還元" not in text

    # --- D. 配当を拾わない ---

    def test_dividend_not_outlook(self):
        """配当段落を見通しとして拾わない"""
        from src.events.earnings_guidance_extractor import _extract_outlook_paragraphs_fallback
        text = (
            "配当予想に関する説明\n\n"
            "当社は株主還元を経営の重要課題と位置づけ、"
            "安定配当の継続を方針としております。\n\n"
        )
        result = _extract_outlook_paragraphs_fallback(text)
        assert result == ""

    # --- E. 品質ガード緩和 ---

    def test_quality_short_but_meaningful(self):
        """短いが見通し語2つ以上 → 品質OK"""
        from src.events.earnings_guidance_extractor import _is_outlook_quality_ok
        text = "来期は需要拡大と為替の影響を想定"
        assert _is_outlook_quality_ok(text) is True

    def test_quality_too_short(self):
        """あまりにも短い → 品質NG"""
        from src.events.earnings_guidance_extractor import _is_outlook_quality_ok
        assert _is_outlook_quality_ok("短い文") is False

    # --- F. XBRL text block 優先 ---

    def test_xbrl_text_block_priority(self):
        """XBRL text block と HTML段落の両方がある場合は XBRL 側が優先"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        xbrl_html = (
            "今後の見通し\n"
            "XBRL側: 来期は原材料価格の改善により増収を見込んでおります。\n"
        )
        plain_html = (
            "今後の見通し\n"
            "HTML側: 来期の業績は拡大を想定しております。\n"
        )
        # XBRL 側を先に処理
        xbrl_text = _extract_outlook_text(xbrl_html)
        assert "XBRL側" in xbrl_text
        # XBRL が取れていれば plain は使わない (extract_guidance_from_zip のロジック確認)
        # ここでは個別関数の結果を確認
        plain_text = _extract_outlook_text(plain_html)
        assert "HTML側" in plain_text

    # --- G. 「配当予想に関する説明」見出し打ち切り ---

    def test_haito_yosou_not_outlook(self):
        """「配当予想に関する説明」は outlook として拾わない（打ち切り語ヒット）"""
        from src.events.earnings_guidance_extractor import _extract_outlook_text
        html = (
            "今後の見通し\n"
            "来期は需要回復を見込み、売上高の拡大を想定しております。\n"
            "配当予想に関する説明\n"
            "当期末配当は１株当たり50円\n"
        )
        text = _extract_outlook_text(html)
        assert "回復" in text
        assert "当期末配当" not in text
