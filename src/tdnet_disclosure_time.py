"""Resolve a TDNET filing's official publication time from its listing HTML."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")


def is_date_only(value: str | None) -> bool:
    """Return whether *value* is exactly a calendar date without a time."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()))


def canonicalize_tdnet_url(value: str | None) -> str:
    """Compare TDNET document URLs by stable path, ignoring query strings."""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split("?", 1)[0].rstrip("/")


def official_listing_url(date_iso: str, page: int) -> str:
    date_compact = date_iso.replace("-", "")
    if not re.fullmatch(r"\d{8}", date_compact):
        raise ValueError(f"invalid TDNET date: {date_iso!r}")
    return f"https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_compact}.html"


def resolve_official_disclosure_datetime(
    date_iso: str,
    source_url: str,
    *,
    max_pages: int = 20,
    get: Callable[..., requests.Response] = requests.get,
) -> str | None:
    """Return the official JST timestamp for a TDNET URL on a listing date.

    The TDNET listing is the authority for a disclosure's publication time.  A
    missing match deliberately returns ``None``; callers must never invent a
    midnight time from a date-only value.
    """
    target_url = canonicalize_tdnet_url(source_url)
    if not target_url:
        return None
    return fetch_official_listing_times(date_iso, max_pages=max_pages, get=get).get(target_url)


def fetch_official_listing_times(
    date_iso: str,
    *,
    max_pages: int = 20,
    get: Callable[..., requests.Response] = requests.get,
) -> dict[str, str]:
    """Return every PDF URL and official JST timestamp from a TDNET listing day."""
    listing_times: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        response = get(official_listing_url(date_iso, page), timeout=20)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("table tr")
        if not rows:
            break
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            published_time = cells[0].get_text(strip=True)
            if not _TIME_RE.fullmatch(published_time):
                continue
            parsed = datetime.strptime(
                f"{date_iso} {published_time}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=JST).isoformat(timespec="seconds")
            for anchor in row.find_all("a", href=True):
                found_url = canonicalize_tdnet_url(
                    urljoin(response.url or official_listing_url(date_iso, page), anchor["href"])
                )
                if found_url.lower().endswith(".pdf"):
                    listing_times[found_url] = parsed
    return listing_times
