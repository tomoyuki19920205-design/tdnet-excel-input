"""Shared document classifier for the production Company IR Nightly contract.

The title allowlist and exclusions here are the single source of truth used by
both the Nightly detector and Company News overlap exclusion.  Keep this
classifier high precision: appearing on an IR page is never enough by itself.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


ASSET_MATERIAL = "earnings_material"
ASSET_VIDEO = "earnings_video"

MATERIAL_TERMS = (
    "決算説明資料", "決算説明会資料", "決算補足説明資料", "決算説明補足資料",
    "決算補足資料", "決算説明会プレゼンテーション",
    "financialresultspresentation", "earningspresentation",
    "resultsbriefingmaterials", "presentationmaterialsforfinancialresults",
)
VIDEO_TERMS = (
    "決算説明動画", "決算説明会動画", "決算説明会webcast", "決算説明会ライブ配信",
    "financialresultsbriefingvideo", "earningswebcast", "resultspresentationvideo",
)
EXCLUDED_TERMS = (
    "決算短信", "有価証券報告書", "四半期報告書", "適時開示", "株主総会", "招集通知",
    "統合報告書", "q&a", "qa要旨", "質疑応答", "書き起こし", "文字おこし",
    "transcript", "中期経営計画", "事業説明会", "個人投資家", "irセミナー",
    "会社説明会", "訂正", "修正について",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).lower()


def is_pdf_url(url: str) -> bool:
    path = urlsplit(url or "").path
    return bool(re.search(r"\.pdf$", path, re.IGNORECASE))


def classify_ir_nightly_asset(title: str, url: str, context: str = "") -> str | None:
    """Return exactly the asset types currently notified by IR Nightly.

    ``context`` remains accepted for API compatibility but deliberately cannot
    turn a sibling or an IR-channel label into a positive classification.
    """
    title_n = normalize_text(title)
    if not title_n or any(term in title_n for term in EXCLUDED_TERMS):
        return None
    if any(term in title_n for term in VIDEO_TERMS):
        return ASSET_VIDEO
    if any(term in title_n for term in MATERIAL_TERMS) and is_pdf_url(url):
        return ASSET_MATERIAL
    return None


def is_ir_nightly_covered_document(title: str, url: str) -> bool:
    return classify_ir_nightly_asset(title, url) is not None
