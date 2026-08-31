"""High-precision title classification for metadata-only TDNET PDF alerts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote


EARNINGS_MATERIAL = "earnings_material"
MONTHLY_UPDATE = "monthly_update"
MANAGEMENT_STRATEGY = "management_strategy"
JST = timezone(timedelta(hours=9))
# Rollout boundary: do not turn previously filtered same-day disclosures into a backfill.
PDF_ONLY_MATERIAL_ALERTS_ACTIVATED_AT = datetime(2026, 7, 21, 16, 59, 1, tzinfo=JST)
MANAGEMENT_STRATEGY_ALERTS_ACTIVATED_AT = datetime(2026, 8, 21, 19, 53, 13, tzinfo=JST)


@dataclass(frozen=True)
class PdfOnlyMaterialMatch:
    event_type: str
    short_label: str


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def is_pdf_url(url: str) -> bool:
    """Accept official direct/redirect URLs whose decoded target is a PDF."""
    value = unquote((url or "").strip()).lower()
    return value.startswith(("https://", "http://")) and bool(
        re.search(r"\.pdf(?:$|[?#&])", value)
    )


def is_after_pdf_only_material_activation(disclosed_at: str, event_type: str = "") -> bool:
    """Return true only for disclosures published at/after the production rollout."""
    value = (disclosed_at or "").strip()
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    boundary = (
        MANAGEMENT_STRATEGY_ALERTS_ACTIVATED_AT
        if event_type == MANAGEMENT_STRATEGY
        else PDF_ONLY_MATERIAL_ALERTS_ACTIVATED_AT
    )
    return parsed >= boundary


_EARNINGS_TERMS = (
    "決算説明資料",
    "決算説明会資料",
    "決算説明会プレゼンテーション資料",
    "決算補足説明資料",
    "決算説明補足資料",
    "決算補足資料",
    "決算参考資料",
    "決算説明会<参考資料>",
    "financialresultspresentation",
    "resultspresentation",
    "presentationmaterialforfinancialresults",
)

_EARNINGS_EXCLUDES = (
    "決算短信", "決算報告", "開催のお知らせ", "開催いたしました", "開催中止",
    "公開のお知らせ", "株主総会", "株主向け", "訂正", "修正について",
)

# These are intentionally compound/high-signal rules.  Generic words such as
# ``資料`` / ``説明`` / ``お知らせ`` must never classify a disclosure by
# themselves.  NFKC + whitespace removal in ``_normalize`` absorbs full-width
# ASCII, bracket and spacing variants without weakening the semantic match.
_EARNINGS_BRIEFING_CONTEXTS = (
    "決算説明会", "決算説明", "決算補足説明", "業績説明会",
    "financialresultsbriefing", "earningscall", "earningspresentation",
)

_EARNINGS_BRIEFING_CONTENT_TERMS = (
    "エグゼクティブサマリー", "サマリー", "要約",
    "書き起こし", "文字起こし", "文字おこし", "transcript", "スクリプト",
    "質疑応答", "q&a", "qa要旨", "qa概要",
)

_FAQ_TERMS = (
    "よくある質問と回答", "よくあるご質問と回答", "よくあるご質問",
    "よくいただく質問", "frequentlyaskedquestions",
)

_MONTHLY_EXCLUDES = (
    "月次レポート", "月次報告", "月報", "基準価額", "nav", "投資信託",
    "ファンド", "etf", "reit", "リート", "上場投信", "運用報告",
    "開示方針", "開示終了", "公表終了", "掲載終了", "irカレンダー", "訂正",
)

_MONTHLY_TERMS = (
    "月次売上高", "月次売上", "月次業績", "月次実績", "月次概況", "月次kpi",
    "月次動向", "月次速報", "月次連結売上", "月次連結営業収益", "月次情報",
    "月次販売高", "月次料飲売上", "月次営業レポート", "月次ハイライト",
    "月度売上", "月度実績", "月度月次", "売上高前年比速報", "売上高速報",
    "既存店売上高", "monthlysales", "monthlykpi", "monthlybusinessupdate",
    "月次前年比速報", "月次データ", "月次仕入", "月次開示", "月次に関する",
)

# High-confidence management strategy titles. Matching is performed against the
# NFKC/space/case-normalized title while the original title remains untouched.
MANAGEMENT_STRATEGY_STRONG_TERMS = (
    "中期経営計画",
    "中期事業計画",
    "中長期経営計画",
    "中長期事業計画",
    "中期経営方針",
    "中長期経営方針",
    "経営計画",
    "成長可能性",
    "資本コスト",
    "株価を意識",
    "pbr改善",
    "pbrの改善",
    "pbr向上",
    "pbrの向上",
)

_GROWTH_STRATEGY_CONTEXTS = (
    "説明資料", "成長戦略資料", "説明会", "に関する見解", "の推進状況", "について",
    "事業計画", "経営計画",
)
_GROWTH_STRATEGY_EXCLUDES = (
    "ファンド", "統合報告書", "登壇", "増資", "業務提携", "資本提携",
)

_MANAGEMENT_STRATEGY_CONTEXTS = (
    "説明資料", "説明会", "中期", "中長期", "長期", "進捗", "策定", "公表",
)
_MANAGEMENT_STRATEGY_EXCLUDES = (
    "登壇", "人材戦略", "大学経営戦略", "セミナー",
)

_CORPORATE_VALUE_CONTEXTS = (
    "取組", "取り組み", "対応", "方針", "計画", "戦略", "ロードマップ",
)
_CORPORATE_VALUE_EXCLUDES = (
    "提携", "報酬", "委員会", "セミナー", "パートナーシップ", "事業譲受", "増資",
)

_LONG_TERM_VISION_CONTEXTS = (
    "策定", "公表", "説明", "進捗", "見直し", "更新",
)


def _is_management_strategy_title(normalized_title: str) -> bool:
    if any(term in normalized_title for term in MANAGEMENT_STRATEGY_STRONG_TERMS):
        return True

    if "成長戦略" in normalized_title:
        if not any(term in normalized_title for term in _GROWTH_STRATEGY_EXCLUDES):
            if any(term in normalized_title for term in _GROWTH_STRATEGY_CONTEXTS):
                return True

    if "経営戦略" in normalized_title:
        if not any(term in normalized_title for term in _MANAGEMENT_STRATEGY_EXCLUDES):
            if any(term in normalized_title for term in _MANAGEMENT_STRATEGY_CONTEXTS):
                return True

    if "企業価値向上" in normalized_title:
        if not any(term in normalized_title for term in _CORPORATE_VALUE_EXCLUDES):
            if any(term in normalized_title for term in _CORPORATE_VALUE_CONTEXTS):
                return True

    if any(term in normalized_title for term in ("長期ビジョン", "経営ビジョン")):
        if any(term in normalized_title for term in _LONG_TERM_VISION_CONTEXTS):
            return True

    return False


def _extract_quarter(title: str) -> str:
    n = _normalize(title)
    quarter_patterns = (
        ("1Q", (r"第?1四半期", r"第?一四半期", r"1q", r"firstquarter")),
        ("2Q", (r"第?2四半期", r"第?二四半期", r"中間期?", r"2q", r"secondquarter", r"interim")),
        ("3Q", (r"第?3四半期", r"第?三四半期", r"3q", r"thirdquarter")),
        ("4Q", (r"第?4四半期", r"第?四四半期", r"4q", r"fourthquarter")),
    )
    for label, patterns in quarter_patterns:
        if any(re.search(pattern, n, re.IGNORECASE) for pattern in patterns):
            return label
    if any(term in n for term in ("通期", "本決算", "年度決算", "fullyear", "yearend")):
        return "FY"
    if re.search(r"(?:^|[^a-z])fy\s*\d{2,4}", unicodedata.normalize("NFKC", title), re.IGNORECASE):
        return "FY"
    return ""


def _extract_month(title: str) -> str:
    n = _normalize(title)
    # Explicit business-month expressions take precedence over every fiscal-year expression.
    patterns = (
        r"(?<!\d)(1[0-2]|0?[1-9])月度",
        r"(?<!\d)(1[0-2]|0?[1-9])月分",
        r"(?<!\d)(1[0-2]|0?[1-9])月月次",
        r"月次[^()（）]{0,24}[（(]?(1[0-2]|0?[1-9])月(?!期)(?:度)?[)）]?",
        r"(?<!\d)(1[0-2]|0?[1-9])月(?!期)(?:の)?月次",
        r"月次[^0-9]{0,12}(1[0-2]|0?[1-9])月(?!期)",
    )
    for pattern in patterns:
        match = re.search(pattern, n)
        if match:
            return f"{int(match.group(1))}月月次"
    return "月次"


def classify_pdf_only_material(title: str, pdf_url: str | None = None) -> PdfOnlyMaterialMatch | None:
    """Classify only clear target titles; if a URL is supplied it must identify a PDF."""
    if pdf_url is not None and not is_pdf_url(pdf_url):
        return None
    n = _normalize(title)
    if not n:
        return None

    if any(term in n for term in _EARNINGS_TERMS):
        if not any(term in n for term in _EARNINGS_EXCLUDES):
            quarter = _extract_quarter(title)
            label = f"{quarter}決算説明資料" if quarter else "決算説明資料"
            return PdfOnlyMaterialMatch(EARNINGS_MATERIAL, label)

    if (
        any(context in n for context in _EARNINGS_BRIEFING_CONTEXTS)
        and any(term in n for term in _EARNINGS_BRIEFING_CONTENT_TERMS)
        and not any(term in n for term in _EARNINGS_EXCLUDES)
    ):
        quarter = _extract_quarter(title)
        if any(term in n for term in ("質疑応答", "q&a", "qa要旨", "qa概要")):
            base_label = "決算説明会 Q&A"
        elif any(term in n for term in ("書き起こし", "文字起こし", "文字おこし", "transcript", "スクリプト")):
            base_label = "決算説明会 書き起こし"
        else:
            base_label = "決算説明会 要約"
        label = f"{quarter}{base_label}" if quarter else base_label
        return PdfOnlyMaterialMatch(EARNINGS_MATERIAL, label)

    if any(term in n for term in _FAQ_TERMS) or re.search(r"(?:^|[^a-z])faq(?:[^a-z]|$)", n):
        return PdfOnlyMaterialMatch(EARNINGS_MATERIAL, "IR FAQ")

    # Earnings material retains precedence for a title matching both groups,
    # so one disclosure creates one event/card and keeps existing behavior.
    if _is_management_strategy_title(n):
        return PdfOnlyMaterialMatch(MANAGEMENT_STRATEGY, "中期経営・戦略")

    if any(term in n for term in _MONTHLY_TERMS):
        if not any(term in n for term in _MONTHLY_EXCLUDES):
            return PdfOnlyMaterialMatch(MONTHLY_UPDATE, _extract_month(title))

    return None
