#!/usr/bin/env python3
"""summary_ai_client.py — OpenAI Responses API による要約生成

gpt-5.4-mini をデフォルトモデルとし、json_schema で構造化出力を強制する。
429 / 5xx の再試行は最大2回まで（exponential backoff）。
high 優先度で失敗時のみ gpt-5.4 にフォールバック。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("summary_ai_client")


# ============================================================
# JSON Schema 定義（V2: 増減理由の箇条書き整形専用）
# ============================================================
SUMMARY_JSON_SCHEMA = {
    "name": "earnings_reason_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "company_reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "全社の増減理由を箇条書き化。各項目は40文字以内。最大3項目。元テキストの情報のみ使用。",
            },
            "segment_reasons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["segment_name", "reason"],
                    "additionalProperties": False,
                },
                "description": "セグメント別の増減理由。セグメント名と理由(40文字以内)のペア。",
            },
        },
        "required": ["company_reasons", "segment_reasons"],
        "additionalProperties": False,
    },
}

# V1 互換スキーマ（決算短信以外用）
SUMMARY_JSON_SCHEMA_V1 = {
    "name": "tdnet_disclosure_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "日本語1行要約（30文字以内）。開示の核心を簡潔に。",
            },
            "bullets": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
                "description": "ポイント3つ。それぞれ40文字以内。",
            },
            "tone": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed", "cautious"],
                "description": "開示全体のトーン",
            },
            "needs_review": {
                "type": "boolean",
                "description": "情報不足や判断困難でレビューが必要な場合にtrue",
            },
        },
        "required": ["headline", "bullets", "tone", "needs_review"],
        "additionalProperties": False,
    },
}

# ============================================================
# システムプロンプト（V2: 整形専用）
# ============================================================
_SYSTEM_PROMPT_V2 = """\
あなたは日本の上場企業の決算短信テキストを箇条書きに整形するアシスタントです。

与えられたテキストは決算短信の「経営成績の概況」から抽出された増減理由です。
売上高・営業利益の増減要因だけを簡潔な箇条書きに整形してください。

絶対に守るルール:
1. 入力テキストに書かれている情報のみを使用すること
2. 数値を自分で生成・計算してはいけない
3. YOY/QoQ等の比率を計算してはいけない
4. 推測や補足情報を追加してはいけない
5. 「情報不足のため確認が必要」等の逃げ文を生成してはいけない
6. 不要な定型文（「引き続き努めてまいります」等）は削除すること
7. 各項目は40文字以内で簡潔に
8. セグメント理由にはセグメント名を含めること

出力してはいけない情報:
- 1株当たり利益／損失
- 1口当たり分配金
- 注記（(注)、(注1)、(注2)等）
- 物件数、件数、拠点数だけの説明
- 顧客所在地による分類説明
- 「売上高は◯◯百万円増加」のような金額だけの説明

優先して出力すべき情報:
- 受注・販売動向（好調、堅調、低迷等）
- 原価・コスト要因（原材料高、コスト削減等）
- 價格改定・値上げの影響
- 為替影響
- 新規連結・M&A
- 減損・一過性損益
- 市場環境・需要動向
"""

# V1 互換プロンプト（決算短信以外用）
_SYSTEM_PROMPT_V1 = """\
あなたは日本の上場企業のTDNET適時開示を要約する金融アナリストです。
与えられた開示情報から、投資家向けの速報要約を生成してください。

