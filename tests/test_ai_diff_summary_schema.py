#!/usr/bin/env python3
"""tests/test_ai_diff_summary_schema.py — AI出力スキーマ + E2Eミニテスト"""
import pytest
import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.filing_diff.ai_summary import (
    validate_ai_summary_json,
    build_ai_diff_prompt,
    _DEFAULT_SUMMARY,
)


class TestValidateSchema:
    """AI出力スキーマテスト"""

    def test_valid_json(self):
        raw = json.dumps({
            "summary_overall": "需要が弱含み",
            "demand_change": "堅調→弱含み",
            "profit_factor_change": "原材料費増",
            "guidance_change": "据え置き",
            "risk_change": "在庫調整リスク追加",
            "new_keywords": ["在庫調整", "不透明感"],
            "notable_added_phrases": ["先行き不透明感が高まっている"],
            "notable_removed_phrases": ["堅調な需要"],
            "tone_change": "slightly_negative",
            "confidence": "high",
            "caution_note": "",
        })
        result = validate_ai_summary_json(raw)
        assert result["summary_overall"] == "需要が弱含み"
        assert result["tone_change"] == "slightly_negative"
        assert "在庫調整" in result["new_keywords"]

    def test_all_required_keys_present(self):
        result = validate_ai_summary_json("{}")
        for key in [
            "summary_overall", "demand_change", "profit_factor_change",
            "guidance_change", "risk_change", "new_keywords",
            "notable_added_phrases", "notable_removed_phrases",
            "tone_change", "confidence", "caution_note",
        ]:
            assert key in result

    def test_missing_keys_filled_with_defaults(self):
        result = validate_ai_summary_json('{"summary_overall": "test"}')
        assert result["summary_overall"] == "test"
        assert result["tone_change"] == "neutral"  # default
        assert result["confidence"] == "medium"  # default

    def test_json_in_markdown_block(self):
        raw = '```json\n{"summary_overall": "in markdown"}\n```'
        result = validate_ai_summary_json(raw)
        assert result["summary_overall"] == "in markdown"

    def test_json_with_surrounding_text(self):
        raw = 'Here is the analysis:\n{"summary_overall": "extracted"}\nEnd.'
        result = validate_ai_summary_json(raw)
        assert result["summary_overall"] == "extracted"

    def test_invalid_json_raises(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            validate_ai_summary_json("not json at all")

    def test_list_type_coercion(self):
        """list型でない値は自動補正"""
        raw = json.dumps({
            "new_keywords": "単一文字列",
            "notable_added_phrases": None,
            "notable_removed_phrases": 123,
        })
        result = validate_ai_summary_json(raw)
        assert isinstance(result["new_keywords"], list)
        assert isinstance(result["notable_added_phrases"], list)
        assert isinstance(result["notable_removed_phrases"], list)

    def test_dict_input(self):
        """dict入力も受け付ける"""
        result = validate_ai_summary_json({"summary_overall": "from dict"})
        assert result["summary_overall"] == "from dict"

    def test_empty_json_fallback(self):
        result = validate_ai_summary_json("{}")
        assert result["new_keywords"] == []
        assert result["tone_change"] == "neutral"


class TestBuildPrompt:
    """プロンプト構築テスト"""

    def test_prompt_contains_ticker(self):
        context = {
            "ticker": "6623",
            "company_name": "愛知電機",
            "current_title": "2026/3Q 決算短信",
            "previous_title": "2026/2Q 決算短信",
            "comparison_confidence": "high",
            "sections_diff": [],
        }
        prompt = build_ai_diff_prompt(context)
        assert "6623" in prompt
        assert "愛知電機" in prompt

    def test_prompt_contains_diff(self):
        context = {
            "ticker": "1832",
            "sections_diff": [{
                "section_name": "operating_results",
                "added": ["新しい文言が追加されました。"],
                "removed": ["前回の文言が削除されました。"],
                "changed": [],
                "keywords": ["需要"],
            }],
        }
        prompt = build_ai_diff_prompt(context)
        assert "新しい文言が追加されました" in prompt
        assert "前回の文言が削除されました" in prompt
        assert "需要" in prompt

    def test_prompt_json_instruction(self):
        prompt = build_ai_diff_prompt({"sections_diff": []})
        assert "json" in prompt.lower()
        assert "summary_overall" in prompt


class TestE2EMini:
    """E2Eミニテスト — サンプル current/previous テキストから要約レコードが組み立てられる"""

    def test_full_pipeline_mock(self):
        """AI抜きでパイプライン全体のデータフローを確認"""
        from src.filing_diff.text_extractor import split_into_sections, clean_text
        from src.filing_diff.section_diff import diff_sections

        prev_text = clean_text(
            "経営成績に関する説明\n"
            "当期は需要が堅調に推移し、売上高は増加しました。\n"
            "営業利益は価格改定の寄与により増加しました。\n"
            "\n"
            "業績予想に関する説明\n"
            "通期の業績予想は期初予想を据え置きます。\n"
        )
        curr_text = clean_text(
            "経営成績に関する説明\n"
            "当期は一部分野で需要が弱含みとなりましたが、全体としては堅調でした。\n"
            "原材料費の高騰が利益を圧迫しました。\n"
            "\n"
            "業績予想に関する説明\n"
            "通期の業績予想は期初予想を据え置きます。\n"
            "先行き不透明感が高まっています。\n"
        )

        prev_sections = split_into_sections(prev_text)
        curr_sections = split_into_sections(curr_text)

        assert len(prev_sections) >= 2
        assert len(curr_sections) >= 2

        # セクション名の一致を確認
        prev_map = {s.section_name_normalized: s for s in prev_sections}
        diffs = []
        for cs in curr_sections:
            ps = prev_map.get(cs.section_name_normalized)
            if ps:
                d = diff_sections(ps.section_text, cs.section_text, cs.section_name_normalized)
                diffs.append(d)

        # 差分が存在すること
        has_diff = any(
            d.added_sentences or d.removed_sentences or d.changed_pairs
            for d in diffs
        )
        assert has_diff

        # キーワードが検出されること
        all_kw = []
        for d in diffs:
            all_kw.extend(d.keywords)
        # 原材料高 or 不透明感 が検出されるはず
        assert len(all_kw) > 0

        # プロンプト生成
        payload = {
            "ticker": "6623",
            "company_name": "愛知電機",
            "current_title": "テスト3Q",
            "previous_title": "テスト2Q",
            "comparison_confidence": "high",
            "sections_diff": [
                {
                    "section_name": d.section_name,
                    "added": d.added_sentences,
                    "removed": d.removed_sentences,
                    "changed": [(p, c) for p, c in d.changed_pairs],
                    "keywords": d.keywords,
                }
                for d in diffs
            ],
        }
        prompt = build_ai_diff_prompt(payload)
        assert len(prompt) > 100
        assert "6623" in prompt


class TestRateLimitHandling:
    """429 Rate Limit ハンドリングテスト"""

    def _make_mock_response(self, status_code, body_text, headers=None):
        """requests.Response をモック"""
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = (200 <= status_code < 400)
        resp.text = body_text
        resp.content = body_text.encode("utf-8")
        resp.encoding = "utf-8"
        resp.headers = headers or {}
        resp.json.return_value = json.loads(body_text) if status_code == 200 else {}
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            from requests.exceptions import HTTPError
            resp.raise_for_status.side_effect = HTTPError(
                f"{status_code} Error", response=resp
            )
        return resp

    def _success_response(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "summary_overall": "テスト成功",
                        "tone_change": "neutral",
                        "confidence": "high",
                    })
                }
            }]
        })
        return self._make_mock_response(200, body)

    def _rate_limit_response(self, body_type="rate_limit", retry_after=None):
        bodies = {
            "rate_limit": '{"error":{"message":"Rate limit reached","type":"rate_limit_reached"}}',
            "quota": '{"error":{"message":"You exceeded your quota","type":"insufficient_quota"}}',
            "unknown": '{"error":{"message":"Too many requests"}}',
        }
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return self._make_mock_response(429, bodies[body_type], headers)

    def test_429_rate_limit_returns_ai_rate_limited(self):
        """429が最大リトライ後にai_rate_limitedで返る"""
        from unittest.mock import patch, MagicMock
        from src.filing_diff.ai_summary import generate_ai_diff_summary

        mock_post = MagicMock(side_effect=[
            self._rate_limit_response("rate_limit"),
            self._rate_limit_response("rate_limit"),
            self._rate_limit_response("rate_limit"),
            self._rate_limit_response("rate_limit"),  # 4回目で打ち止め
        ])

        with patch("requests.post", mock_post), \
             patch("time.sleep") as mock_sleep, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            result = generate_ai_diff_summary({"sections_diff": []})

        assert result["_ai_status"] == "ai_rate_limited"
        assert result["_rate_limit_category"] == "rate_limit_reached"
        assert mock_sleep.call_count == 3  # 3回sleep後、4回目で打ち止め

    def test_429_quota_returns_ai_rate_limited(self):
        """quota超過の429でもai_rate_limitedで返る"""
        from unittest.mock import patch, MagicMock
        from src.filing_diff.ai_summary import generate_ai_diff_summary

        mock_post = MagicMock(side_effect=[
            self._rate_limit_response("quota"),
            self._rate_limit_response("quota"),
            self._rate_limit_response("quota"),
            self._rate_limit_response("quota"),
        ])

        with patch("requests.post", mock_post), \
             patch("time.sleep"), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            result = generate_ai_diff_summary({"sections_diff": []})

        assert result["_ai_status"] == "ai_rate_limited"
        assert result["_rate_limit_category"] == "insufficient_quota"

    def test_429_twice_then_success_returns_completed(self):
        """429を2回返した後、3回目で成功してcompletedになる"""
        from unittest.mock import patch, MagicMock
        from src.filing_diff.ai_summary import generate_ai_diff_summary

        mock_post = MagicMock(side_effect=[
            self._rate_limit_response("rate_limit"),
            self._rate_limit_response("rate_limit"),
            self._success_response(),
        ])

        with patch("requests.post", mock_post), \
             patch("time.sleep") as mock_sleep, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            result = generate_ai_diff_summary({"sections_diff": []})

        assert result["_ai_status"] == "completed"
        assert result["summary_overall"] == "テスト成功"
        assert mock_sleep.call_count == 2  # 2回sleepして3回目で成功
        # backoff の値を確認: 2秒, 5秒
        assert mock_sleep.call_args_list[0][0][0] == 2
        assert mock_sleep.call_args_list[1][0][0] == 5

    def test_non_429_http_error_returns_ai_failed(self):
        """500等は即座にai_failedで返る（リトライしない）"""
        from unittest.mock import patch, MagicMock
        from src.filing_diff.ai_summary import generate_ai_diff_summary

        mock_post = MagicMock(return_value=self._make_mock_response(
            500, '{"error":"Internal Server Error"}'
        ))

        with patch("requests.post", mock_post), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            result = generate_ai_diff_summary({"sections_diff": []})

        assert result["_ai_status"] == "ai_failed"
        assert mock_post.call_count == 1  # リトライなし

    def test_retry_after_header_takes_priority(self):
        """Retry-Afterヘッダがある場合、固定backoffより優先する"""
        from unittest.mock import patch, MagicMock
        from src.filing_diff.ai_summary import generate_ai_diff_summary

        mock_post = MagicMock(side_effect=[
            self._rate_limit_response("rate_limit", retry_after=7),
            self._success_response(),
        ])

        with patch("requests.post", mock_post), \
             patch("time.sleep") as mock_sleep, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}):
            result = generate_ai_diff_summary({"sections_diff": []})

        assert result["_ai_status"] == "completed"
        # Retry-After=7 が優先される（固定backoff 2 ではなく）
        mock_sleep.assert_called_once_with(7.0)

    def test_sanitize_log_masks_api_key(self):
        """ログにAPIキーが含まれない"""
        from src.filing_diff.ai_summary import _sanitize_log
        text = 'Error with key sk-abcdefghij1234567890 in request'
        sanitized = _sanitize_log(text)
        assert "sk-abcdefghij" not in sanitized
        assert "sk-***MASKED***" in sanitized

    def test_classify_429_rate_limit(self):
        from src.filing_diff.ai_summary import _classify_429
        assert _classify_429('{"error":{"type":"rate_limit_reached"}}') == "rate_limit_reached"

    def test_classify_429_insufficient_quota(self):
        from src.filing_diff.ai_summary import _classify_429
        assert _classify_429('{"error":{"message":"insufficient_quota"}}') == "insufficient_quota"

    def test_classify_429_unknown(self):
        from src.filing_diff.ai_summary import _classify_429
        assert _classify_429('{"error":{"message":"something else"}}') == "http_429_unknown"

