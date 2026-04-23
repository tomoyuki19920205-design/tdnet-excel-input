"""lib/backfill/segment_ai_fallback.py — セグメント AI フォールバック抽出

役割:
  XBRL + PDF ルール抽出が両方失敗し、かつ normal_skip でない案件のみを対象に
  ChatGPT API (chat.completions) でセグメント名・売上・利益を最終的に抽出する。

制限:
  - AIは全件に使わない。quarantined 候補（特定の失敗理由）のみ対象。
  - OPENAI_API_KEY 環境変数を使用（既存実装に準拠）。
  - モデル出力は JSON 強制。
  - 推測禁止。本文にある値のみ返す。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger("backfill.worker_v4")


def _safe_text(s: object) -> str:
    """テキストを UTF-8 安全な str に正規化する。

    PDF 由来テキストや例外メッセージに含まれるサロゲートペア・不正バイトを除去し、
    Windows 環境で 'ascii' codec エラーが発生しないようにする。
    OpenAI API に渡す前に必ず通すこと。
    """
    if s is None:
        return ""
    text = str(s)
    # サロゲートや不正なコードポイントを UTF-8 で除去 / 置換してラウンドトリップ
    try:
        return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # フォールバック: replace で落として返す
        try:
            return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        except Exception:
            return ""


# ------------------------------------------------------------------
# AI フォールバックを起動する失敗理由のホワイトリスト
# single_segment_omitted / segment_disclosure_omitted / non_operating_target_etf
# は送らない（仕様変更8）
# ------------------------------------------------------------------
_AI_FALLBACK_REASONS: frozenset[str] = frozenset([
    "no_valid_horizontal_segment_table",
    "no_records",
    "too_few_sales",
    "too_few_valid_segments",
    "v4_no_segments",                    # PDF 抽出で取れなかった場合
    "v4_no_records_after_conversion",
    "no_extraction_attempted",
])

# ------------------------------------------------------------------
# 除外セグメント名（合計行・調整額・全社など）
# ------------------------------------------------------------------
_EXCLUDE_SEGMENT_NAME_PATTERNS: list[str] = [
    "合計", "計", "調整額", "調整", "全社", "消去",
    "報告セグメント計", "合計/消去", "合計・消去",
]

# ------------------------------------------------------------------
# System / User プロンプト
# ------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "あなたは日本の決算短信からセグメント情報を構造化抽出するアシスタントです。\n"
    "単一セグメント・記載省略・ETF/REIT等でセグメント表が存在しない場合は no_segments を返してください。\n"
    "推測で数値を埋めず、本文にある値だけ返してください。\n"
    "売上高と利益（営業利益、セグメント利益など）を優先してください。"
)

_USER_PROMPT_TMPL = """\
以下は日本企業の決算短信テキスト抜粋です。
セグメント情報を読み取り、指定の JSON スキーマのみを出力してください。
他の文章・説明・コードブロックは一切含めないでください。

企業コード (ticker): {ticker}
開示タイトル: {title}

--- テキスト抜粋 ---
{text_excerpt}
--- 抜粋終わり ---

出力 JSON スキーマ:
{{
  "result": "segments" または "no_segments",
  "segments": [
    {{
      "segment_name": "string",
      "sales": number または null,
      "profit": number または null,
      "period_type": "current" または "previous" または "unknown"
    }}
  ]
}}

