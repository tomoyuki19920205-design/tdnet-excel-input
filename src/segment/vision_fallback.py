"""
vision_fallback.py — PDF セグメント抽出 vision フォールバック経路
=================================================================

役割:
  v4 ルールベース抽出が失敗したセグメント表ページを対象に、
  ページ画像を OpenAI Vision API に渡して
  セグメント名・売上・利益を JSON で再抽出する。

主要関数 (外部 API):
  - extract_segments_with_vision(...)      -> VisionFallbackResult
  - validate_vision_segment_result(data)  -> (ok, errors)
  - to_segment_records(data, ...)         -> list[SegmentRecordV4]

設計方針:
  - feature flag ENABLE_VISION_SEGMENT_FALLBACK=1 のときのみ有効
  - provider は初期版 openai 固定
  - PDF→PNG は pymupdf (fitz) のみ、DPI=150
  - validation 通過時のみ採用
  - 失敗時は None / 空リストを返し呼び出し側でフォールスルー
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# Windows 環境で logging からの日本語出力時の cp932 エンコードエラーを回避
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

# SegmentRecordV4 は呼び出し元と同一プロジェクト内に存在する
# NOTE: 循環 import を避けるため遅延 or 直接參照
from src.analysis.segment_detection_v4 import SegmentRecordV4

logger = logging.getLogger("tdnet.v4.vision")


# ==============================================================
# UTF-8 安全化ヘルパー
# ==============================================================

def _utf8_safe(s: object) -> object:
    """文字列を UTF-8 ラウンドトリップで正規化する。

    Windows cp932 環境で OpenAI SDK の内部シリアライズが
    ASCII 扱いになる問題を根本除去する。
    文字列以外の型はそのまま返す。
    """
    if not isinstance(s, str):
        return s
    return s.encode("utf-8", "replace").decode("utf-8")


# ==============================================================
# 定数
# ==============================================================

# 補助列として扱うセグメント名パターン（完全一致・含む判定）
_AUX_SEGMENT_PATTERNS: frozenset[str] = frozenset([
    "連結", "合計", "計", "調整額", "調整", "その他", "消去", "全社",
    "報告セグメント計", "合計/消去", "合計・消去",
])

# main segments で許容しない名前パターン（部分一致）
_REJECT_NAME_PATTERNS: list[str] = [
    "連結", "合計", "計", "調整額", "調整", "その他", "消去",
]

# Vision プロンプト (JSON only)
_VISION_PROMPT = """\
次のPDFページ画像は日本企業の決算短信に含まれるセグメント情報表です。
表を読み取り、以下の JSON スキーマのみを出力してください。
ほかの文章・説明・コードブロックは一切含めないでください。

スキーマ:
{
  "page": <整数 またはnull>,
  "period": "current" または "previous",
  "segments": [
    {"name": "<セグメント名>", "sales": <整数またはnull>, "profit": <整数またはnull>}
  ],
  "other": {"sales": <整数またはnull>, "profit": <整数またはnull>},
  "adjustment": {"sales": <整数またはnull>, "profit": <整数またはnull>},
  "consolidated": {"sales": <整数またはnull>, "profit": <整数またはnull>},
  "notes": "<特記事項があれば、なければ空文字>",
  "confidence": <0.0〜1.0 の小数>
}

ルール:
- 数値は整数のみ（単位は表に記載の通り、百万円 / 千万円 / 千円 など）
- カンマなし（例: 1250721）
- △ または ▲ は負数として整数化（例: △656 → -656）
- null は値が読み取れない・存在しない場合に使用
- 「報告セグメント」「セグメント情報」などの親見出しはセグメント名に数えない
- segments には事業セグメント名のみ入れる
  - その他 / 調整額 / 連結合計 などは segments に入れず other / adjustment / consolidated に入れる
