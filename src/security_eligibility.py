"""Security-first eligibility rules for the corporate TDnet pipeline.

ETF/ETN eligibility is decided from official J-Quants/TDnet attributes before
document-title classification.  Name/title matching remains only as a fallback
when no authoritative classification is available (for legacy HTML/API rows).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .common_ticker import strip_tdnet_trailing_zero

logger = logging.getLogger("tdnet.security_eligibility")

# J-Quants V2 /equities/master ProdCat values observed in the authoritative
# master.  014 covers domestic ETFs.  023 covers listed investment products
# such as foreign ETFs, commodity trusts and ETNs.  013 is an individual REIT
# and is intentionally not excluded.
ETF_LIKE_PRODUCT_CATEGORIES = frozenset({"014", "023"})

# JPX TDnet public-item classifications: 36=ETF, 37=ETN.  DiscItems values
# contain the two-digit classification followed by the individual item code.
ETF_LIKE_TDNET_PUBLIC_ITEM_PREFIXES = frozenset({"36", "37"})

_LEGACY_INSTRUMENT_KEYWORDS = (
    "etf", "etn", "投資信託", "ファンド", "ishares", "iシェアーズ",
    "アイシェアーズ", "上場投信", "インデックス",
)


@dataclass(frozen=True)
class SecurityEligibilityDecision:
    is_etf_like: bool
    authoritative: bool
    source: str
    product_category: str = ""
    matched_public_item: str = ""
    master_date: str = ""


def _normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).lower()


def _normalize_date(value: object) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def _default_master_db_path() -> Path:
    configured = os.environ.get("JQUANTS_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data" / "jquants.db"


@lru_cache(maxsize=16)
def _load_master_snapshot(
    db_path_text: str,
    as_of_date: str,
) -> tuple[str, dict[str, str]]:
    """Load the latest authoritative snapshot on/before ``as_of_date``."""
    db_path = Path(db_path_text)
    if not db_path.is_file():
        return "", {}

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        try:
            if as_of_date:
                row = conn.execute(
                    "SELECT MAX(date) FROM market_data_universe WHERE date<=?",
                    (as_of_date,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(date) FROM market_data_universe"
                ).fetchone()
            snapshot_date = str(row[0] or "") if row else ""
            if not snapshot_date:
                return "", {}
            rows = conn.execute(
                "SELECT ticker, product_category FROM market_data_universe "
                "WHERE date=?",
                (snapshot_date,),
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning(
            "[SECURITY_ELIGIBILITY] master_unavailable path=%s error=%s",
            db_path,
            type(exc).__name__,
        )
        return "", {}

    categories = {
        strip_tdnet_trailing_zero(str(ticker or "").strip().upper()):
        str(product_category or "").strip()
        for ticker, product_category in rows
        if ticker
    }
    return snapshot_date, categories


def lookup_product_category(
    ticker: object,
    *,
    as_of_date: object = "",
    master_db_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Return ``(ProdCat, snapshot_date)`` for numeric or alpha tickers."""
    normalized_ticker = strip_tdnet_trailing_zero(
        str(ticker or "").strip().upper()
    )
    if not normalized_ticker:
        return "", ""
    db_path = Path(master_db_path) if master_db_path else _default_master_db_path()
    snapshot_date, categories = _load_master_snapshot(
        str(db_path.resolve()), _normalize_date(as_of_date)
    )
    return categories.get(normalized_ticker, ""), snapshot_date


def classify_security_eligibility(
    ticker: object,
    *,
    product_category: object = "",
    tdnet_public_items: Iterable[object] = (),
    as_of_date: object = "",
    master_db_path: str | os.PathLike[str] | None = None,
    title: object = "",
    company_name: object = "",
) -> SecurityEligibilityDecision:
    """Classify an instrument without relying on ticker-number ranges."""
    for raw_item in tdnet_public_items or ():
        item = str(raw_item or "").strip()
        if item[:2] in ETF_LIKE_TDNET_PUBLIC_ITEM_PREFIXES:
            return SecurityEligibilityDecision(
                True, True, "tdnet_public_item", matched_public_item=item
            )

    category = str(product_category or "").strip()
    master_date = ""
    source = "item_product_category"
    if not category:
        category, master_date = lookup_product_category(
            ticker, as_of_date=as_of_date, master_db_path=master_db_path
        )
        source = "jquants_equities_master"

    if category:
        return SecurityEligibilityDecision(
            category in ETF_LIKE_PRODUCT_CATEGORIES,
            True,
            source,
            product_category=category,
            master_date=master_date,
        )

    combined = f"{_normalize_text(title)} {_normalize_text(company_name)}"
    fallback_match = any(keyword in combined for keyword in _LEGACY_INSTRUMENT_KEYWORDS)
    return SecurityEligibilityDecision(
        fallback_match,
        False,
        "legacy_text_fallback" if fallback_match else "unclassified",
    )


def classify_disclosure_security(
    item: object,
    *,
    as_of_date: object = "",
    master_db_path: str | os.PathLike[str] | None = None,
) -> SecurityEligibilityDecision:
    """Resolve security eligibility from a DisclosureItem-like object."""
    effective_date = as_of_date or getattr(item, "published_at", "")
    return classify_security_eligibility(
        getattr(item, "ticker", ""),
        product_category=getattr(item, "product_category", ""),
        tdnet_public_items=getattr(item, "tdnet_public_items", ()),
        as_of_date=effective_date,
        master_db_path=master_db_path,
        title=getattr(item, "title", ""),
        company_name=getattr(item, "company_name", ""),
    )


def is_etf_like_security(
    ticker: object,
    **kwargs: object,
) -> bool:
    """Boolean convenience wrapper; the classifier remains the source of truth."""
    return classify_security_eligibility(ticker, **kwargs).is_etf_like
