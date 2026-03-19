#!/usr/bin/env python3
# ============================================================
# ai_summary.py — OpenAI GPT-4o-mini 差分要約
# ============================================================
"""
差分候補を OpenAI API に渡し、JSON互換の要約を生成する。

設定:
  .env に以下を設定:
    AI_PROVIDER=openai
    OPENAI_API_KEY=sk-...
    OPENAI_MODEL=gpt-4o-mini
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# Windows環境でのASCIIエンコーディング問題を回避
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

logger = logging.getLogger("filing_diff")

# ================================================================
# .env 読み込み
# ================================================================
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def _load_dotenv():
    env_path = Path(_PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


# ================================================================
# カスタム例外
# ================================================================

class RateLimitError(Exception):
    """OpenAI 429 Rate Limit エラー"""

    def __init__(self, message: str, status_code: int,
                 response_body: str, retry_after: float | None,
                 category: str):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after = retry_after
        self.category = category  # rate_limit_reached / insufficient_quota / http_429_unknown


# ================================================================
# AI出力スキーマ
# ================================================================

_REQUIRED_KEYS = [
    "summary_overall",
    "demand_change",
    "profit_factor_change",
    "guidance_change",
    "risk_change",
    "new_keywords",
    "notable_added_phrases",
    "notable_removed_phrases",
    "tone_change",
    "confidence",
    "caution_note",
]

_DEFAULT_SUMMARY: dict = {
    "summary_overall": "",
    "demand_change": "",
    "profit_factor_change": "",
    "guidance_change": "",
    "risk_change": "",
    "new_keywords": [],
    "notable_added_phrases": [],
    "notable_removed_phrases": [],
    "tone_change": "neutral",
    "confidence": "medium",
    "caution_note": "",
}


def validate_ai_summary_json(raw: str | dict) -> dict:
    """
    AI出力をパースし、必須キーを検証する。
    足りないキーはデフォルト値で補完。
    """
    if isinstance(raw, str):
        # JSON部分を抽出（```json...```で囲まれている場合）
        cleaned = raw.strip()
        m = re.search(r"```json\s*(.*?)\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
        # 先頭/末尾の余分なテキストを除去
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            cleaned = cleaned[start:end]
        parsed = json.loads(cleaned)
    else:
        parsed = raw

    if not isinstance(parsed, dict):
        raise ValueError(f"AI output is not a dict: {type(parsed)}")

    # 必須キー補完
    result = dict(_DEFAULT_SUMMARY)
    result.update(parsed)

    # list型の検証
    for key in ["new_keywords", "notable_added_phrases", "notable_removed_phrases"]:
        if not isinstance(result[key], list):
            result[key] = [str(result[key])] if result[key] else []

    return result


# ================================================================
# プロンプト構築
# ================================================================

def build_ai_diff_prompt(context: dict) -> str:
    """AI差分要約プロンプトを構築する"""
    prompt = (
        "あなたは日本企業の決算短信分析の専門家です。\n"
        "以下は同一企業の2つの決算短信から機械的に抽出された「本文の差分」です。\n"
        "この差分だけを根拠に「前回から何が変わったか」を分析してください。\n"
        "\n"
        "## 基本情報\n"
        f"- 企業: {context.get('company_name', '')} "
        f"(ticker: {context.get('ticker', '')})\n"
        f"- 今回開示: {context.get('current_title', '')}\n"
        f"- 比較対象: {context.get('previous_title', '')}\n"
        f"- 比較信頼度: {context.get('comparison_confidence', 'medium')}\n"
        "\n"
        "## セクション別の差分\n\n"
    )

    sections = context.get("sections_diff", [])
    for sd in sections:
        prompt += f"### {sd.get('section_name', 'unknown')}\n\n"
        added = sd.get("added", [])
        removed = sd.get("removed", [])
        changed = sd.get("changed", [])
        keywords = sd.get("keywords", [])

        if added:
            prompt += "**今回追加された文言:**\n"
            for s in added[:10]:
                prompt += f"- {s[:200]}\n"
            prompt += "\n"
        if removed:
            prompt += "**前回にあって今回消えた文言:**\n"
            for s in removed[:10]:
                prompt += f"- {s[:200]}\n"
            prompt += "\n"
        if changed:
            prompt += "**文言が変化した箇所:**\n"
            for prev, curr in changed[:5]:
                prompt += f"- 前回: {prev[:150]}\n  今回: {curr[:150]}\n"
            prompt += "\n"
        if keywords:
            prompt += f"**検出キーワード:** {', '.join(keywords)}\n\n"

    prompt += (
        "## 出力ルール\n\n"
        "1. 以下のJSON形式で出力してください。JSON以外のテキストは一切不要です。\n"
        "2. 本文にない情報を推測で書かないでください。\n"
        "3. 数字の厳密比較はしないでください。\n"
        '4. 「〜と考えられる」は使ってよいですが、根拠のない断定は禁止です。\n'
        "\n"
        "```json\n"
        "{\n"
        '  "summary_overall": "全体の変化を1-2文で要約",\n'
        '  "demand_change": "需要認識の変化（なければ空文字）",\n'
        '  "profit_factor_change": "利益要因の変化（なければ空文字）",\n'
        '  "guidance_change": "見通しの変化（なければ空文字）",\n'
        '  "risk_change": "リスク認識の変化（なければ空文字）",\n'
        '  "new_keywords": ["今回新たに登場した重要キーワードのリスト"],\n'
        '  "notable_added_phrases": ["注目すべき追加表現のリスト"],\n'
        '  "notable_removed_phrases": ["注目すべき削除表現のリスト"],\n'
        '  "tone_change": "stronger_positive/slightly_positive/neutral/'
        'slightly_negative/stronger_negative/mixed のいずれか",\n'
        '  "confidence": "high/medium/low のいずれか",\n'
        '  "caution_note": "分析上の注意事項（なければ空文字）"\n'
        "}\n"
        "```"
    )
    return prompt


# ================================================================
# ヘルパー関数
# ================================================================

def _sanitize_log(text: str, max_len: int = 200) -> str:
    """ログ出力用にテキストを短縮し、APIキーをマスクする"""
    import re as _re
    truncated = text[:max_len]
    # sk-... パターンをマスク（万が一 response body に含まれた場合の安全策）
    truncated = _re.sub(r"sk-[A-Za-z0-9_-]{10,}", "sk-***MASKED***", truncated)
    return truncated


def _classify_429(response_body: str) -> str:
    """
    429 response body から原因を判別する。

    Returns:
        'rate_limit_reached' | 'insufficient_quota' | 'http_429_unknown'
    """
    body_lower = response_body.lower()
    if "rate_limit_reached" in body_lower or "rate limit" in body_lower:
        return "rate_limit_reached"
    if "insufficient_quota" in body_lower or "quota" in body_lower:
        return "insufficient_quota"
    return "http_429_unknown"


# ================================================================
# OpenAI API 呼び出し (requests ライブラリ)
# ================================================================

def _call_openai(prompt: str) -> str:
    """
    OpenAI Chat Completions API を requests で呼び出す（1回のみ）。

    - リトライ制御は generate_ai_diff_summary 側で行う
    - 429 は RateLimitError を raise（category 付き）
    - 429 以外の HTTP エラーは raise_for_status() で処理
    """
    import requests as req_lib

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set. Add it to .env")

    url = "https://api.openai.com/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "あなたは日本企業の決算短信分析の専門家です。"
                    "指示されたJSON形式でのみ回答してください。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }

    # headers は ASCII のみ（日本語を絶対に入れない）
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {api_key}",
    }

    # デバッグログ（APIキー・body内容は出さない）
    logger.debug(
        f"[AI] requests.post: url={url}, model={model}, "
        f"headers_keys={list(headers.keys())}, "
        f"payload_keys={list(payload.keys())}"
    )

    resp = req_lib.post(
        url,
        json=payload,
        headers=headers,
        timeout=120,
    )

    logger.debug(
        f"[AI] response: status={resp.status_code}, "
        f"encoding={resp.encoding}, "
        f"len={len(resp.content)}"
    )

    # --- 429 Rate Limit ---
    if resp.status_code == 429:
        body_text = resp.text
        category = _classify_429(body_text)
        retry_after_raw = resp.headers.get("Retry-After")
        retry_after = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except (ValueError, TypeError):
                pass

        logger.warning(
            f"[AI] 429 Rate Limit: category={category}, "
            f"retry_after={retry_after}, "
            f"body={_sanitize_log(body_text)}"
        )
        raise RateLimitError(
            message=f"OpenAI 429: {category}",
            status_code=429,
            response_body=body_text,
            retry_after=retry_after,
            category=category,
        )

    # --- 429 以外の HTTP エラー ---
    if not resp.ok:
        logger.error(
            f"[AI] HTTP {resp.status_code} error: "
            f"{_sanitize_log(resp.text)}"
        )
        resp.raise_for_status()

    resp_json = resp.json()
    choices = resp_json.get("choices", [])
    if not choices:
        raise RuntimeError(
            f"OpenAI returned no choices: {_sanitize_log(resp.text)}"
        )

    content = choices[0].get("message", {}).get("content", "")
    logger.debug(f"[AI] content len={len(content)}")
    return content


# ================================================================
# メインエントリポイント
# ================================================================

# 429 リトライ用 backoff（秒）
_RATE_LIMIT_BACKOFF = [2, 5, 10]


def generate_ai_diff_summary(
    diff_payload: dict,
    max_retries: int = 1,
) -> dict:
    """
    差分ペイロードからAI要約を生成する。

    Args:
        diff_payload: build_ai_diff_prompt に渡す context dict
        max_retries: JSONパース失敗時のリトライ回数

    Returns:
        validate_ai_summary_json で検証済みのdict
        失敗時は:
          - 429 → ai_status='ai_rate_limited' + _rate_limit_category
          - その他 → ai_status='ai_failed'
    """
    prompt = build_ai_diff_prompt(diff_payload)
    rate_limit_max = len(_RATE_LIMIT_BACKOFF)  # 最大3回リトライ
    rate_limit_count = 0
    attempt = 0  # JSONパース用カウンタ（429リトライとは独立）

    while True:
        try:
            raw_response = _call_openai(prompt)
            logger.info(
                f"[AI] OpenAI response received "
                f"(attempt={attempt+1}, rate_limit_retries={rate_limit_count}, "
                f"len={len(raw_response)})"
            )
            result = validate_ai_summary_json(raw_response)
            result["_raw_response"] = raw_response
            result["_ai_status"] = "completed"
            return result

        except RateLimitError as e:
            rate_limit_count += 1

            if rate_limit_count > rate_limit_max:
                logger.error(
                    f"[AI] 429 rate limit: {rate_limit_count} retries exhausted. "
                    f"category={e.category}"
                )
                return {
                    **_DEFAULT_SUMMARY,
                    "_ai_status": "ai_rate_limited",
                    "_rate_limit_category": e.category,
                    "_raw_response": "",
                    "_error": f"429 {e.category} after {rate_limit_count} retries",
                }

            # Retry-After ヘッダがあれば優先、なければ固定 backoff
            if e.retry_after and e.retry_after > 0:
                wait_sec = e.retry_after
            else:
                wait_sec = _RATE_LIMIT_BACKOFF[rate_limit_count - 1]

            logger.warning(
                f"[AI] 429 retry {rate_limit_count}/{rate_limit_max}: "
                f"category={e.category}, waiting {wait_sec}s"
            )
            time.sleep(wait_sec)
            continue  # 429 リトライ — attempt は増やさない

        except json.JSONDecodeError as e:
            logger.warning(
                f"[AI] JSON parse failed (attempt {attempt+1}): {e}"
            )
            if attempt < max_retries:
                attempt += 1
                continue
            return {
                **_DEFAULT_SUMMARY,
                "_ai_status": "ai_failed",
                "_raw_response": raw_response if 'raw_response' in dir() else "",
                "_error": f"JSON parse failed: {e}",
            }

        except Exception as e:
            # Windows環境でのエラーメッセージ文字化け防止
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            logger.error(f"[AI] API call failed: {err_msg}")
            return {
                **_DEFAULT_SUMMARY,
                "_ai_status": "ai_failed",
                "_raw_response": "",
                "_error": err_msg,
            }
