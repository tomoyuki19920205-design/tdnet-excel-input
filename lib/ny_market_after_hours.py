"""Two-stage, ticker-agnostic discovery plans for NY after-hours coverage."""
from __future__ import annotations

from datetime import date
from typing import Any


class NYMarketAfterHoursError(ValueError):
    pass


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NYMarketAfterHoursError(f"{field} must be a non-empty string")
    return value.strip()


def build_after_hours_discovery_plan(
    market_session_date: str,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return broad discovery queries, then uniform per-candidate verification queries."""
    try:
        date.fromisoformat(market_session_date)
    except (TypeError, ValueError) as exc:
        raise NYMarketAfterHoursError("market_session_date must be YYYY-MM-DD") from exc
    broad_queries = [
        f'after market earnings calendar "{market_session_date}" US stocks',
        f'after-hours stock movers "{market_session_date}" earnings results',
        f'site:sec.gov/Archives/edgar/data 8-K "{market_session_date}" results of operations',
        f'after-hours movers "{market_session_date}" clinical trial FDA company announcement',
    ]
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            raise NYMarketAfterHoursError(f"candidates[{index}] must be an object")
        ticker = _text(candidate.get("ticker"), f"candidates[{index}].ticker").upper()
        company = _text(candidate.get("company_name"), f"candidates[{index}].company_name")
        if ticker in seen:
            raise NYMarketAfterHoursError(f"duplicate candidate ticker: {ticker}")
        seen.add(ticker)
        reviews.append({
            "ticker": ticker,
            "company_name": company,
            "verification_queries": [
                f'"{ticker}" "{company}" "{market_session_date}" results investor relations',
                f'site:sec.gov/Archives/edgar/data "{ticker}" "{market_session_date}" 8-K',
                f'"{ticker}" "{company}" after-hours "{market_session_date}"',
            ],
        })
    return {
        "contract_version": "ny_market_after_hours_v1",
        "discovery_method": "broad_discovery_then_primary_verification",
        "market_session_date": market_session_date,
        "broad_discovery_queries": broad_queries,
        "candidate_verification": reviews,
    }
