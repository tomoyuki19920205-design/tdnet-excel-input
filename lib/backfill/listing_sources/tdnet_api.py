"""lib/backfill/listing_sources/tdnet_api.py — 将来の TDnet API provider (stub)

将来の正本 listing source 差し替え用。
現時点では NotImplementedError を投げる。
"""
from __future__ import annotations

from .base import FilingInfo


class TdnetApiListingProvider:
    """将来の TDnet API provider (未実装 stub)。

    TDnet が公式 API を提供した場合、ここに実装を追加する。
    CompositeProvider で先頭に配置すれば自動的に正本 source になる。
    """

    @property
    def name(self) -> str:
        return "tdnet_api"

    def list_filings(
        self,
        start_date: str,
        end_date: str,
        *,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> list[FilingInfo]:
        raise NotImplementedError(
            "TDnet API provider は未実装です。"
            "TdnetHtmlListingProvider を fallback として使用してください。"
        )
