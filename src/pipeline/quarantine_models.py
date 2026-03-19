# ============================================================
# quarantine_models.py — Stage-aware Quarantine モデル
# ============================================================
"""
quarantine を「どの段階で落ちたか」「何が不足だったか」を
構造化して記録する仕組み。

設計思想:
  - 失敗記録だけでなく、改善のヒントも残す
  - failed_stage でパイプラインのどの層で失敗したか特定
  - review_hint で人間が次に何を確認すべきか示唆
  - candidate_score_json で判定根拠を保持
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..analysis.candidate_models import ExtractionStage


# ============================================================
# QuarantineInfo — 拡張 quarantine 情報
# ============================================================

@dataclass
class QuarantineInfo:
    """
    Stage-aware quarantine 情報。

    既存の quarantine_record() に追加情報を渡すための構造体。
    """
    # --- 既存フィールド (後方互換) ---
    company_code: str = ""
    reason: str = ""
    fiscal_year_end: str = ""
    quarter: str = ""
    metric_type: str = ""
    detail: str = ""
    source_doc_id: str = ""

    # --- 拡張フィールド ---
    failed_stage: str = ""              # ExtractionStage の値
    issue_type: str = ""                # "keyword_miss" / "tag_unknown" / "ocr_needed" etc.
    rule_trace: list[str] = field(default_factory=list)
    candidate_score_json: str = ""      # スコア情報のJSON文字列
    review_hint: str = ""               # 人間向け改善ヒント

    def to_quarantine_kwargs(self) -> dict[str, Any]:
        """
        MigrationDB.quarantine_record() に渡す kwargs を生成。
        既存の引数 + 拡張カラムを含む。
        """
        return {
            "company_code": self.company_code,
            "reason": self.reason,
            "fiscal_year_end": self.fiscal_year_end,
            "quarter": self.quarter,
            "metric_type": self.metric_type,
            "detail": self.detail,
            "source_doc_id": self.source_doc_id,
            "failed_stage": self.failed_stage,
            "review_hint": self.review_hint,
        }


# ============================================================
# ヘルパー関数
# ============================================================

# review_hint テンプレート
_HINT_TEMPLATES: dict[str, str] = {
    "no_sales_col": "売上列候補が見つかりません。SALES_COL_KEYWORDS の拡張を確認してください。",
    "no_profit_col": "利益列候補が見つかりません。PROFIT_COL_KEYWORDS の拡張を確認してください。",
    "no_segment_table": "セグメント表が検出されません。セグメント表ヘッダーキーワードを確認してください。",
    "no_rows": "セグメント表は検出されましたが行が抽出できません。数値パターンまたは行フィルタを確認してください。",
    "xbrl_tag_unknown": "XBRL タグが既知マップにありません。業態別プロファイルの追加を検討してください。",
    "xbrl_bank": "銀行業タグ候補が検出されました。XBRL bank profile を確認してください。",
    "xbrl_reit": "REIT/投資法人タグ候補が検出されました。XBRL reit profile を確認してください。",
    "ocr_needed": "テキスト抽出不可（画像PDF）。OCR対象候補として記録しています。",
    "text_empty": "PDFからテキストが抽出できません。ファイル形式を確認してください。",
    "order_no_total": "受注キーワードはありますが合計行が見つかりません。合計行パターンの拡張を検討してください。",
}


def build_quarantine_info(
    *,
    company_code: str,
    reason: str,
    failed_stage: str | ExtractionStage = "",
    issue_type: str = "",
    rule_trace: list[str] | None = None,
    candidate_scores: dict[str, Any] | None = None,
    review_hint_key: str = "",
    review_hint_custom: str = "",
    fiscal_year_end: str = "",
    quarter: str = "",
    metric_type: str = "",
    detail: str = "",
    source_doc_id: str = "",
) -> QuarantineInfo:
    """
    QuarantineInfo を構築するヘルパー。

    Args:
        review_hint_key: _HINT_TEMPLATES のキー
        review_hint_custom: カスタム hint (key より優先)
    """
    stage_str = failed_stage.value if isinstance(failed_stage, ExtractionStage) else failed_stage

    # review_hint 決定
    hint = review_hint_custom
    if not hint and review_hint_key:
        hint = _HINT_TEMPLATES.get(review_hint_key, "")

    # candidate_scores を JSON 化
    score_json = ""
    if candidate_scores:
        try:
            score_json = json.dumps(candidate_scores, ensure_ascii=False)
        except (TypeError, ValueError):
            score_json = str(candidate_scores)

    return QuarantineInfo(
        company_code=company_code,
        reason=reason,
        fiscal_year_end=fiscal_year_end,
        quarter=quarter,
        metric_type=metric_type,
        detail=detail,
        source_doc_id=source_doc_id,
        failed_stage=stage_str,
        issue_type=issue_type,
        rule_trace=rule_trace or [],
        candidate_score_json=score_json,
        review_hint=hint,
    )
