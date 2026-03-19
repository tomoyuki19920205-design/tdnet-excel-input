"""lib/backfill/xbrl_url_inference.py — TDnet XBRL URL 推定共通ヘルパー

TDnet の PDF URL から XBRL ZIP URL を機械的に推定する。
listing / ingest / manifest 再生成で同じロジックを共有する。

TDnet URL パターン:
  PDF:  https://www.release.tdnet.info/inbs/1401{doc_id}.pdf
  XBRL: https://www.release.tdnet.info/inbs/0812{doc_id}.zip

注意:
  - 推定 URL は「存在する可能性がある」だけで、実際に XBRL が用意されているとは限らない
  - 存在確認はダウンロード段階で行う (404 → skip)
"""
from __future__ import annotations

import re

# TDnet PDF URL のプレフィックス (決算短信 = "1401")
_TDNET_PDF_PREFIX = "1401"
# TDnet XBRL ZIP URL のプレフィックス
_TDNET_XBRL_PREFIX = "0812"

_TDNET_INBS_BASE = "https://www.release.tdnet.info/inbs/"

# PDF URL の正規表現 (1401 + 数字列 + .pdf)
_PDF_URL_RE = re.compile(
    r"(?:https?://www\.release\.tdnet\.info/inbs/)?"
    r"(1401)(\d+)\.pdf$",
    re.IGNORECASE,
)


def infer_xbrl_url_from_pdf(doc_url: str) -> str | None:
    """PDF URL から XBRL ZIP URL を推定する。

    Args:
        doc_url: TDnet PDF URL (e.g. "https://...inbs/140120260313581653.pdf")

    Returns:
        推定 XBRL URL (e.g. "https://...inbs/081220260313581653.zip")
        パターン不一致の場合は None
    """
    if not doc_url:
        return None

    m = _PDF_URL_RE.search(doc_url)
    if not m:
        return None

    doc_id = m.group(2)  # "20260313581653"
    return f"{_TDNET_INBS_BASE}{_TDNET_XBRL_PREFIX}{doc_id}.zip"


def has_inferred_xbrl_url(doc_url: str) -> bool:
    """PDF URL から XBRL URL が推定可能かどうかを返す。"""
    return infer_xbrl_url_from_pdf(doc_url) is not None
