#!/usr/bin/env python3
"""buyback_classifier.py — 自社株買い関連文書の分類器

タイトルと本文冒頭から buyback 系開示かを判定し、
event_type を推定する。
"""
from __future__ import annotations

import re
from .buyback_models import (
    ClassificationResult,
    BUYBACK_DECISION,
    BUYBACK_STATUS,
    BUYBACK_RESULT,
    TREASURY_CANCEL,
)

# ============================================================
# buyback_event_subtype 判定
# 値: "new_program" | "tostnet" | "ignore"
# ============================================================

# ToSTNeT系パターン（タイトルに含まれる場合は tostnet）
_TOSTNET_PATTERNS = [
    "ToSTNeT",
    "tostnet",
    "立会外買付取引",
]

# new_program 確定パターン（取得枠決議系）
_NEW_PROGRAM_PATTERNS = [
    "取得に係る事項の決定",
    "取得に係る事項",
    "取得枠設定",
    "取得枠の設定",
    "自己株式取得に係る事項",
    "自己株式の取得及び自己株式の消却",
    "自己株式取得に係る事項の決定及び自己株式の消却",
    "自己株式の取得の決定",
]

# 強除外パターン（タイトルにあれば ignore）
# ※ ToSTNeT が含まれる場合は除外しない（tostnet 判定優先）
_IGNORE_TITLE_PATTERNS = [
    "取得状況",
    "取得結果",
    "取得終了",
    "消却完了",
    "自己株式の処分",
    "自己株式処分",
    "譲渡制限付株式",
    "ストックオプション",
    "新株予約権",
    "役職員向け株式報酬",
    "持株会",
    "決算短信",
    "決算説明資料",
    "補足資料",
    "四半期報告",
]


def classify_buyback_subtype(title: str) -> str:
    """タイトルから自社株買い通知サブタイプを判定する。

    Parameters
    ----------
    title : str
        開示タイトル

    Returns
    -------
    str
        "new_program" | "tostnet" | "ignore"
    """
    if not title:
        return "ignore"

    # 1. ToSTNeT 判定（最優先 — 取得状況/結果/終了でも ToSTNeT なら通知対象）
    for pat in _TOSTNET_PATTERNS:
        if pat.lower() in title.lower():
            return "tostnet"

    # 2. 強除外パターン（ToSTNeT ではない場合）
    for pat in _IGNORE_TITLE_PATTERNS:
        if pat in title:
            return "ignore"

    # 3. new_program 判定
    for pat in _NEW_PROGRAM_PATTERNS:
        if pat in title:
            return "new_program"

    # 4. 「自己株式取得」を含む汎用パターン（取得方針決定等）
    #    ただし「取得状況/結果/終了」は既に除外済み
    if "自己株式取得" in title or "自己株式の取得" in title:
        return "new_program"

    # 5. 判定できない場合は ignore
    return "ignore"

# ============================================================
# 強除外パターン（これが含まれると buyback ではない）
# ============================================================
_STRONG_EXCLUDE = [
    "ストックオプション",
    "新株予約権",
    "譲渡制限付株式",
    "第三者割当",
    "持株会",
]

# 条件付き除外（タイトルのみにこれがあり、取得系キーワードがない場合は除外）
_CONDITIONAL_EXCLUDE = [
    "自己株式処分",
    "自己株式の処分",
    "自己株式移転",
    "自己株式の移転",
]

# ============================================================
# treasury_cancel 誤検出抑制パターン
# ============================================================
_CANCEL_SUPPRESS = [
    "社債",
    "転換社債",
    "新株予約権付社債",
    "買入消却",
    "消却見合わせ",
]

# ============================================================
# event_type 判定パターン（優先順位順）
# ============================================================
# 優先順位: 消却 > 結果/終了 > 状況 > 決定
_EVENT_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    (TREASURY_CANCEL, [
        "消却",
    ]),
    (BUYBACK_RESULT, [
        "取得結果",
        "取得終了",
    ]),
    (BUYBACK_STATUS, [
        "取得状況",
    ]),
    (BUYBACK_DECISION, [
        "取得に係る事項の決定",
        "取得に係る事項",
        "取得枠",
        "自己株式取得",
        "自己株式の取得",
    ]),
]

# ============================================================
# 強キーワード（buyback 関連の判定用）
# ============================================================
_STRONG_KEYWORDS = [
    "自己株式取得",
    "自己株式の取得",
    "取得に係る事項",
    "取得状況",
    "取得結果",
    "取得終了",
    "自己株式消却",
    "自己株式の消却",
    "share buyback",
    "share repurchase",
    "treasury stock",
]

# ============================================================
# 補助キーワード
# ============================================================
_SUPPORT_KEYWORDS = [
    "取得株式数",
    "取得価額の総額",
    "取得期間",
    "取得方法",
    "発行済株式総数",
    "市場買付",
    "立会外取引",
    "ToSTNeT",
    "消却予定日",
    "消却株式数",
]

# ============================================================
# 本文構造 decision 判定用キーワード
# ============================================================
_DECISION_STRUCTURE_KEYWORDS = [
    "取得する株式の総数",
    "取得し得る株式の総数",
    "取得株式の種類及び数",
    "取得する株式の数",
]
_DECISION_AMOUNT_KEYWORDS = [
    "取得価額の総額",
    "取得に要する資金",
    "取得総額",
]
_DECISION_PERIOD_KEYWORDS = [
    "取得期間",
    "取得の期間",
]


