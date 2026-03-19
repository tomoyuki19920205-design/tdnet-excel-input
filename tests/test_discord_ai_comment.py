#!/usr/bin/env python3
"""tests/test_discord_ai_comment.py — Discord AI差分要約コメントのテスト"""
import json
import os
import sqlite3
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# tools/ は通常 sys.path に無いので追加
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from discord_alerts import (
    build_ai_comment_block,
    extract_positive_reason,
    extract_risk_reason,
    get_latest_diff_summary,
    _truncate,
    _parse_keywords_json,
    _TONE_JA,
)


# ============================================================
# テスト用 In-Memory DB ヘルパー
# ============================================================

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS filing_diff_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT,
    current_doc_id TEXT,
    previous_doc_id TEXT,
    current_title TEXT,
    previous_title TEXT,
    disclosed_at TEXT,
    period TEXT,
    quarter TEXT,
    comparison_rule TEXT,
    comparison_confidence TEXT,
    extraction_status TEXT,
    diff_status TEXT,
    ai_status TEXT,
    summary_overall TEXT,
    demand_change TEXT,
    profit_factor_change TEXT,
    guidance_change TEXT,
    risk_change TEXT,
    new_keywords_json TEXT,
    notable_added_phrases_json TEXT,
    notable_removed_phrases_json TEXT,
    tone_change TEXT,
    confidence TEXT,
    caution_note TEXT,
    raw_diff_payload_json TEXT,
    raw_ai_response_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


