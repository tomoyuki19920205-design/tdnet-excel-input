"""lib/backfill/listing_provider.py — ListingProvider Protocol + CompositeProvider"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .listing_sources.base import FilingInfo

logger = logging.getLogger("backfill.listing")


@runtime_checkable
class ListingProvider(Protocol):
    """filing 一覧取得の抽象プロトコル。

    backfill 本体は「どの listing source から来たか」を意識せず処理できる。
    """

    @property
    def name(self) -> str:
        """provider 名 ("tdnet_html", "tdnet_api" 等)"""
        ...

    def list_filings(
        self,
        start_date: str,
        end_date: str,
        *,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> list[FilingInfo]:
        """指定期間の filing 一覧を返す。

        Args:
            start_date: YYYY-MM-DD (inclusive)
            end_date: YYYY-MM-DD (inclusive)
            tickers: 絞り込み対象 ticker (None = 全銘柄)
            doc_types: 絞り込み対象 doc_type (None = 全タイプ)

        Returns:
            FilingInfo のリスト
        """
        ...


class CompositeListingProvider:
    """複数 provider を順に試行し、filing_id で重複排除する。

    Usage::

        provider = CompositeListingProvider([
            TdnetApiProvider(),    # 正本 (将来)
            TdnetHtmlProvider(),   # fallback
        ])
        filings = provider.list_filings("2024-01-01", "2024-12-31")
    """

    def __init__(self, providers: list[ListingProvider]) -> None:
        if not providers:
            raise ValueError("providers は1つ以上必要です")
        self.providers = providers

    @property
    def name(self) -> str:
        return "composite"

    def list_filings(
        self,
        start_date: str,
        end_date: str,
        *,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> list[FilingInfo]:
        """providers を順に試行し、成功した最初の結果を返す。

        各 provider の結果は filing_id で重複排除する。
        """
        seen_ids: set[str] = set()
        all_filings: list[FilingInfo] = []
        errors: list[tuple[str, str]] = []

        for provider in self.providers:
            try:
                logger.info(
                    f"[listing] trying provider={provider.name} "
                    f"range={start_date}~{end_date}"
                )
                filings = provider.list_filings(
                    start_date, end_date,
                    tickers=tickers, doc_types=doc_types,
                )
                # filing_id で dedup
                new_count = 0
                for f in filings:
                    if f.filing_id not in seen_ids:
                        seen_ids.add(f.filing_id)
                        all_filings.append(f)
                        new_count += 1

                logger.info(
                    f"[listing] provider={provider.name}: "
                    f"total={len(filings)} new={new_count} "
                    f"dedup={len(filings) - new_count}"
                )

                # 最初に成功した provider の結果があればそれで十分
                if filings:
                    break

            except Exception as e:
                logger.warning(
                    f"[listing] provider={provider.name} failed: {e}"
                )
                errors.append((provider.name, str(e)))
                continue

        if not all_filings and errors:
            logger.error(
                f"[listing] all providers failed: "
                f"{[(name, err[:100]) for name, err in errors]}"
            )

        return all_filings
