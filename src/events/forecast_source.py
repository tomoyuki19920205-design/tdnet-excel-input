"""Resolve a TDnet disclosure to its exchange-hosted copy by exact document ID."""
from __future__ import annotations

import re


_TDNET_PDF = re.compile(r"https://www\.release\.tdnet\.info/inbs/(1401\d{14})\.pdf\Z")


def exchange_pdf_url(tdnet_url: str, ticker: str) -> str:
    """Return only a same-ID JPX URL; reject ambiguous or non-TDnet inputs."""
    match = _TDNET_PDF.fullmatch(tdnet_url or "")
    if not match or not re.fullmatch(r"\d{3}[\dA-Z]", ticker or ""):
        return ""
    return f"https://www2.jpx.co.jp/disc/{ticker}0/{match.group(1)}.pdf"