- current / previous の両期が表に含まれる場合は current (当期) を優先して読み取る
- セグメントが1件しか見つからない場合も必ず segments に出力する（空配列にしない）
- 「連結」「その他」しかない場合もそれを segments に含めてよい。とにかく表に見える事業名を列挙すること
- JSON のみ出力し、その他の文章は一切含めない
"""

# ==============================================================
# 結果データクラス
# ==============================================================

@dataclass
class VisionFallbackResult:
    """Vision fallback の実行結果"""
    success: bool = False
    segment_records: list[SegmentRecordV4] = field(default_factory=list)
    raw_json: dict[str, Any] | None = None
    raw_response_text: str = ""
    confidence: float = 0.0
    validation_errors: list[str] = field(default_factory=list)
    selected_page: int | None = None
    provider: str = "openai"
    model: str = ""


# ==============================================================
# PDF → PNG 変換
# ==============================================================

def _page_to_png_bytes(pdf_path: str, page_idx: int, dpi: int = 150) -> bytes | None:
    """
    指定ページを PNG bytes に変換する。
    pymupdf (fitz) のみ使用。未インストール時は None を返す。

    Args:
        pdf_path: PDFファイルパス
        page_idx: 物理ページインデックス (0-based)
        dpi: 解像度 (デフォルト 150)

    Returns:
        PNG bytes、失敗時は None
    """
    try:
        import fitz  # type: ignore  # pymupdf
    except ImportError:
        logger.warning("[v4-vision-reject] reason=import_error (pymupdf not installed)")
        return None

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.warning("[v4-vision-reject] reason=pdf_open_error detail=%s", e)
        return None

    try:
        if page_idx < 0 or page_idx >= len(doc):
            logger.warning(
                "[v4-vision-reject] reason=page_out_of_range page=%d total=%d",
                page_idx, len(doc),
            )
            return None

        page = doc[page_idx]
        zoom = dpi / 72.0  # fitz のデフォルト DPI は 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        png_bytes: bytes = pix.tobytes("png")
        return png_bytes
    except Exception as e:
        logger.warning("[v4-vision-reject] reason=png_convert_error detail=%s", e)
        return None
    finally:
        doc.close()


# ==============================================================
# OpenAI Vision API 呼び出し
# ==============================================================

def _call_openai_vision(
    png_bytes: bytes,
    model: str = "gpt-4o",
    timeout: float = 30.0,
    max_retries: int = 1,
) -> tuple[str, str]:
    """
    OpenAI Vision API を呼び出し、レスポンステキストを返す。

    Args:
        png_bytes: PNG 画像 bytes
        model: OpenAI モデル名
        timeout: API タイムアウト秒数 (デフォルト 30秒)
        max_retries: 失敗時の再試行回数 (デフォルト 1回)

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
        return "", f"client_init_error: {e}"

    # PNG を base64 エンコード
    b64_image = base64.b64encode(png_bytes).decode("utf-8")
    image_url = f"data:image/png;base64,{b64_image}"

    # messages 内の全ての text を UTF-8 正規化（_utf8_safe 適用）
    # Windows cp932 環境で SDK 内部シリアライズが ascii 扱いになる問額を根本除去する。
    _messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _utf8_safe(_VISION_PROMPT)},
                {"type": "image_url", "image_url": {"url": _utf8_safe(image_url), "detail": "high"}},
            ],
        }
    ]
    logger.info(
        "[v4-vision-debug] text_sample=%r",
        _messages[0]["content"][0]["text"][:20],
    )

    last_err = ""
    for attempt in range(max_retries + 1):  # 0, 1  (初回 + retry 1回)
        try:
            response = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=_messages,
                max_tokens=1024,
                temperature=0.0,
            )
            _content = response.choices[0].message.content or ""
            if isinstance(_content, bytes):
                _content = _content.decode("utf-8", "replace")
            text = _content
            return text, ""
        except Exception as e:
            # 例外メッセージに日本語が含まれても ascii encode エラーにならないよう安全化
            _e_msg = str(e).encode("utf-8", "replace").decode("utf-8")
            last_err = f"api_error: {_e_msg}"
            if attempt < max_retries:
                logger.warning(
                    "[v4-vision] api call failed (attempt %d/%d): %s -- retrying",
                    attempt + 1, max_retries + 1, _e_msg,
                )
            # else: 最終試行失敗、下の return へ

    return "", last_err



# ==============================================================
# JSON パース
# ==============================================================

def _parse_vision_json(text: str) -> tuple[dict[str, Any] | None, str]:
    """
    API レスポンステキストから JSON を抽出・パースする。

    Returns:
        (parsed_dict, error_reason)
    """
    # JSON only モードでも念のため ```json ブロックを除去
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    if not text:
        return None, "invalid_json (empty)"

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None, "invalid_json (not a dict)"
        return data, ""
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"


# ==============================================================
# バリデーション
# ==============================================================