ルール:
1. headline は開示の核心を30文字以内で簡潔に記述
2. bullets は重要ポイントを3つ、各40文字以内
3. tone は開示全体の印象（positive/negative/neutral/mixed/cautious）
4. 情報が不足して正確な要約が困難な場合は needs_review を true にする
5. 数値は可能な限り具体的に記載（前年比、増減率など）
6. 推測や解釈は避け、開示内容に基づいた事実のみ記述
"""

# 後方互換
_SYSTEM_PROMPT = _SYSTEM_PROMPT_V1


# ============================================================
# モデル設定
# ============================================================
DEFAULT_MODEL = "gpt-5.4-mini"
COST_PRIORITY_MODEL = "gpt-5-mini"
HIGH_PRIORITY_FALLBACK_MODEL = "gpt-5.4"


# ============================================================
# API クライアント
# ============================================================

def _get_openai_client():
    """OpenAI クライアントを取得（遅延インポート）"""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai パッケージが必要です。pip install openai でインストールしてください。"
        )

    # env_loader 経由で読み込み済みの環境変数を使用
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        from .env_loader import _ENV_PATH
        raise ValueError(
            f"OPENAI_API_KEY が未設定です。"
            f" .env 探索先: {_ENV_PATH}"
            f" — .env に OPENAI_API_KEY=<値> を設定してください。"
        )

    return OpenAI(api_key=api_key)


def _call_responses_api(
    client,
    input_text: str,
    model: str,
    schema: dict | None = None,
    system_prompt: str = "",
) -> tuple[dict, dict]:
    """OpenAI Responses API を呼び出す。

    Returns
    -------
    (parsed_result, usage_info)
    """
    use_schema = schema or SUMMARY_JSON_SCHEMA
    use_prompt = system_prompt or _SYSTEM_PROMPT

    response = client.responses.create(
        model=model,
        instructions=use_prompt,
        input=input_text,
        text={
            "format": {
                "type": "json_schema",
                "name": use_schema["name"],
                "strict": use_schema["strict"],
                "schema": use_schema["schema"],
            }
        },
    )

    # レスポンスからテキスト出力を取得
    output_text = ""
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    output_text = content.text
                    break

    if not output_text:
        raise ValueError("API response に出力テキストが含まれていません")

    parsed = json.loads(output_text)

    # usage 実測値
    usage_info = {
        "input_tokens": response.usage.input_tokens if response.usage else 0,
        "output_tokens": response.usage.output_tokens if response.usage else 0,
        "model_used": model,
    }

    return parsed, usage_info


def call_summary_api(
    input_text: str,
    model: str = "",
    priority: str = "normal",
    max_retries: int = 2,
) -> tuple[dict, dict]:
    """AI要約 API を呼び出す（リトライ付き）。

    Parameters
    ----------
    input_text : str
        要約対象テキスト
    model : str
        使用モデル（空の場合はデフォルト）
    priority : str
        優先度（high の場合は上位モデルへのフォールバックあり）
    max_retries : int
        最大リトライ回数（429/5xx 用）

    Returns
    -------
    (result_dict, usage_dict)

    Raises
    ------
    Exception
        全リトライ失敗時
    """
    if not model:
        model = DEFAULT_MODEL

    client = _get_openai_client()
    last_error: Optional[Exception] = None

    # 1. 通常モデルでリトライ
    for attempt in range(1 + max_retries):
        try:
            result, usage = _call_responses_api(client, input_text, model)
            logger.info(
                f"[AI] success model={model} attempt={attempt + 1} "
                f"tokens={usage['input_tokens']}+{usage['output_tokens']}"
            )
            return result, usage

        except Exception as e:
            last_error = e
            error_str = str(e)
            is_retryable = (
                "429" in error_str
                or "rate_limit" in error_str.lower()
                or "500" in error_str
                or "502" in error_str
                or "503" in error_str
            )

            if is_retryable and attempt < max_retries:
                wait = (attempt + 1) * 2  # 2s, 4s
                logger.warning(
                    f"[AI] retryable error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"waiting {wait}s..."
                )
                time.sleep(wait)
                continue
            else:
                logger.error(f"[AI] non-retryable error or max retries reached: {e}")
                break

    # 2. high 優先度の場合のみ上位モデルにフォールバック
    if priority == "high" and model != HIGH_PRIORITY_FALLBACK_MODEL:
        logger.warning(
            f"[AI] falling back to {HIGH_PRIORITY_FALLBACK_MODEL} for high-priority item"
        )
        try:
            result, usage = _call_responses_api(client, input_text, HIGH_PRIORITY_FALLBACK_MODEL)
            logger.info(
                f"[AI] fallback success model={HIGH_PRIORITY_FALLBACK_MODEL} "
                f"tokens={usage['input_tokens']}+{usage['output_tokens']}"
            )
            return result, usage
        except Exception as fallback_err:
            logger.error(f"[AI] fallback also failed: {fallback_err}")
            last_error = fallback_err

    raise last_error or RuntimeError("AI summary API call failed")


def call_reason_format_api(
    reason_text: str,
    segment_texts: dict[str, str] | None = None,
    model: str = "",
    max_retries: int = 2,
) -> tuple[dict, dict]:
    """V2: 増減理由テキストを箇条書きに整形する。

    Parameters
    ----------
    reason_text : 全社の増減理由テキスト
    segment_texts : {セグメント名: 理由テキスト}
    model : 使用モデル
    max_retries : リトライ回数

    Returns
    -------
    ({"company_reasons": [...], "segment_reasons": [...]}, usage_dict)
    """
    if not model:
        model = DEFAULT_MODEL

    # AI への入力構築
    input_parts = [f"【全社の増減理由】\n{reason_text}"]
    if segment_texts:
        for seg_name, seg_reason in segment_texts.items():
            input_parts.append(f"【{seg_name}セグメント】\n{seg_reason}")

    input_text = "\n\n".join(input_parts)

    client = _get_openai_client()
    last_error: Optional[Exception] = None

    for attempt in range(1 + max_retries):
        try:
            result, usage = _call_responses_api(
                client, input_text, model,
                schema=SUMMARY_JSON_SCHEMA,
                system_prompt=_SYSTEM_PROMPT_V2,
            )
            logger.info(
                f"[AI-V2] success model={model} attempt={attempt + 1} "
                f"tokens={usage['input_tokens']}+{usage['output_tokens']}"
            )
            return result, usage
        except Exception as e:
            last_error = e
            error_str = str(e)
            is_retryable = any(code in error_str for code in ("429", "500", "502", "503"))
            if is_retryable and attempt < max_retries:
                wait = (attempt + 1) * 2
                logger.warning(f"[AI-V2] retryable error attempt {attempt + 1}: {e}. waiting {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"[AI-V2] error: {e}")
                break

    raise last_error or RuntimeError("AI reason format API call failed")
