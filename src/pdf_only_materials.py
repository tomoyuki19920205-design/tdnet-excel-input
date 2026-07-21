"""High-precision title classification for metadata-only TDNET PDF alerts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote


EARNINGS_MATERIAL = "earnings_material"
MONTHLY_UPDATE = "monthly_update"
JST = timezone(timedelta(hours=9))
# Rollout boundary: do not turn previously filtered same-day disclosures into a backfill.
PDF_ONLY_MATERIAL_ALERTS_ACTIVATED_AT = datetime(2026, 7, 21, 16, 59, 1, tzinfo=JST)


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


def is_after_pdf_only_material_activation(disclosed_at: str) -> bool:
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
    return parsed >= PDF_ONLY_MATERIAL_ALERTS_ACTIVATED_AT


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
    "質疑応答", "q&a", "qa要旨", "書き起こし", "文字おこし", "transcript",
    "スクリプト", "株主総会", "株主向け", "訂正", "修正について",
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

    if any(term in n for term in _MONTHLY_TERMS):
        if not any(term in n for term in _MONTHLY_EXCLUDES):
            return PdfOnlyMaterialMatch(MONTHLY_UPDATE, _extract_month(title))

    return None