def _insert_summary(conn, ticker, period, quarter, ai_status,
                     summary_overall="", demand_change="",
                     profit_factor_change="", guidance_change="",
                     risk_change="", tone_change="", new_keywords_json="[]",
                     created_at=None):
    conn.execute(
        """INSERT INTO filing_diff_summaries (
            ticker, period, quarter, ai_status,
            summary_overall, demand_change, profit_factor_change,
            guidance_change, risk_change, tone_change,
            new_keywords_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
        (ticker, period, quarter, ai_status,
         summary_overall, demand_change, profit_factor_change,
         guidance_change, risk_change, tone_change,
         new_keywords_json, created_at),
    )
    conn.commit()


# ============================================================
# TestBuildAiCommentBlock
# ============================================================

class TestBuildAiCommentBlock:
    """AI コメントブロック構築テスト"""

    def test_ai_comment_with_full_summary(self):
        """completed レコード → AIコメント付き通知"""
        row = {
            "summary_overall": "需要が堅調に推移し、売上高が増加した",
            "profit_factor_change": "原材料費削減が利益に寄与",
            "demand_change": "堅調な受注が継続",
            "guidance_change": "通期見通しを上方修正",
            "risk_change": "為替リスクが増大",
            "tone_change": "slightly_positive",
            "new_keywords_json": json.dumps(["受注増加", "DX投資", "原材料"]),
        }
        result = build_ai_comment_block(row)
        assert "【AI差分要約】" in result
        assert "・総評:" in result
        assert "・好調理由:" in result
        assert "・見通し:" in result
        assert "・注意点:" in result
        assert "・トーン: やや強気" in result
        assert "・キーワード:" in result
        assert "受注増加" in result

    def test_ai_comment_without_summary(self):
        """レコード None → 空文字列"""
        result = build_ai_comment_block(None)
        assert result == ""

    def test_ai_comment_all_empty_fields(self):
        """全フィールド空 → 空文字列"""
        row = {
            "summary_overall": "",
            "profit_factor_change": "",
            "demand_change": "",
            "guidance_change": "",
            "risk_change": "",
            "tone_change": "",
            "new_keywords_json": "[]",
        }
        result = build_ai_comment_block(row)
        assert result == ""

    def test_long_summary_truncated(self):
        """長文 summary_overall が80文字で切られる"""
        long_text = "あ" * 200
        row = {"summary_overall": long_text}
        result = build_ai_comment_block(row)
        # "・総評: " (5文字) + 79文字 + "…" = 85文字以内
        lines = result.split("\n")
        summary_line = [l for l in lines if "総評" in l][0]
        # 80文字 + "・総評: " プレフィックス
        assert len(summary_line) <= 90
        assert "…" in summary_line


# ============================================================
# TestExtractReasons
# ============================================================

class TestExtractReasons:
    """好調理由 / 注意点の抽出テスト"""

    def test_positive_reason_from_fields(self):
        """文フィールド優先で好調理由が組み立てられる"""
        row = {
            "profit_factor_change": "価格改定効果",
            "demand_change": "受注好調",
            "new_keywords_json": json.dumps(["需要", "原材料"]),
        }
        result = extract_positive_reason(row)
        assert "価格改定効果" in result
        assert "受注好調" in result
        # キーワードは文フィールドがあるので補完されない
        assert "需要" not in result

    def test_positive_reason_from_keywords_only(self):
        """文フィールドが空の場合、キーワードから補完"""
        row = {
            "profit_factor_change": "",
            "demand_change": "",
            "new_keywords_json": json.dumps(["需要", "回復", "不透明感"]),
        }
        result = extract_positive_reason(row)
        assert "需要" in result
        assert "回復" in result
        # ネガティブKWは含まれない
        assert "不透明感" not in result

    def test_risk_reason_from_fields(self):
        """文フィールド優先で注意点が組み立てられる"""
        row = {
            "risk_change": "地政学リスクが高まる",
            "new_keywords_json": json.dumps(["原材料", "減損"]),
        }
        result = extract_risk_reason(row)
        assert "地政学リスクが高まる" in result
        # キーワードは文フィールドがあるので補完されない
        assert "原材料" not in result

    def test_risk_reason_from_keywords_only(self):
        """文フィールドが空の場合、ネガティブキーワードから補完"""
        row = {
            "risk_change": "",
            "new_keywords_json": json.dumps(["在庫調整", "高騰", "成長"]),
        }
        result = extract_risk_reason(row)
        assert "在庫調整" in result
        assert "高騰" in result
        assert "成長" not in result


# ============================================================
# TestGetLatestDiffSummary
# ============================================================

class TestGetLatestDiffSummary:
    """AI差分要約の取得テスト"""

    def test_exact_match_found(self):
        """同一 ticker/period/quarter で completed → 取得できる"""
        conn = _make_db()
        _insert_summary(conn, "4062", "2027-03-31", "3Q", "completed",
                        summary_overall="テスト完全一致")
        result = get_latest_diff_summary(conn, "4062", "2027-03-31", "3Q")
        assert result is not None
        assert result["summary_overall"] == "テスト完全一致"
        conn.close()

    def test_no_completed_returns_none(self):
        """completed がなければ None"""
        conn = _make_db()
        _insert_summary(conn, "4062", "2027-03-31", "3Q", "ai_failed")
        _insert_summary(conn, "4062", "2027-03-31", "3Q", "ai_rate_limited")
        result = get_latest_diff_summary(conn, "4062", "2027-03-31", "3Q")
        assert result is None
        conn.close()

    def test_fallback_to_nearest_period(self):
        """同一 ticker で別 period/quarter の completed → 近いレコードが選ばれる"""
        conn = _make_db()
        # 遠い period
        _insert_summary(conn, "4062", "2025-03-31", "3Q", "completed",
                        summary_overall="遠い期", created_at="2025-01-01")
        # 近い period
        _insert_summary(conn, "4062", "2027-03-31", "2Q", "completed",
                        summary_overall="近い期", created_at="2027-01-01")
        # 対象: 2027-03-31 3Q → 完全一致なし → 近い period/quarter が選ばれる
        result = get_latest_diff_summary(conn, "4062", "2027-03-31", "3Q")
        assert result is not None
        assert result["summary_overall"] == "近い期"
        conn.close()

    def test_ai_failed_excluded(self):
        """ai_failed / ai_rate_limited は採用されない"""
        conn = _make_db()
        _insert_summary(conn, "4062", "2027-03-31", "3Q", "ai_failed",
                        summary_overall="失敗した要約")
        _insert_summary(conn, "4062", "2027-03-31", "3Q", "ai_rate_limited",
                        summary_overall="制限された要約")
        result = get_latest_diff_summary(conn, "4062", "2027-03-31", "3Q")
        assert result is None
        conn.close()


# ============================================================
# TestToneConversion
# ============================================================

class TestToneConversion:
    """tone_change の日本語変換テスト"""

    def test_all_tones_converted(self):
        for eng, ja in _TONE_JA.items():
            row = {"tone_change": eng}
            result = build_ai_comment_block(row)
            assert ja in result, f"tone={eng} → {ja} が見つからない"

    def test_unknown_tone_shows_raw(self):
        row = {"tone_change": "unknown_value"}
        result = build_ai_comment_block(row)
        assert "unknown_value" in result


# ============================================================
# TestKeywordsJson
# ============================================================

class TestKeywordsJson:
    """new_keywords_json のパースと表示テスト"""

    def test_parse_valid_json(self):
        kws = _parse_keywords_json(json.dumps(["A", "B", "C"]))
        assert kws == ["A", "B", "C"]

    def test_parse_with_duplicates(self):
        kws = _parse_keywords_json(json.dumps(["A", "B", "A", "C"]))
        assert kws == ["A", "B", "C"]

    def test_parse_empty(self):
        assert _parse_keywords_json("") == []
        assert _parse_keywords_json(None) == []
        assert _parse_keywords_json("[]") == []

    def test_max_5_keywords_in_comment(self):
        """キーワードは最大5個まで表示"""
        kws = ["kw1", "kw2", "kw3", "kw4", "kw5", "kw6", "kw7"]
        row = {"new_keywords_json": json.dumps(kws)}
        result = build_ai_comment_block(row)
        assert "kw5" in result
        assert "kw6" not in result

    def test_invalid_json_returns_empty(self):
        assert _parse_keywords_json("not json") == []