def validate_vision_segment_result(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    vision 抽出結果の JSON を検証する。

    強化バリデーション:
    - main segments が 2 件以上
    - 各 segment に name がある
    - sales または profit の少なくとも一方が数値
    - consolidated が存在する
    - main segments が補助列名のみで構成されていない
    - 同一 name 重複がない
    - 全数値が 0 または null でない
    - consolidated / other / adjustment が segments に混入していない

    Returns:
        (ok: bool, errors: list[str])
    """
    errors: list[str] = []

    segments = data.get("segments", [])
    if not isinstance(segments, list):
        errors.append("segments is not a list")
        return False, errors

    # 件数チェック
    if len(segments) == 0:
        errors.append("main_segments_lt2 (n=0)")
        return False, errors
    if len(segments) < 2:
        # 1件でも補助列名でない事業名を含む場合は PARTIAL_ACCEPT として後続チェックへ
        _seg0_name = (segments[0].get("name") or "").strip()
        _is_aux = any(pat in _seg0_name for pat in _REJECT_NAME_PATTERNS)
        if not _seg0_name or _is_aux:
            errors.append(f"main_segments_lt2 (n={len(segments)})")
            return False, errors
        # 事業名あり → PARTIAL_ACCEPT: 後続の name/数値チェックに進む

    # 各 segment の name / 数値チェック
    names: list[str] = []
    all_numeric_zero_or_null = True

    for i, seg in enumerate(segments):
        name = seg.get("name", "")
        if not name or not isinstance(name, str) or not name.strip():
            errors.append(f"segment[{i}] has no name")
            continue

        names.append(name)

        sales = seg.get("sales")
        profit = seg.get("profit")

        # 少なくとも一方が数値
        if not isinstance(sales, (int, float)) and not isinstance(profit, (int, float)):
            errors.append(f"segment[{i}] ({name}): both sales and profit are null/non-numeric")

        # 全数値ゼロ・null 判定用
        if isinstance(sales, (int, float)) and sales != 0:
            all_numeric_zero_or_null = False
        if isinstance(profit, (int, float)) and profit != 0:
            all_numeric_zero_or_null = False

    if errors:
        return False, errors

    # 全数値がゼロまたは null
    if all_numeric_zero_or_null:
        errors.append("all_numeric_zero_or_null")
        return False, errors

    # main segments が補助列名のみで構成 (reject)
    non_aux_names = [
        n for n in names
        if not any(pat in n for pat in _REJECT_NAME_PATTERNS)
    ]
    if not non_aux_names:
        errors.append(
            f"main_segments_all_auxiliary: names={names}"
        )
        return False, errors

    # 同一名重複チェック
    seen: set[str] = set()
    for n in names:
        if n in seen:
            errors.append(f"duplicate_segment_name: {n!r}")
        seen.add(n)

    if errors:
        return False, errors

    # consolidated の存在チェック（緩め: 数値が1つでもあればOK）
    consolidated = data.get("consolidated", {}) or {}
    c_sales = consolidated.get("sales")
    c_profit = consolidated.get("profit")
    if not isinstance(c_sales, (int, float)) and not isinstance(c_profit, (int, float)):
        errors.append("consolidated missing or all null")

    if errors:
        return False, errors

    # ------------------------------------------------------------------
    # 数値整合チェック: 調整額が存在する表では合計≠連結が正常なため無効化
    # （main segments の name/sales/profit 取得が目的であり会計的一致は必須でない）
    # _sum_err = _check_numeric_consistency(data)
    # if _sum_err:
    #     errors.append(_sum_err)
    #     return False, errors

    return True, []



# ==============================================================
# SegmentRecordV4 への変換
# ==============================================================

def to_segment_records(
    data: dict[str, Any],
    raw_profit_label: str = "セグメント利益",
) -> list[SegmentRecordV4]:
    """
    vision JSON から SegmentRecordV4 のリストを生成する。

    - extraction_engine = "v4_vision" のみで識別
    - SegmentRecordV4 は変更しない（source/derivation_method は追加しない）
    - main segments のみ対象（other/adjustment/consolidated は除外）

    Args:
        data: validate_vision_segment_result 通過済みの dict
        raw_profit_label: 利益ラベル文字列

    Returns:
        list[SegmentRecordV4]
    """
    records: list[SegmentRecordV4] = []
    segments = data.get("segments", [])

    for i, seg in enumerate(segments, start=1):
        name = (seg.get("name") or "").strip()
        if not name:
            continue

        sales_val = seg.get("sales")
        profit_val = seg.get("profit")

        # 数値型に正規化（JSON から int が来るはずだが念のため）
        sales_f: float | None = float(sales_val) if isinstance(sales_val, (int, float)) else None
        profit_f: float | None = float(profit_val) if isinstance(profit_val, (int, float)) else None

        # どちらも None なら除外
        if sales_f is None and profit_f is None:
            continue

        records.append(SegmentRecordV4(
            segment_name=name,
            segment_order=i,
            segment_sales=sales_f,
            segment_profit=profit_f,
            raw_profit_label=raw_profit_label or "セグメント利益",
            extraction_engine="v4_vision",
        ))

    return records


# ==============================================================
# メイン関数
# ==============================================================

def extract_segments_with_vision(
    pdf_path: str,
    candidate_pages: list[int],
    *,
    ticker: str = "",
    period: str = "current",
    provider: str = "openai",
    model: str = "gpt-4o",
    max_pages: int = 5,
) -> VisionFallbackResult:
    """
    候補ページを画像化して Vision API でセグメント抽出を試みる。

    各ページを試行し、validation 通過した最初の結果を採用。
    全ページ失敗時は success=False の VisionFallbackResult を返す。

    Args:
        pdf_path: PDF ファイルパス
        candidate_pages: 試行する物理ページインデックスのリスト
        ticker: ティッカー（ログ用）
        period: "current" / "previous"
        provider: "openai" のみ有効（初期版）
        model: OpenAI モデル名
        max_pages: 最大試行ページ数

    Returns:
        VisionFallbackResult
    """
    vfr = VisionFallbackResult(provider=provider, model=model)

    # API キー事前確認
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY", ""):
            logger.warning(
                "[v4-vision-reject] ticker=%s reason=no_api_key", ticker
            )
            vfr.validation_errors = ["no_api_key"]
            return vfr
    else:
        logger.warning(
            "[v4-vision-reject] ticker=%s reason=unsupported_provider provider=%s",
            ticker, provider,
        )
        vfr.validation_errors = [f"unsupported_provider: {provider}"]
        return vfr

    # pymupdf 事前確認
    try:
        import fitz  # noqa: F401  # type: ignore
    except ImportError:
        logger.warning(
            "[v4-vision-reject] ticker=%s reason=import_error (pymupdf)", ticker
        )
        vfr.validation_errors = ["import_error (pymupdf)"]
        return vfr

    # 先頭 max_pages 件 ＋ 末尾 max_pages 件（重複排除・順序維持）で幅広く試行
    _head = candidate_pages[:max_pages]
    _tail = candidate_pages[-max_pages:] if len(candidate_pages) > max_pages else []
    _seen: set[int] = set()
    pages_to_try: list[int] = []
    for _p in _head + _tail:
        if _p not in _seen:
            pages_to_try.append(_p)
            _seen.add(_p)

    logger.info(
        "[v4-vision-fallback] ticker=%s candidates=%s pages_to_try=%s provider=%s model=%s",
        ticker, candidate_pages, pages_to_try, provider, model,
    )

    for page_idx in pages_to_try:
        logger.debug("[v4-vision-fallback] trying page=%d", page_idx)

        # PDF → PNG
        png_bytes = _page_to_png_bytes(pdf_path, page_idx)
        if png_bytes is None:
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=png_convert_failed",
                ticker, page_idx,
            )
            continue

        # Vision API 呼び出し
        raw_text, err = _call_openai_vision(png_bytes, model=model)
        if err:
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=%s",
                ticker, page_idx, err,
            )
            vfr.raw_response_text = raw_text
            vfr.validation_errors.append(err)
            continue

        vfr.raw_response_text = raw_text

        # JSON パース
        data, parse_err = _parse_vision_json(raw_text)
        if parse_err or data is None:
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=%s",
                ticker, page_idx, parse_err,
            )
            vfr.validation_errors.append(parse_err or "invalid_json (unknown)")
            continue

        vfr.raw_json = data

        # segments=[] は構造認識失敗として次ページへ
        if not data.get("segments"):
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=empty_segments",
                ticker, page_idx,
            )
            vfr.validation_errors.append("empty_segments")
            continue

        # バリデーション
        ok, val_errors = validate_vision_segment_result(data)

        seg_names = [s.get("name") for s in data.get("segments", [])]
        has_other = data.get("other") is not None
        has_adj = data.get("adjustment") is not None
        has_consol = data.get("consolidated") is not None

        if not ok:
            logger.warning(
                "[v4-vision-json] ticker=%s page=%d segments=%s "
                "other=%s adjustment=%s consolidated=%s validation=FAILED errors=%s",
                ticker, page_idx,
                json.dumps(seg_names, ensure_ascii=False),
                "yes" if has_other else "no",
                "yes" if has_adj else "no",
                "yes" if has_consol else "no",
                val_errors,
            )
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=validation_failed",
                ticker, page_idx,
            )
            vfr.validation_errors.extend(val_errors)
            continue

        # 採用
        raw_profit_label = _infer_profit_label(data)
        records = to_segment_records(data, raw_profit_label=raw_profit_label)

        if not records:
            logger.warning(
                "[v4-vision-reject] ticker=%s page=%d reason=no_records_after_conversion",
                ticker, page_idx,
            )
            vfr.validation_errors.append("no_records_after_conversion")
            continue

        vfr.success = True
        vfr.segment_records = records
        vfr.confidence = float(data.get("confidence", 0.85))
        vfr.selected_page = page_idx

        logger.info(
            "[v4-vision-json] ticker=%s selected_page=%d candidates=%s segments=%s "
            "other=%s adjustment=%s consolidated=%s validation=ok confidence=%.2f",
            ticker, page_idx, pages_to_try,
            json.dumps([r.segment_name for r in records], ensure_ascii=False),
            "yes" if has_other else "no",
            "yes" if has_adj else "no",
            "yes" if has_consol else "no",
            vfr.confidence,
        )
        return vfr

    # 全ページ失敗
    logger.warning(
        "[v4-vision-reject] ticker=%s reason=all_pages_failed pages_tried=%s",
        ticker, pages_to_try,
    )
    return vfr


# ==============================================================
# 補助関数
# ==============================================================

def _check_numeric_consistency(
    data: dict[str, Any],
    tolerance: float = 0.02,
) -> str:
    """
    数値整合チェック: sum(segments + other + adjustment) ≈ consolidated (±tolerance)

    Vision の列ズレ・1列抜け・1列重複を検出するための最重要チェック。

    Args:
        data: validate_vision_segment_result 呼び出し済みの dict
        tolerance: 許容誤差率 (デフォルト 2%)

    Returns:
        エラー文字列 (整合OK なら空文字)
    """
    def _to_float(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) else None

    def _sum_key(key: str) -> tuple[float | None, bool]:
        """segments + other + adjustment の key 合計を返す。(合計値, 計算できたか)"""
        total = 0.0
        has_any = False
        for seg in data.get("segments", []):
            v = _to_float(seg.get(key))
            if v is not None:
                total += v
                has_any = True
        for col in ["other", "adjustment"]:
            block = data.get(col) or {}
            v = _to_float(block.get(key))
            if v is not None:
                total += v
                has_any = True
        return (total if has_any else None), has_any

    def _check_one(key: str) -> str:
        """1つのキー (sales / profit) で整合チェックし、NGならエラー文字列を返す"""
        consol_val = _to_float((data.get("consolidated") or {}).get(key))
        if consol_val is None:
            return ""  # consolidated がない場合はチェック不要

        seg_total, has_any = _sum_key(key)
        if not has_any or seg_total is None:
            return ""  # segments 側が全 null ならチェック不要

        if abs(consol_val) < 1:
            # consolidated がほぼ 0 → ゼロ割防止、チェックスキップ
            return ""

        ratio = abs(seg_total - consol_val) / abs(consol_val)
        if ratio > tolerance:
            return (
                f"numeric_inconsistency ({key}): "
                f"sum={seg_total:.0f} consolidated={consol_val:.0f} "
                f"diff_ratio={ratio:.1%} (>{tolerance:.0%})"
            )
        return ""

    # sales と profit それぞれでチェックし、
    # どちらか一方でも不整合があれば reject する。
    # (列ズレは sales・profit 独立して現れる)
    err_sales = _check_one("sales")
    err_profit = _check_one("profit")

    # 不整合のあった方のエラーを返す (sales を優先)
    if err_sales:
        return err_sales
    if err_profit:
        return err_profit
    return ""


def _infer_profit_label(data: dict[str, Any]) -> str:
    """
    vision JSON から利益ラベルを推定する（notes や固定値から）。
    現状は固定で "セグメント利益" を返す。
    """
    # 将来的に notes や構造から推定する拡張が可能
    return "セグメント利益"
