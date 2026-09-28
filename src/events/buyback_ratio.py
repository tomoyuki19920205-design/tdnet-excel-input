"""Sourced, scope-specific share ratios for buyback notices.

Never substitute a market-cap ratio or a previous programme's percentage.
The small evidence registry is limited to an exact disclosure URL; an unknown
issuer or changed cumulative count leaves the ratio empty.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .buyback_extractor import normalize_jp_date, normalize_share_count

_BASES = Path(__file__).resolve().parents[2] / "config" / "buyback_share_bases.json"
_EX_TREASURY = re.compile(
    r"発行済株式総数\s*[（(]\s*自己株式を除く\s*[）)]"
    r"[^\n]{0,70}?([\d,，]+)\s*株"
)


def resolve_denominator(
    text: str, *, ticker: str, source_url: str, disclosure_date: str,
    cumulative_acquired: Optional[int] = None,
) -> Optional[dict]:
    """Return auditable ex-treasury shares only when evidence is unambiguous."""
    date = disclosure_date[:10]
    for record in json.loads(_BASES.read_text(encoding="utf-8")):
        if record["ticker"] != ticker or record["applies_to_source_url"] != source_url:
            continue
        if date != record["adjustment_as_of"] or cumulative_acquired != record["cumulative_acquired_shares"]:
            return None
        denominator = record["base_ex_treasury_shares"] - cumulative_acquired
        if denominator <= 0:
            return None
        return {
            "shares": denominator,
            "as_of": date,
            "base_as_of": record["base_as_of"],
            "source_url": record["base_source_url"],
            "source_title": record["base_source_title"],
            "adjustment_shares": cumulative_acquired,
            "adjustment_source_url": record["adjustment_source_url"],
        }

    # A count stated in the same company disclosure is usable when no separate
    # cumulative acquisition needs to be applied.  Ratios alone are not counts.
    if cumulative_acquired is not None:
        return None
    match = _EX_TREASURY.search(text)
    if not match:
        return None
    shares = normalize_share_count(match.group(1) + "株")
    if not shares or shares <= 0:
        return None
    context = text[max(0, match.start() - 120):match.start()]
    dates = re.findall(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", context)
    as_of = normalize_jp_date(dates[-1]) if dates else date
    if not as_of or as_of > date:
        return None
    return {
        "shares": shares,
        "as_of": as_of,
        "base_as_of": as_of,
        "source_url": source_url,
        "source_title": "当該自己株式取得開示",
        "adjustment_shares": None,
        "adjustment_source_url": None,
    }