ルール:
- 日本語の原文から読み取れる内容のみ（推測禁止）
- 合計行・調整額・全社・消去はセグメントとして返さない
- sales も profit も null の行も含めない
- 少なくとも 2 セグメント、または有効な売上/利益列がある時だけ segments を返す
- セグメント表が存在しない場合は result="no_segments", segments=[]
- 数値はカンマなし整数（△/▲は負数: 例 △656 → -656）
"""


def _is_ai_fallback_applicable(pdf_error: str) -> bool:
    """PDF 失敗理由が AI フォールバック対象か判定する。"""
    if not pdf_error:
        return False
    # "normal_skip:" で始まる場合は除外（normal skip は AI に送らない）
    if pdf_error.startswith("normal_skip:"):
        return False
    # ホワイトリストの失敗理由のいずれかを含む場合のみ対象
    for reason in _AI_FALLBACK_REASONS:
        if reason in pdf_error:
            return True
    return False


def _extract_text_from_pdf(pdf_path: str, max_chars: int = 15_000) -> str:
    """PDF からテキストを抽出する（セグメント周辺ページ優先、最大 max_chars 文字）。

    セグメント関連キーワードが含まれるページを優先して返す。
    無理な場合は先頭から順番に取得。
    """
    try:
        import fitz  # type: ignore  # pymupdf
    except ImportError:
        logger.warning("[v4] AI FALLBACK: pymupdf not installed, cannot extract PDF text")
        return ""

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.warning("[v4] AI FALLBACK: PDF open error: %s", e)
        return ""

    _SEGMENT_KEYWORDS = [
        "セグメント情報", "事業セグメント", "報告セグメント", "セグメント別",
        "事業の種類別", "所在地別", "売上高", "営業収益",
    ]

    try:
        # まずセグメント関連ページをスコアリング
        page_texts: list[tuple[int, str]] = []
        for page_idx in range(len(doc)):
            try:
                page_text = doc[page_idx].get_text()
            except Exception:
                continue
            score = sum(1 for kw in _SEGMENT_KEYWORDS if kw in page_text)
            page_texts.append((score, page_text))

        # スコア降順でソートし、上位ページを優先
        page_texts.sort(key=lambda x: -x[0])

        # スコアが高いページから順に取得
        combined = ""
        for _score, pt in page_texts:
            if len(combined) >= max_chars:
                break
            combined += pt
        return combined[:max_chars]
    except Exception as e:
        logger.warning("[v4] AI FALLBACK: text extraction error: %s", e)
        return ""
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _call_openai_text(
    system_prompt: str,
    user_prompt: str,
    model: str = "gpt-4o-mini",
    timeout: float = 45.0,
    max_retries: int = 1,
) -> tuple[str, str]:
    """OpenAI chat.completions API を呼び出す。

    Returns:
        (response_text, error_reason)
        成功時: (json_text, "")
        失敗時: ("", error_reason)
    """
    try:
        import openai  # type: ignore
    except ImportError:
        return "", "import_error (openai not installed)"

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return "", "no_api_key"

    try:
        client = openai.OpenAI(api_key=api_key, timeout=timeout)
    except Exception as e:
        # client_init_error にも生の e を入れない
        return "", _safe_text(f"client_init_error: {e}")

    # ── payload 構築直前: 全文字列フィールドを _safe_text() で再サニタイズ ──
    logger.debug("[ai-debug] before_payload_build")
    _sys_content = _safe_text(system_prompt)
    _usr_content = _safe_text(user_prompt)
    messages = [
        {"role": "system", "content": _sys_content},
        {"role": "user",   "content": _usr_content},
    ]
    logger.debug("[ai-debug] after_payload_build sys_len=%d usr_len=%d",
                 len(_sys_content), len(_usr_content))

    last_err = ""
    for attempt in range(max_retries + 1):
        logger.debug("[ai-debug] before_openai_call attempt=%d", attempt)
        try:
            response = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=messages,
                max_tokens=1024,
                temperature=0.0,
            )
            logger.debug("[ai-debug] after_openai_call attempt=%d", attempt)
            content = response.choices[0].message.content or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            return content, ""
        except Exception as e:
            # ① 例外型名を ASCII-safe に取得
            _exc_type = _safe_text(type(e).__name__)
            # ② 例外メッセージを _safe_text で完全サニタイズ（カスケード防止）
            _e_msg = _safe_text(str(e))
            last_err = _safe_text(f"api_error({_exc_type}): {_e_msg}")
            logger.debug("[ai-debug] exception_stage=attempt_%d exc_type=%s",
                         attempt, _exc_type)
            if attempt < max_retries:
                # logger に渡す文字列は全て変数化済みの safe 文字列のみ
                logger.warning(
                    "[v4] AI FALLBACK API retry %d/%d exc=%s msg=%s",
                    attempt + 1, max_retries + 1, _exc_type, _e_msg,
                )
                time.sleep(1.0)

    return "", last_err



def _parse_ai_json(text: str) -> tuple[dict | None, str]:
    """AI レスポンステキストから JSON をパースする。"""
    text = text.strip()
    # ```json ブロックを削除（念のため）
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    if not text:
        return None, "ai_parse_error (empty response)"

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None, "ai_parse_error (not a dict)"
        return data, ""
    except json.JSONDecodeError as e:
        return None, f"ai_parse_error: {e}"


def _filter_ai_segments(segments: list[dict]) -> list[dict]:
    """除外パターンに一致するセグメント行を除去する。

    - segment_name が「合計」「計」「調整額」「全社」「消去」だけの行
    - sales も profit も null の行
    """
    filtered = []
    for seg in segments:
        name = (seg.get("segment_name") or "").strip()
        if not name:
            continue
        # 除外パターン判定
        if any(pat in name for pat in _EXCLUDE_SEGMENT_NAME_PATTERNS):
            continue
        # sales と profit が両方 null / 非数値なら除外
        sales = seg.get("sales")
        profit = seg.get("profit")
        if not isinstance(sales, (int, float)) and not isinstance(profit, (int, float)):
            continue
        filtered.append(seg)
    return filtered


def extract_segments_with_ai(
    pdf_path: str,
    ticker: str,
    title: str | None = None,
    model: str = "gpt-4o-mini",
) -> dict | None:
    """PDF から AI を使ってセグメント情報を抽出する（最終フォールバック）。

    Args:
        pdf_path: PDF ファイルパス
        ticker: ティッカー（ログ・プロンプト用）
        title: 開示タイトル（ログ・プロンプト用）
        model: 使用する OpenAI モデル

    Returns:
        dict または None
        {
            "success": bool,
            "reason": "ai_ok" | "ai_no_segments" | "ai_parse_error" | "ai_api_error",
            "segments": [
                {
                    "segment_name": str,
                    "sales": int | None,
                    "profit": int | None,
                    "period_type": "current" | "previous" | "unknown",
                }
            ],
            "raw_text": str,   # モデル応答生文字列（ログ用）
        }
        前提条件不満（API キーなし等）の場合は None
    """
    # API キー事前確認
    if not os.environ.get("OPENAI_API_KEY", ""):
        logger.warning("[v4] AI FALLBACK: OPENAI_API_KEY not set, skipping")
        return None

    logger.info(
        "[v4] AI FALLBACK START pdf=%s ticker=%s",
        pdf_path, ticker,
    )

    # PDF テキスト抽出（サロゲート・等の不正コードポイントを事前除去）
    text_excerpt = _safe_text(_extract_text_from_pdf(pdf_path) if pdf_path else "")
    if not text_excerpt:
        logger.warning(
            "[v4] AI FALLBACK: no text extracted from PDF pdf=%s ticker=%s",
            pdf_path, ticker,
        )
        # テキストが取れなくてもプロンプトは送る（空テキストでも試みる）

    # プロンプト構築 → format() 後の最終文字列も _safe_text() を通す（二重防御）
    user_prompt = _safe_text(_USER_PROMPT_TMPL.format(
        ticker=_safe_text(ticker),
        title=_safe_text(title or ""),
        text_excerpt=text_excerpt or "(テキスト抽出不可)",
    ))

    # API 呼び出し
    # DEBUG: dump prompt to file for inspection (remove before production)
    try:
        from pathlib import Path
        Path("tmp").mkdir(exist_ok=True)
        Path("tmp/ai_user_prompt.txt").write_text(user_prompt, encoding="utf-8")
        Path("tmp/ai_system_prompt.txt").write_text(_SYSTEM_PROMPT, encoding="utf-8")
    except Exception:
        pass

    raw_text, api_err = _call_openai_text(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=model,
    )

    if api_err:
        # api_err 自体に非 ASCII 文字が含まれる可能性があるため安全な形式のみログ出力
        _api_err_safe = _safe_text(api_err)
        logger.warning(
            "[v4] AI FALLBACK API_ERROR pdf=%s ticker=%s err=%s",
            pdf_path, ticker, _api_err_safe,
        )
        return {
            "success": False,
            "reason": "ai_api_error",
            "segments": [],
            "raw_text": raw_text,
        }

    # JSON パース
    data, parse_err = _parse_ai_json(raw_text)
    if parse_err or data is None:
        logger.warning(
            "[v4] AI FALLBACK PARSE_ERROR pdf=%s ticker=%s err=%s raw=%r",
            pdf_path, ticker, parse_err, raw_text[:200],
        )
        return {
            "success": False,
            "reason": "ai_parse_error",
            "segments": [],
            "raw_text": raw_text,
        }

    # result フィールド確認
    result_field = data.get("result", "")
    if result_field == "no_segments":
        logger.info(
            "[v4] AI FALLBACK NO_SEGMENTS pdf=%s ticker=%s",
            pdf_path, ticker,
        )
        return {
            "success": False,
            "reason": "ai_no_segments",
            "segments": [],
            "raw_text": raw_text,
        }

    # セグメントリスト取得 & フィルタ
    raw_segments = data.get("segments", [])
    if not isinstance(raw_segments, list):
        raw_segments = []

    segments = _filter_ai_segments(raw_segments)

    # 品質ゲート（変更4）
    # - 有効 segment >= 2 → success=True (ai_ok)
    # - 1件のみだが sales/profit どちらか有効 → success=True (ai_ok, partial扱いは呼び出し元)
    # - それ未満 → ai_no_segments 扱い
    if len(segments) == 0:
        logger.info(
            "[v4] AI FALLBACK NO_SEGMENTS pdf=%s ticker=%s (all filtered out)",
            pdf_path, ticker,
        )
        return {
            "success": False,
            "reason": "ai_no_segments",
            "segments": [],
            "raw_text": raw_text,
        }

    logger.info(
        "[v4] AI FALLBACK OK pdf=%s ticker=%s segments=%d",
        pdf_path, ticker, len(segments),
    )
    return {
        "success": True,
        "reason": "ai_ok",
        "segments": segments,
        "raw_text": raw_text,
    }


# ------------------------------------------------------------------
# period / quarter 補完ヘルパー（変更1・変更4）
# ------------------------------------------------------------------

_QUARTER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"第[1１]四半期"), "Q1"),
    (re.compile(r"第[2２]四半期"), "Q2"),
    (re.compile(r"第[3３]四半期"), "Q3"),
    (re.compile(r"通期|期末|本決算|年度決算"), "FY"),
]

# ページテキストから (period_type, quarter) を解析するパターン（長いものを先に）
_PAGE_Q_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"当第[1１]四半期"), "current", "Q1"),
    (re.compile(r"当第[2２]四半期"), "current", "Q2"),
    (re.compile(r"当第[3３]四半期"), "current", "Q3"),
    (re.compile(r"前第[1１]四半期"), "previous", "Q1"),
    (re.compile(r"前第[2２]四半期"), "previous", "Q2"),
    (re.compile(r"前第[3３]四半期"), "previous", "Q3"),
]

_CURRENT_TERM_RE = re.compile(r"当期")
_PREVIOUS_TERM_RE = re.compile(r"前期")


def _resolve_quarter_from_text(text: str) -> str:
    """テキスト（主にタイトル）から quarter を返す。解決できなければ 'unknown'。"""
    for pat, q in _QUARTER_PATTERNS:
        if pat.search(text):
            return q
    return "unknown"


def _resolve_period_from_page_text(text: str) -> tuple[str, str]:
    """ページテキストから (period_type, quarter) を返す。

    Returns:
        (period_type, quarter) — 解決できなければそれぞれ 'unknown'
    """
    # 「当第N四半期」「前第N四半期」を優先
    for pat, pt, q in _PAGE_Q_PATTERNS:
        if pat.search(text):
            return pt, q

    # 「当期」「前期」の単純ラベル
    has_current = bool(_CURRENT_TERM_RE.search(text))
    has_previous = bool(_PREVIOUS_TERM_RE.search(text))
    if has_current and not has_previous:
        return "current", "unknown"
    if has_previous and not has_current:
        return "previous", "unknown"
    if has_current and has_previous:
        return "mixed", "unknown"
    return "unknown", "unknown"


def resolve_ai_period_context(
    title: str,
    ai_segments: list[dict],
    page_text_hint: str = "",
) -> dict:
    """AI 成功後の period_type / quarter を補完する。

    優先順:
      1. title から quarter を判定 (reason="title_label")
      2. page_text_hint から period_type + quarter を判定 (reason="page_label")
      3. ai_segments の period_type 多数決 (reason="ai_only")
      4. 解決できなければ unknown (reason="unknown")

    Returns:
        {
            "period_type": "current" | "previous" | "mixed" | "unknown",
            "quarter": "Q1" | "Q2" | "Q3" | "FY" | "unknown",
            "confidence": "high" | "medium" | "low",
            "reason": "title_label" | "page_label" | "ai_only" | "unknown",
        }
    """
    title = title or ""
    page_text_hint = page_text_hint or ""

    quarter: str = "unknown"
    period_type: str = "unknown"
    confidence: str = "low"
    reason: str = "unknown"

    # ── Step 1: タイトルから quarter 判定 ──
    _q_from_title = _resolve_quarter_from_text(title)
    if _q_from_title != "unknown":
        quarter = _q_from_title
        confidence = "high"
        reason = "title_label"

    # ── Step 2: page_text から period_type / quarter を補完 ──
    if page_text_hint:
        _pt, _pq = _resolve_period_from_page_text(page_text_hint)
        # quarter 補完（タイトルで取れていない場合のみ）
        if _pq != "unknown" and quarter == "unknown":
            quarter = _pq
            if reason == "unknown":
                reason = "page_label"
            if confidence == "low":
                confidence = "medium"
        # period_type 補完
        if _pt != "unknown":
            period_type = _pt
            if reason == "unknown":
                reason = "page_label"
            if confidence == "low":
                confidence = "medium"

    # ── Step 3: AI segments の period_type 多数決（補助） ──
    if period_type == "unknown" and ai_segments:
        _votes = [s.get("period_type", "unknown") or "unknown" for s in ai_segments]
        _n_current = _votes.count("current")
        _n_previous = _votes.count("previous")
        if _n_current > 0 and _n_previous == 0:
            period_type = "current"
            if reason == "unknown":
                reason = "ai_only"
        elif _n_previous > 0 and _n_current == 0:
            period_type = "previous"
            if reason == "unknown":
                reason = "ai_only"
        elif _n_current > 0 and _n_previous > 0:
            period_type = "mixed"
            if reason == "unknown":
                reason = "ai_only"

    return {
        "period_type": period_type,
        "quarter": quarter,
        "confidence": confidence,
        "reason": reason,
    }
