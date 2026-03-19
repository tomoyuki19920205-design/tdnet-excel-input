"""lib/backfill/listing_sources/base.py — FilingInfo + filing_id 生成"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


@dataclass
class FilingInfo:
    """バックフィル対象の filing 情報。

    listing provider が返す共通データモデル。
    backfill 本体は listing source を意識せず処理できる。
    """
    filing_id: str              # deterministic SHA1 (24 chars)
    ticker: str                 # 4桁コード
    title: str                  # 原文タイトル
    disclosure_date: str        # YYYY-MM-DD
    doc_url: str                # PDF URL
    xbrl_url: str | None        # XBRL ZIP URL (あれば)
    doc_type: str               # "financial_statement" / "forecast_revision"
    company_name: str
    published_at: str           # 公開時刻
    listing_source: str         # "tdnet_html" / "tdnet_api" 等
    has_xbrl: bool              # XBRL 有無 (確認済み)
    xbrl_url_inferred: bool = False  # xbrl_url が PDF URL からの推定値か


def normalize_title(title: str) -> str:
    """タイトルを正規化する。

    fetcher.py の同名関数と同一ロジック（依存を避けるため再実装）。
    - NFKC 正規化 (全角→半角)
    - 改行・スペース除去
    - 小文字化
    """
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    s = s.lower()
    return s


def canonicalize_url(url: str) -> str:
    """URL を正規化する。

    - クエリパラメータ除去
    - スキーム差異吸収 (http → https)
    - 末尾スラッシュ除去
    """
    # クエリパラメータ・フラグメント除去
    url = url.split("?")[0].split("#")[0]
    # http → https
    if url.startswith("http://"):
        url = "https://" + url[7:]
    # 末尾スラッシュ除去
    url = url.rstrip("/")
    return url


def make_filing_id(
    disclosure_date: str,
    ticker: str,
    title: str,
    doc_url: str,
) -> str:
    """filing_id を決定的に生成する。

    同一 filing は再 listing しても同一 ID になることを保証する。

    Args:
        disclosure_date: YYYY-MM-DD
        ticker: 4桁コード
        title: 原文タイトル
        doc_url: 文書 URL

    Returns:
        24文字の hex string (SHA1[:24])
    """
    title_norm = normalize_title(title)
    canonical_url = canonicalize_url(doc_url)
    raw = f"{disclosure_date}|{ticker}|{title_norm}|{canonical_url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
