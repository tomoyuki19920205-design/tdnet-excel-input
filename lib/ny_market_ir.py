"""Read a Q4-hosted issuer's public investor-news feed with source evidence."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


Fetch = Callable[[str], tuple[bytes, str]]
_FEED_PATH = "/feed/PressRelease.svc/GetPressReleaseList"
_CATEGORY_RE = re.compile(r"\bcategory\s*:\s*['\"]([0-9a-fA-F-]{36})['\"]")
_SCRIPT_RE = re.compile(r"<script[^>]+src=['\"]([^'\"]*evergreen\.q4Api[^'\"]*)['\"]", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_STOCK_TICKERS_RE = re.compile(r"\btickerList\s*:\s*(\[[^\]]{1,3000}\])", re.I)


class IRFeedError(ValueError):
    """An official feed could not be identified or verified."""


class _PageIdentity(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible: list[str] = []
        self.links: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._hidden += 1
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.visible.append(data)


def _identity_evidence(page: str, title: str, issuer_name: str, ticker: str) -> str:
    if not issuer_name.strip() or not ticker.strip():
        raise IRFeedError("issuer name and ticker are required for official page identity")
    if issuer_name.casefold() in title.casefold() and ticker.casefold() in title.casefold():
        return "page_title_name_and_ticker"
    parsed = _PageIdentity()
    parsed.feed(page)
    visible = " ".join(parsed.visible)
    if issuer_name.casefold() not in visible.casefold():
        raise IRFeedError(f"issuer identity not found in official page title or visible text: {title!r}")
    for link in parsed.links:
        query = {key.casefold(): values for key, values in parse_qs(urlsplit(link).query).items()}
        if any(value.casefold() == ticker.casefold() for value in query.get("ticker", [])) and any(
            value.casefold() in ("nasdaq", "nyse", "amex") for value in query.get("exchange", [])
        ):
            return "visible_corporate_name_and_stock_link"
    for match in _STOCK_TICKERS_RE.finditer(page):
        script_start = page.rfind("<script", 0, match.start())
        if script_start < 0 or ".q4StockSummary(" not in page[script_start:match.start()] or page.find("</script>", script_start) < match.end():
            continue
        try:
            stocks = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(stocks, list) and any(
            isinstance(stock, dict)
            and str(stock.get("StockSymbol", "")).casefold() == ticker.casefold()
            and str(stock.get("StockExchange", "")).casefold() in ("xnas", "xnys", "xase")
            for stock in stocks
        ):
            return "visible_corporate_name_and_official_q4_stock_widget"
    raise IRFeedError(f"ticker identity not found in official page title or stock link: {title!r}")


def _default_fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "NYMarketDaily/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                return response.read(), response.geturl()
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _same_origin(expected: str, actual: str) -> bool:
    a, b = urlsplit(expected), urlsplit(actual)
    return a.scheme == b.scheme == "https" and a.netloc.lower() == b.netloc.lower()


def _fetch_checked(fetch: Fetch, url: str, origin: str) -> bytes:
    raw, resolved = fetch(url)
    if not _same_origin(origin, resolved):
        raise IRFeedError(f"official feed redirected outside issuer origin: {resolved}")
    if not raw:
        raise IRFeedError(f"empty official response: {url}")
    return raw


def _date(value: str) -> date:
    for pattern in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise IRFeedError(f"unrecognized issuer release date: {value!r}")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _save_raw(output_dir: Path, prefix: str, suffix: str, raw: bytes) -> dict:
    filename = f"{prefix}-{suffix}"
    _atomic_write(output_dir / filename, raw)
    return {"file": filename, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def fetch_q4_ir_news(
    ir_url: str,
    issuer_name: str,
    ticker: str,
    market_session_date: str,
    output_dir: Path,
    *,
    prefix: str = "ny-ir",
    fetch: Fetch = _default_fetch,
) -> dict:
    """Fetch the first-party JSON feed named by an issuer's Q4 news widget.

    A returned empty target-day list means only that this verified feed has no
    target-day entry. The caller still checks SEC and other official channels.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise IRFeedError("unsafe evidence prefix")
    target = date.fromisoformat(market_session_date)
    if urlsplit(ir_url).scheme != "https" or not urlsplit(ir_url).netloc:
        raise IRFeedError("IR page must use HTTPS")
    page_raw = _fetch_checked(fetch, ir_url, ir_url)
    page = page_raw.decode("utf-8", errors="replace")
    title_match = _TITLE_RE.search(page)
    title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else ""
    identity_evidence = _identity_evidence(page, title, issuer_name, ticker)
    widget_start = page.find(".q4News(")
    if widget_start < 0:
        raise IRFeedError("official page has no Q4 investor-news widget")
    category_match = _CATEGORY_RE.search(page[widget_start:widget_start + 1000])
    script_match = _SCRIPT_RE.search(page)
    if not category_match or not script_match:
        raise IRFeedError("Q4 widget category or script missing")
    script_url = urljoin(ir_url, script_match.group(1))
    if not _same_origin(ir_url, script_url):
        raise IRFeedError("Q4 widget script is not on the issuer origin")
    script_raw = _fetch_checked(fetch, script_url, ir_url)
    if _FEED_PATH.encode() not in script_raw:
        raise IRFeedError("official Q4 widget does not declare the expected public feed")
    query = {
        "LanguageId": 1,
        "bodyType": 0,
        "pressReleaseDateFilter": 3,
        "categoryId": category_match.group(1),
        "pageSize": -1,
        "pageNumber": 0,
        "tagList": "",
        "includeTags": "true",
        "year": target.year,
        "excludeSelection": 1,
    }
    feed_url = urljoin(ir_url, _FEED_PATH) + "?" + urlencode(query)
    feed_raw = _fetch_checked(fetch, feed_url, ir_url)
    try:
        releases = json.loads(feed_raw)["GetPressReleaseListResult"]
    except (KeyError, TypeError, ValueError) as exc:
        raise IRFeedError("official Q4 feed has no release list") from exc
    if not isinstance(releases, list) or not releases:
        raise IRFeedError("official Q4 feed returned no year records")
    if len(releases) > 5000:
        raise IRFeedError("official Q4 feed year result is unexpectedly large")
    dated = []
    for release in releases:
        if not isinstance(release, dict) or not release.get("Headline") or not release.get("PressReleaseDate"):
            raise IRFeedError("official Q4 feed contains an incomplete release")
        release_day = _date(str(release["PressReleaseDate"]))
        if release_day.year != target.year:
            raise IRFeedError("official Q4 feed ignored the requested year")
        dated.append((release_day, release))
    if any(dated[i][0] < dated[i + 1][0] for i in range(len(dated) - 1)):
        raise IRFeedError("official Q4 feed is not in descending date order")

    evidence = {
        "schema_version": "ny_q4_ir_news_v1",
        "issuer_name": issuer_name,
        "ticker": ticker,
        "market_session_date": market_session_date,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_page_url": ir_url,
        "official_page_title": title,
        "official_page_identity_evidence": identity_evidence,
        "official_widget_script_url": script_url,
        "official_feed_url": feed_url,
        "year_record_count": len(releases),
        "page_raw": _save_raw(output_dir, prefix, "page.html", page_raw),
        "widget_script_raw": _save_raw(output_dir, prefix, "widget.js", script_raw),
        "feed_raw": _save_raw(output_dir, prefix, "feed.json", feed_raw),
        "target_day_releases": [],
        "note": "An empty target-day feed is not proof that no other official channel published an item.",
    }
    for index, (release_day, release) in enumerate(dated):
        if release_day != target:
            continue
        link = release.get("LinkToDetailPage")
        if not isinstance(link, str) or not link:
            raise IRFeedError("target-day issuer release has no detail URL")
        detail_url = urljoin(ir_url, link)
        if not _same_origin(ir_url, detail_url):
            raise IRFeedError("target-day issuer release points outside issuer origin")
        detail_raw = _fetch_checked(fetch, detail_url, ir_url)
        evidence["target_day_releases"].append({
            "feed_index": index,
            "press_release_id": release.get("PressReleaseId"),
            "press_release_date": release["PressReleaseDate"],
            "headline": release["Headline"],
            "detail_url": detail_url,
            "detail_raw": _save_raw(output_dir, prefix, f"release-{index}.html", detail_raw),
        })
    evidence["target_day_count"] = len(evidence["target_day_releases"])
    _atomic_write(output_dir / f"{prefix}-summary.json", (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return evidence