# ============================================================
# メイン分類関数
# ============================================================
def classify_buyback(title: str, body_head: str = "") -> ClassificationResult:
    """タイトルと本文冒頭から buyback 系開示かを判定する。

    Parameters
    ----------
    title : str
        開示タイトル
    body_head : str
        本文冒頭テキスト（1000文字程度推奨）

    Returns
    -------
    ClassificationResult
    """
    title = title.strip()
    body_head = body_head.strip()
    combined = title + " " + body_head[:2000]

    matched_keywords: list[str] = []
    confidence = 0.0

    # 1. 強除外チェック（タイトルのみ）
    for excl in _STRONG_EXCLUDE:
        if excl in title:
            return ClassificationResult(
                is_buyback_related=False,
                event_type_candidate="",
                confidence=0.0,
                matched_keywords=[f"EXCLUDED:{excl}"],
            )

    # 2. 条件付き除外チェック
    for cond_excl in _CONDITIONAL_EXCLUDE:
        if cond_excl in title:
            # タイトルに「処分」があっても「取得」が同時にあれば除外しない
            has_acquire = any(kw in title for kw in ["取得", "buyback", "repurchase"])
            if not has_acquire:
                return ClassificationResult(
                    is_buyback_related=False,
                    event_type_candidate="",
                    confidence=0.0,
                    matched_keywords=[f"COND_EXCLUDED:{cond_excl}"],
                )

    # 3. 強キーワードマッチ
    for kw in _STRONG_KEYWORDS:
        if kw.lower() in combined.lower():
            matched_keywords.append(kw)

    # 4. 補助キーワードマッチ
    for kw in _SUPPORT_KEYWORDS:
        if kw.lower() in combined.lower():
            matched_keywords.append(kw)

    # 5. event_type 推定（タイトル優先）
    event_type = _detect_event_type(title)

    # タイトルで確定しなかった場合は本文で試行（拡大範囲）
    if not event_type and body_head:
        event_type = _detect_event_type(body_head[:2000])

    # 5b. 本文構造から decision を推定
    if not event_type and body_head:
        event_type = _detect_decision_from_structure(body_head[:3000])
        if event_type:
            confidence += 0.10  # 構造推定ボーナス

    # 6. treasury_cancel 誤検出抑制
    if event_type == TREASURY_CANCEL:
        for suppress in _CANCEL_SUPPRESS:
            if suppress in combined:
                # 社債関連の消却は treasury_cancel ではない
                event_type = ""
                matched_keywords.append(f"CANCEL_SUPPRESSED:{suppress}")
                break

    # 7. confidence 算出
    if event_type:
        confidence += 0.40  # タイトル or 本文から event_type 確定

    strong_count = sum(1 for kw in _STRONG_KEYWORDS if kw.lower() in combined.lower())
    if strong_count > 0:
        confidence += 0.20

    support_count = sum(1 for kw in _SUPPORT_KEYWORDS if kw.lower() in combined.lower())
    if support_count >= 2:
        confidence += 0.10
    elif support_count >= 1:
        confidence += 0.05

    confidence = min(confidence, 1.0)

    is_related = confidence >= 0.20 and len(matched_keywords) > 0

    # treasury_cancel が抑制された場合は buyback_related を false にする
    if not event_type and is_related:
        # キーワードが CANCEL_SUPPRESSED だけの場合は除外
        real_keywords = [kw for kw in matched_keywords
                         if not kw.startswith("CANCEL_SUPPRESSED:")]
        if not real_keywords:
            is_related = False

    return ClassificationResult(
        is_buyback_related=is_related,
        event_type_candidate=event_type or "",
        confidence=round(confidence, 2),
        matched_keywords=matched_keywords,
    )


def _detect_event_type(text: str) -> str:
    """テキストから event_type を推定する（優先順位順）"""
    for etype, patterns in _EVENT_TYPE_PATTERNS:
        for pat in patterns:
            if pat in text:
                # 「消却」の場合、「自己株式」も近くにあることを確認
                if etype == TREASURY_CANCEL:
                    if "自己株式" in text or "treasury" in text.lower():
                        # さらに社債系の消却でないことを確認
                        for suppress in _CANCEL_SUPPRESS:
                            if suppress in text:
                                continue  # この pattern をスキップ
                        return etype
                    # 「消却」単体では弱い → skip
                    continue
                return etype
    return ""


def _detect_decision_from_structure(text: str) -> str:
    """本文構造から buyback_decision を推定する。

    株数 + 金額 + 期間 の3点が近接して出る場合は decision を強く推定。
    """
    has_shares = any(kw in text for kw in _DECISION_STRUCTURE_KEYWORDS)
    has_amount = any(kw in text for kw in _DECISION_AMOUNT_KEYWORDS)
    has_period = any(kw in text for kw in _DECISION_PERIOD_KEYWORDS)

    if has_shares and has_amount and has_period:
        return BUYBACK_DECISION
    if has_shares and has_amount:
        return BUYBACK_DECISION
    return ""
