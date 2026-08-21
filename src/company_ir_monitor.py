"""Nightly-only monitor for earnings presentation materials on company IR sites.

Only compact metadata is persisted.  HTML, PDF bodies, and video bodies are
never stored.  The first successful crawl of each source is a baseline and
therefore never emits viewer events.
"""
from __future__ import annotations

import csv
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from src.events.common_models import EventRecord
from src.events.tdnet_event_store import save_event_to_supabase

logger = logging.getLogger("company_ir_monitor")
JST = timezone(timedelta(hours=9))

ASSET_MATERIAL = "earnings_material"
ASSET_VIDEO = "earnings_video"
EVENT_MATERIAL = "company_ir_material"
EVENT_VIDEO = "company_ir_video"

_MATERIAL_TERMS = (
    "決算説明資料", "決算説明会資料", "決算補足説明資料", "決算説明補足資料",
    "決算補足資料", "決算説明会プレゼンテーション",
    "financialresultspresentation", "earningspresentation",
    "resultsbriefingmaterials", "presentationmaterialsforfinancialresults",
)
_VIDEO_TERMS = (
    "決算説明動画", "決算説明会動画", "決算説明会webcast", "決算説明会ライブ配信",
    "financialresultsbriefingvideo", "earningswebcast", "resultspresentationvideo",
)
_EXCLUDES = (
    "決算短信", "有価証券報告書", "四半期報告書", "適時開示", "株主総会", "招集通知",
    "統合報告書", "q&a", "qa要旨", "質疑応答", "書き起こし", "文字おこし",
    "transcript", "中期経営計画", "事業説明会", "個人投資家", "irセミナー",
    "会社説明会", "訂正", "修正について",
)
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "daiwair.jp", "logmi.jp")
_PERIOD_RE = re.compile(
    r"(?:20\d{2}|19\d{2}|令和\d+)年\s*\d{1,2}月期(?:\s*(?:第\s*[1-4一二三四]\s*四半期|通期))?"
)
_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "phpsessid"}
_STANDARD_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


@dataclass(frozen=True)
class IrSource:
    source_id: int
    ticker: str
    company_name: str
    source_url: str


@dataclass(frozen=True)
class IrAsset:
    asset_type: str
    title: str
    asset_url: str
    source_page_url: str
    content_sha256: str | None = None


@dataclass
class RunStats:
    sources: int = 0
    failed_sources: int = 0
    discovered: int = 0
    baseline: int = 0
    new_assets: int = 0
    pending: int = 0
    tdnet_suppressed: int = 0
    notified: int = 0
    publish_failed: int = 0
    individual_retries: int = 0
    delayed_retries: int = 0
    delayed_retry_successes: int = 0
    circuit_open_count: int = 0
    circuit_wait_seconds: float = 0.0


def _now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).lower()


def normalize_company_ir_display_title(value: str) -> str:
    """Derive a stable viewer title without changing persisted source metadata."""
    title = unicodedata.normalize("NFKC", value or "")
    title = re.sub(
        r"^\s*(?:\d{4}年\s*\d{1,2}月\s*\d{1,2}日|\d{4}\s*[/\-]\s*\d{1,2}\s*[/\-]\s*\d{1,2})\s*",
        "",
        title,
    )
    title = re.sub(
        r"\s*[\(\[]\s*(?:約\s*)?[\d,.]+\s*(?:KB|MB|GB)\s*[\)\]]\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月期", r"\1年\2月期", title)
    title = re.sub(r"第\s*([1-4])\s*四半期", r"第\1四半期", title)
    return re.sub(r"\s+", " ", title).strip()


def normalize_url(value: str) -> str:
    """Stable URL identity without fragments and common tracking parameters."""
    parts = urlsplit((value or "").strip())
    is_pdf = bool(re.search(r"\.pdf$", parts.path, re.IGNORECASE))
    query = [
        (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
        and not (is_pdf and key.lower() in {"h", "w", "height", "width"})
        and not (is_pdf and not val and re.fullmatch(r"[0-9a-f]{16,}", key, re.IGNORECASE))
        and not (is_pdf and not val and re.fullmatch(r"\d{8,}", key))
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    netloc = parts.netloc.lower()
    # Public company sites commonly alternate absolute PDF links between
    # www.example.jp and www2/www3.example.jp while serving identical bytes.
    # Restrict this alias rule to PDFs; source/navigation hosts stay exact.
    if is_pdf:
        netloc = re.sub(r"^www\d+\.", "www.", netloc)
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def _is_pdf_url(url: str) -> bool:
    value = normalize_text(url)
    return bool(re.search(r"\.pdf(?:$|[?&#])", value))


def classify_asset(title: str, url: str, context: str = "") -> str | None:
    """High-precision allowlist classifier for only the two requested types."""
    title_n = normalize_text(title)
    # Context is deliberately not used for positive/exclusion classification:
    # one accordion often contains a presentation, transcript, and Q&A, and a
    # sibling must never turn a generic PDF into an earnings presentation.
    combined = title_n
    if not title_n or any(term in title_n for term in _EXCLUDES):
        return None
    if any(term in combined for term in _VIDEO_TERMS):
        return ASSET_VIDEO
    if any(term in combined for term in _MATERIAL_TERMS) and _is_pdf_url(url):
        return ASSET_MATERIAL
    return None


def _context_for(node: Tag, text_cache: dict[int, str] | None = None) -> str:
    """Return a short structural context, primarily for fiscal-period headings."""
    chunks: list[str] = []
    # Preserve the original parent-context semantics because that context is
    # part of the persisted asset identity. Cache repeated container text so
    # large archive pages do not become quadratic in their number of links.
    cache = text_cache if text_cache is not None else {}
    for parent in list(node.parents)[:4]:
        if isinstance(parent, Tag):
            cache_key = id(parent)
            text = cache.get(cache_key)
            if text is None:
                text = parent.get_text(" ", strip=True)
                cache[cache_key] = text
            if len(text) <= 500:
                chunks.append(text)
            if _PERIOD_RE.search(text):
                break
    previous = node.find_all_previous(["h1", "h2", "h3", "h4", "h5", "dt"], limit=3)
    chunks.extend(tag.get_text(" ", strip=True) for tag in previous)
    return " ".join(chunks)[:1200]


def _display_title(title: str, context: str) -> str:
    clean = re.sub(r"\s+", " ", title or "").strip()
    if _PERIOD_RE.search(clean):
        return clean
    match = _PERIOD_RE.search(context or "")
    return f"{match.group(0)} {clean}" if match else clean


def extract_assets(html: str | bytes, source_page_url: str) -> list[IrAsset]:
    """Extract direct links from anchors plus iframe/source embeds."""
    # Production fetches are bytes and use the much faster lxml parser. Keep
    # the tolerant built-in parser for caller-supplied text fixtures.
    soup = BeautifulSoup(html or "", "lxml" if isinstance(html, bytes) else "html.parser")
    found: dict[tuple[str, str], IrAsset] = {}
    context_text_cache: dict[int, str] = {}
    for tag, attr in [(tag, "href") for tag in soup.find_all("a", href=True)] + [
        (tag, "src") for tag in soup.find_all(["iframe", "source"], src=True)
    ]:
        raw_url = str(tag.get(attr) or "").strip()
        if not raw_url or raw_url.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        asset_url = normalize_url(urljoin(source_page_url, raw_url))
        title = tag.get_text(" ", strip=True) or str(tag.get("title") or tag.get("aria-label") or "").strip()
        asset_type = classify_asset(title, asset_url)
        if not asset_type:
            continue
        context = _context_for(tag, context_text_cache)
        display_title = _display_title(title, context)
        key = (asset_type, asset_url)
        candidate = IrAsset(asset_type, display_title, asset_url, source_page_url)
        existing = found.get(key)
        if existing is None or len(candidate.title) > len(existing.title):
            found[key] = candidate
    return list(found.values())


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS company_ir_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            company_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            baseline_completed_at TEXT,
            last_checked_at TEXT,
            last_success_at TEXT,
            last_error TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, source_url)
        );
        CREATE TABLE IF NOT EXISTS company_ir_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            company_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            title TEXT NOT NULL,
            asset_url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            source_page_url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            content_sha256 TEXT,
            is_baseline INTEGER NOT NULL DEFAULT 0,
            notified INTEGER NOT NULL DEFAULT 0,
            notified_at TEXT,
            suppression_reason TEXT,
            CHECK(asset_type IN ('earnings_material', 'earnings_video'))
        );
        CREATE INDEX IF NOT EXISTS idx_company_ir_assets_ticker_type
            ON company_ir_assets(ticker, asset_type);
        CREATE INDEX IF NOT EXISTS idx_company_ir_assets_pending
            ON company_ir_assets(notified, is_baseline, suppression_reason);
        CREATE TABLE IF NOT EXISTS company_ir_monitor_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            notifications_enabled INTEGER NOT NULL DEFAULT 0,
            all_company_baseline_completed_at TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO company_ir_monitor_state
            (singleton, notifications_enabled, updated_at)
        VALUES (1, 0, '1970-01-01T00:00:00+09:00');
    """)
    asset_columns = {row[1] for row in conn.execute("PRAGMA table_info(company_ir_assets)")}
    if "notification_status" not in asset_columns:
        conn.execute(
            "ALTER TABLE company_ir_assets ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.execute("""
            UPDATE company_ir_assets SET notification_status=CASE
              WHEN notified=1 THEN 'notified'
              WHEN is_baseline=1 THEN 'baseline'
              WHEN suppression_reason IS NOT NULL THEN 'suppressed'
              ELSE 'pending' END
        """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_ir_assets_notification_status ON company_ir_assets(notification_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_ir_assets_normalized_identity ON company_ir_assets(ticker,asset_type,normalized_url)")
    # Canonicalization rules can grow as real-world cache-busting URLs are
    # observed. Keep stored lookup values current without rewriting asset keys.
    url_rows = conn.execute("SELECT id,asset_url,normalized_url FROM company_ir_assets").fetchall()
    normalized_updates = [
        (canonical, row[0]) for row in url_rows
        if (canonical := normalize_url(row[1])) != row[2]
    ]
    if normalized_updates:
        conn.executemany("UPDATE company_ir_assets SET normalized_url=? WHERE id=?", normalized_updates)
    # Preserve an audit row for false pending records created by an older URL
    # canonicalizer, but ensure they can never be published when the gate opens.
    pending_rows = conn.execute("""
        SELECT id,ticker,asset_type,title,normalized_url
        FROM company_ir_assets WHERE notification_status='pending'
    """).fetchall()
    for pending in pending_rows:
        candidates = conn.execute("""
            SELECT id,title FROM company_ir_assets
            WHERE ticker=? AND asset_type=? AND normalized_url=? AND id<>?
              AND notification_status IN ('baseline','notified','suppressed')
            ORDER BY CASE notification_status WHEN 'notified' THEN 0 WHEN 'baseline' THEN 1 ELSE 2 END,id
        """, (pending[1], pending[2], pending[4], pending[0])).fetchall()
        if any(_periods_compatible(pending[3], candidate[1]) for candidate in candidates):
            conn.execute("""
                UPDATE company_ir_assets
                SET notification_status='suppressed',suppression_reason='identity_duplicate'
                WHERE id=?
            """, (pending[0],))
    conn.commit()


def notifications_enabled(conn: sqlite3.Connection) -> bool:
    """Return the global production gate; a missing state is fail-closed."""
    init_db(conn)
    row = conn.execute(
        "SELECT notifications_enabled FROM company_ir_monitor_state WHERE singleton=1"
    ).fetchone()
    return bool(row and row[0])


def import_sources_csv(conn: sqlite3.Connection, csv_path: str | Path) -> int:
    path = Path(csv_path)
    if not path.exists():
        return 0
    now = _now_iso()
    changed = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            company = (row.get("company_name") or "").strip()
            source_url = normalize_url(row.get("source_url") or "")
            if not ticker or not company or not source_url.startswith(("http://", "https://")):
                continue
            conn.execute("""
                INSERT INTO company_ir_sources
                    (ticker, company_name, source_url, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(ticker, source_url) DO UPDATE SET
                    company_name=excluded.company_name, status='active', updated_at=excluded.updated_at
            """, (ticker, company, source_url, now, now))
            changed += 1
    conn.commit()
    return changed


def _period_key(title: str) -> str:
    period = _PERIOD_RE.search(title or "")
    return normalize_text(period.group(0)) if period else ""


def _periods_compatible(left_title: str, right_title: str) -> bool:
    """Require equal period identity; different/missing context stays distinct."""
    left = _period_key(left_title)
    right = _period_key(right_title)
    return left == right


def _asset_key(ticker: str, asset: IrAsset) -> str:
    # URL is the primary identity.  A fiscal-period suffix permits the rare,
    # intentional reuse of one URL for a genuinely new period, while wording
    # changes within the same period do not generate noisy new alerts.
    period_key = _period_key(asset.title)
    identity = "|".join((ticker, asset.asset_type, normalize_url(asset.asset_url), period_key))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _walk_hash_values(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"content_sha256", "pdf_sha256", "content_hash", "sha256"} and isinstance(item, str):
                yield item.lower()
            yield from _walk_hash_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_hash_values(item)


def is_tdnet_duplicate(asset: IrAsset, rows: Sequence[Mapping[str, object]]) -> bool:
    target_url = normalize_url(asset.asset_url)
    target_title = normalize_text(asset.title)
    target_hash = (asset.content_sha256 or "").lower()
    for row in rows:
        urls = (row.get("source_url"), row.get("pdf_url"))
        if any(normalize_url(str(url or "")) == target_url for url in urls if url):
            return True
        titles = (row.get("headline"), row.get("source_title"), row.get("display_title"))
        if target_title and any(normalize_text(str(title or "")) == target_title for title in titles):
            return True
        payload = row.get("raw_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if target_hash and target_hash in set(_walk_hash_values(payload)):
            return True
    return False


def hash_remote_pdf(url: str, session: requests.Session, max_bytes: int = 50 * 1024 * 1024) -> str | None:
    """Hash a new PDF in memory; abort large/non-PDF responses and persist no body."""
    try:
        response = session.get(url, timeout=(5, 30), stream=True, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "pdf" not in content_type and not _is_pdf_url(response.url or url):
            return None
        digest = hashlib.sha256()
        size = 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                logger.warning("IR_PDF_HASH_SKIPPED reason=too_large url=%s", url)
                return None
            digest.update(chunk)
        return digest.hexdigest()
    except requests.RequestException as exc:
        logger.warning("IR_PDF_HASH_FAILED url=%s reason=%s", url, exc)
        return None


def _default_fetch(url: str, session: requests.Session) -> bytes:
    response = session.get(url, timeout=(3, 10), allow_redirects=True)
    if response.status_code == 403:
        # Some verified company sites reject descriptive service user agents
        # while serving the same public HTML to ordinary browsers. This is one
        # standards-compliant retry, not a WAF/CAPTCHA bypass.
        response = session.get(
            url,
            timeout=(3, 10),
            allow_redirects=True,
            headers={"User-Agent": _STANDARD_BROWSER_USER_AGENT},
        )
    response.raise_for_status()
    # Let BeautifulSoup detect Japanese encodings from the raw response.  Some
    # IR servers omit/incorrectly declare charset, making response.text mojibake.
    # IR index HTML should be small. Bound pathological embedded payloads so a
    # single site cannot monopolize the all-company baseline parser.
    return response.content[:10 * 1024 * 1024]


_TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _is_transient_fetch_error(exc: Exception) -> bool:
    """Return true only for failures that can safely benefit from a retry."""
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and response.status_code in _TRANSIENT_HTTP_STATUSES
    if isinstance(exc, requests.exceptions.SSLError):
        # Certificate/hostname failures are generally persistent and must not be
        # hidden by repeated TLS handshakes.
        return False
    return isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))


def _is_dns_fetch_error(exc: Exception) -> bool:
    value = f"{type(exc).__name__}: {exc}".lower()
    return any(term in value for term in (
        "getaddrinfo failed", "failed to resolve", "nameresolutionerror",
        "name resolution temporary failure", "temporary failure in name resolution",
    ))


class _ResolverCircuit:
    """Small process-local circuit for cross-host DNS resolver outages."""

    def __init__(
        self,
        *,
        threshold_hosts: int = 3,
        window_seconds: float = 15.0,
        cooldown_seconds: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.threshold_hosts = max(2, threshold_hosts)
        self.window_seconds = max(0.0, window_seconds)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._clock = clock
        self._sleep = sleep
        self._condition = threading.Condition()
        self._failures: deque[tuple[float, str]] = deque()
        self._open_until = 0.0
        self._open = False
        self._probe_in_flight = False
        self.open_count = 0
        self.total_wait_seconds = 0.0

    def _open_locked(self, now: float) -> None:
        self._open = True
        self._open_until = now + self.cooldown_seconds
        self._probe_in_flight = False
        self.open_count += 1
        self.total_wait_seconds += self.cooldown_seconds
        logger.warning(
            "IR_DNS_CIRCUIT_OPEN count=%s cooldown=%.2fs",
            self.open_count, self.cooldown_seconds,
        )
        self._condition.notify_all()

    def record_dns_failure(self, host: str) -> None:
        now = self._clock()
        with self._condition:
            cutoff = now - self.window_seconds
            while self._failures and self._failures[0][0] < cutoff:
                self._failures.popleft()
            self._failures.append((now, host))
            if not self._open and len({item[1] for item in self._failures}) >= self.threshold_hosts:
                self._open_locked(now)

    def before_request(self) -> tuple[bool, float]:
        started = self._clock()
        while True:
            with self._condition:
                now = self._clock()
                if not self._open:
                    return False, max(0.0, now - started)
                if now >= self._open_until and not self._probe_in_flight:
                    self._probe_in_flight = True
                    return True, max(0.0, now - started)
                delay = max(0.01, self._open_until - now) if not self._probe_in_flight else 0.05
            self._sleep(delay)

    def finish_request(self, probe: bool, dns_failed: bool) -> None:
        if not probe:
            return
        with self._condition:
            self._probe_in_flight = False
            # A failed probe can itself be a permanently invalid hostname. Close
            # first and let record_dns_failure() require fresh cross-host evidence
            # before reopening; one bad probe must not pause the whole crawl.
            self._open = False
            self._open_until = 0.0
            self._failures.clear()
            logger.warning("IR_DNS_CIRCUIT_CLOSED probe_dns_failed=%s", dns_failed)
            self._condition.notify_all()


def _default_tdnet_lookup(ticker: str, session: requests.Session) -> list[dict]:
    base = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        return []
    response = session.get(
        base.rstrip("/") + "/rest/v1/tdnet_events",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={
            "select": "headline,source_title,display_title,source_url,pdf_url,raw_payload",
            "ticker": f"eq.{ticker}",
            "event_type": "not.in.(company_ir_material,company_ir_video)",
            "limit": "1000",
        },
        timeout=(5, 30),
    )
    response.raise_for_status()
    return response.json()


def _default_publish(source: IrSource, asset: IrAsset, first_seen_at: str, dry_run: bool) -> bool:
    event_type = EVENT_MATERIAL if asset.asset_type == ASSET_MATERIAL else EVENT_VIDEO
    display_title = normalize_company_ir_display_title(asset.title)
    record = EventRecord(
        source_doc_id=_asset_key(source.ticker, asset),
        ticker=source.ticker,
        company_name=source.company_name,
        disclosure_datetime=first_seen_at,
        title=display_title,
        doc_url=asset.asset_url,
        event_type=event_type,
        subtype="company_ir",
        importance=40,
        summary_text=asset.asset_url,
        raw_payload_json=json.dumps({"source_page_url": asset.source_page_url}, ensure_ascii=False),
        extracted_payload_json=json.dumps({
            "asset_type": asset.asset_type,
            "asset_url": asset.asset_url,
            "content_sha256": asset.content_sha256,
            "first_seen_at": first_seen_at,
        }, ensure_ascii=False),
        fingerprint=_asset_key(source.ticker, asset),
    )
    result = save_event_to_supabase(record, dry_run=dry_run)
    return dry_run or result.get("action") in {"inserted", "duplicate", "updated", "unchanged"}


def run_monitor(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    fetch: Callable[[str], str | bytes] | None = None,
    tdnet_lookup: Callable[[str], Sequence[Mapping[str, object]]] | None = None,
    publish: Callable[[IrSource, IrAsset, str, bool], bool] | None = None,
    pdf_hasher: Callable[[str], str | None] | None = None,
    now_iso: str | None = None,
    max_workers: int | None = None,
    baseline_only: bool = False,
    hash_initial_baseline: bool = False,
    allow_notifications: bool = True,
    source_ids: Sequence[int] | None = None,
    request_interval_seconds: float | None = None,
    audit_records: list[dict[str, object]] | None = None,
    fetch_attempts: int | None = None,
    per_host_concurrency: int | None = None,
    retry_backoff_seconds: Sequence[float] | None = None,
    resolver_threshold_hosts: int | None = None,
    resolver_window_seconds: float | None = None,
    resolver_cooldown_seconds: float | None = None,
    resolver_clock: Callable[[], float] = time.monotonic,
    resolver_sleep: Callable[[float], None] = time.sleep,
) -> RunStats:
    """Crawl every active source independently; one failure never stops the run."""
    init_db(conn)
    now = now_iso or _now_iso()
    session = requests.Session()
    session.headers.update({"User-Agent": "tdnet-company-ir-monitor/1.0 (+nightly; metadata-only)"})
    interval = max(0.0, request_interval_seconds if request_interval_seconds is not None else float(os.environ.get("COMPANY_IR_REQUEST_INTERVAL", "0.1")))
    pace_lock = threading.Lock()
    next_request_at = [0.0]
    attempts = max(1, min(fetch_attempts or int(os.environ.get("COMPANY_IR_FETCH_ATTEMPTS", "3")), 5))
    host_limit = max(1, min(per_host_concurrency or int(os.environ.get("COMPANY_IR_PER_HOST_CONCURRENCY", "1")), 8))
    backoffs = tuple((0.5, 1.5, 3.0, 5.0) if retry_backoff_seconds is None else retry_backoff_seconds)
    worker_local = threading.local()
    host_semaphores: dict[str, threading.BoundedSemaphore] = {}
    host_semaphores_lock = threading.Lock()
    circuit = _ResolverCircuit(
        threshold_hosts=resolver_threshold_hosts or int(os.environ.get("COMPANY_IR_DNS_CIRCUIT_HOSTS", "3")),
        window_seconds=resolver_window_seconds if resolver_window_seconds is not None else float(os.environ.get("COMPANY_IR_DNS_CIRCUIT_WINDOW", "15")),
        cooldown_seconds=resolver_cooldown_seconds if resolver_cooldown_seconds is not None else float(os.environ.get("COMPANY_IR_DNS_CIRCUIT_COOLDOWN", "45")),
        clock=resolver_clock,
        sleep=resolver_sleep,
    )

    def pace() -> None:
        if interval <= 0:
            return
        with pace_lock:
            now_mono = time.monotonic()
            wait = max(0.0, next_request_at[0] - now_mono)
            next_request_at[0] = max(now_mono, next_request_at[0]) + interval
        if wait:
            time.sleep(wait)
    custom_fetch = fetch

    def default_fetch(url: str, telemetry: dict[str, object], attempt_limit: int) -> bytes:
        worker_session = getattr(worker_local, "session", None)
        if worker_session is None:
            worker_session = requests.Session()
            worker_session.headers.update(session.headers)
            worker_local.session = worker_session
        host = (urlsplit(url).hostname or "").lower()
        with host_semaphores_lock:
            host_semaphore = host_semaphores.setdefault(host, threading.BoundedSemaphore(host_limit))
        for attempt in range(attempt_limit):
            probe, waited = circuit.before_request()
            telemetry["circuit_wait_seconds"] = float(telemetry["circuit_wait_seconds"]) + waited
            telemetry["attempt_count"] = int(telemetry["attempt_count"]) + 1
            try:
                with host_semaphore:
                    pace()
                    body = _default_fetch(url, worker_session)
                circuit.finish_request(probe, False)
                return body
            except Exception as exc:
                dns_failed = _is_dns_fetch_error(exc)
                telemetry["dns_retry"] = bool(telemetry["dns_retry"]) or dns_failed
                circuit.finish_request(probe, dns_failed)
                if dns_failed:
                    circuit.record_dns_failure(host)
                if attempt + 1 >= attempt_limit or not _is_transient_fetch_error(exc):
                    raise
                telemetry["individual_retry_count"] = int(telemetry["individual_retry_count"]) + 1
                delay = backoffs[min(attempt, len(backoffs) - 1)] if backoffs else 0.0
                logger.warning(
                    "IR_SOURCE_FETCH_RETRY host=%s attempt=%s/%s delay=%.2fs reason=%s",
                    host, attempt + 2, attempt_limit, delay, exc,
                )
                if delay > 0:
                    time.sleep(delay)
        raise AssertionError("unreachable fetch attempt loop")
    tdnet_lookup = tdnet_lookup or (lambda ticker: (pace(), _default_tdnet_lookup(ticker, session))[1])
    publish = publish or _default_publish
    pdf_hasher = pdf_hasher or (lambda url: (pace(), hash_remote_pdf(url, session))[1])
    stats = RunStats()
    where = ["status='active'"]
    params: list[object] = []
    if baseline_only:
        where.append("baseline_completed_at IS NULL")
    if source_ids is not None:
        if not source_ids:
            return stats
        where.append("id IN (" + ",".join("?" for _ in source_ids) + ")")
        params.extend(source_ids)
    rows = conn.execute(f"""
        SELECT id, ticker, company_name, source_url, baseline_completed_at
        FROM company_ir_sources WHERE {' AND '.join(where)} ORDER BY ticker, id
    """, params).fetchall()
    worker_count = max(1, min(max_workers or int(os.environ.get("COMPANY_IR_WORKERS", "4")), 64))

    def new_telemetry() -> dict[str, object]:
        return {
            "attempt_count": 0,
            "individual_retry_count": 0,
            "initial_result": None,
            "initial_failure_reason": None,
            "final_result": None,
            "dns_retry": False,
            "delayed_retry": False,
            "delayed_retry_success": False,
            "circuit_wait_seconds": 0.0,
        }

    def fetch_source(source_row):
        telemetry = new_telemetry()
        try:
            if custom_fetch is None:
                body = default_fetch(source_row[3], telemetry, attempts)
            else:
                telemetry["attempt_count"] = 1
                body = custom_fetch(source_row[3])
            telemetry["initial_result"] = "success"
            telemetry["final_result"] = "success"
            return extract_assets(body, source_row[3]), None, telemetry
        except Exception as exc:  # returned to the DB-owning main thread
            telemetry["initial_result"] = "fetch_failed"
            telemetry["initial_failure_reason"] = f"{type(exc).__name__}: {exc}"[:500]
            telemetry["final_result"] = "fetch_failed"
            return [], exc, telemetry

    prefetched: dict[int, tuple[list[IrAsset], Exception | None, dict[str, object]]] = {}
    if worker_count == 1 or len(rows) <= 1:
        for row in rows:
            prefetched[int(row[0])] = fetch_source(row)
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="company-ir") as pool:
            futures = {pool.submit(fetch_source, row): int(row[0]) for row in rows}
            for future in as_completed(futures):
                prefetched[futures[future]] = future.result()

    # A DNS wave is often shorter than the complete all-company crawl. Give
    # exhausted DNS failures exactly one final chance in the same run, after
    # the normal queue has drained. HTTP/parse failures never enter this queue.
    delayed_rows = [
        row for row in rows
        if custom_fetch is None and prefetched[int(row[0])][1] is not None
        and _is_dns_fetch_error(prefetched[int(row[0])][1])
    ]

    def delayed_fetch_source(source_row):
        source_id = int(source_row[0])
        telemetry = prefetched[source_id][2]
        telemetry["delayed_retry"] = True
        try:
            body = default_fetch(source_row[3], telemetry, 1)
            telemetry["delayed_retry_success"] = True
            telemetry["final_result"] = "success"
            return extract_assets(body, source_row[3]), None, telemetry
        except Exception as exc:
            telemetry["final_result"] = "fetch_failed"
            return [], exc, telemetry

    if delayed_rows:
        logger.warning("IR_DNS_DELAYED_QUEUE sources=%s", len(delayed_rows))
        if worker_count == 1 or len(delayed_rows) == 1:
            for row in delayed_rows:
                prefetched[int(row[0])] = delayed_fetch_source(row)
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="company-ir-dns-delayed") as pool:
                futures = {pool.submit(delayed_fetch_source, row): int(row[0]) for row in delayed_rows}
                for future in as_completed(futures):
                    prefetched[futures[future]] = future.result()

    telemetry_rows = [item[2] for item in prefetched.values()]
    stats.individual_retries = sum(int(item["individual_retry_count"]) for item in telemetry_rows)
    stats.delayed_retries = sum(bool(item["delayed_retry"]) for item in telemetry_rows)
    stats.delayed_retry_successes = sum(bool(item["delayed_retry_success"]) for item in telemetry_rows)
    stats.circuit_open_count = circuit.open_count
    stats.circuit_wait_seconds = round(circuit.total_wait_seconds, 3)

    tdnet_cache: dict[str, Sequence[Mapping[str, object]]] = {}
    publish_attempted_ids: set[int] = set()
    for source_row in rows:
        source = IrSource(int(source_row[0]), source_row[1], source_row[2], source_row[3])
        initial_baseline = source_row[4] is None
        source_discovered_before = stats.discovered
        source_new_before = stats.new_assets
        stats.sources += 1
        assets, fetch_error, telemetry = prefetched[source.source_id]
        if fetch_error is not None:
            stats.failed_sources += 1
            logger.error("IR_SOURCE_FETCH_FAILED ticker=%s URL=%s reason=%s", source.ticker, source.source_url, fetch_error)
            if not dry_run:
                conn.execute("""
                    UPDATE company_ir_sources SET last_checked_at=?, last_error=?,
                        failure_count=failure_count+1, updated_at=? WHERE id=?
                """, (now, str(fetch_error)[:500], now, source.source_id))
                conn.commit()
            if audit_records is not None:
                response = getattr(fetch_error, "response", None)
                audit_records.append({
                    "ticker": source.ticker,
                    "source_id": source.source_id,
                    "source_url": source.source_url,
                    "result_status": "fetch_failed",
                    "http_status": getattr(response, "status_code", None),
                    "failure_reason": f"{type(fetch_error).__name__}: {fetch_error}"[:500],
                    "asset_count": 0,
                    "new_asset_count": 0,
                    "initial_baseline": initial_baseline,
                    **telemetry,
                    "circuit_open_count": stats.circuit_open_count,
                    "circuit_total_wait_seconds": stats.circuit_wait_seconds,
                })
            continue

        stats.discovered += len(assets)
        for raw_asset in assets:
            key = _asset_key(source.ticker, raw_asset)
            existing = conn.execute("""
                SELECT id, first_seen_at, content_sha256, is_baseline, notified,
                       suppression_reason, notification_status
                FROM company_ir_assets WHERE asset_key=?
            """, (key,)).fetchone()
            if existing is None:
                # Some official CMSes append a different cache-buster or image
                # rendition query on every request. Match the canonical URL,
                # while retaining distinct identities when both titles name
                # different fiscal periods (intentional stable-URL reuse).
                equivalent_rows = conn.execute("""
                    SELECT id,first_seen_at,content_sha256,is_baseline,notified,
                           suppression_reason,notification_status,title
                    FROM company_ir_assets
                    WHERE ticker=? AND asset_type=? AND normalized_url=?
                    ORDER BY CASE notification_status WHEN 'notified' THEN 0
                             WHEN 'baseline' THEN 1 WHEN 'suppressed' THEN 2 ELSE 3 END,id
                """, (source.ticker, raw_asset.asset_type, normalize_url(raw_asset.asset_url))).fetchall()
                equivalent = next(
                    (row for row in equivalent_rows if _periods_compatible(raw_asset.title, row[7])),
                    None,
                )
                if equivalent is not None:
                    existing = equivalent[:7]
            asset = raw_asset
            if existing:
                if not dry_run:
                    conn.execute("UPDATE company_ir_assets SET last_seen_at=? WHERE id=?", (now, existing[0]))
                notification_status = existing[6]
                if notification_status in {"baseline", "notified", "suppressed"}:
                    continue
                asset = IrAsset(raw_asset.asset_type, raw_asset.title, raw_asset.asset_url,
                                raw_asset.source_page_url, existing[2])
                first_seen = existing[1]
                asset_id = existing[0]
                if not allow_notifications:
                    stats.pending += 1
                    continue
            else:
                content_hash = None
                if raw_asset.asset_type == ASSET_MATERIAL and (
                    not initial_baseline or hash_initial_baseline
                ):
                    content_hash = pdf_hasher(raw_asset.asset_url)
                asset = IrAsset(raw_asset.asset_type, raw_asset.title, raw_asset.asset_url,
                                raw_asset.source_page_url, content_hash)
                content_duplicate = False
                if content_hash:
                    content_duplicate = conn.execute("""
                        SELECT 1 FROM company_ir_assets
                        WHERE ticker=? AND asset_type=?
                          AND lower(content_sha256)=lower(?)
                        LIMIT 1
                    """, (source.ticker, raw_asset.asset_type, content_hash)).fetchone() is not None
                if not initial_baseline and source.ticker not in tdnet_cache:
                    try:
                        tdnet_cache[source.ticker] = tdnet_lookup(source.ticker)
                    except Exception as exc:
                        logger.warning("IR_TDNET_DEDUP_LOOKUP_FAILED ticker=%s reason=%s", source.ticker, exc)
                        tdnet_cache[source.ticker] = []
                tdnet_duplicate = False if initial_baseline else is_tdnet_duplicate(asset, tdnet_cache.get(source.ticker, []))
                duplicate = content_duplicate or tdnet_duplicate
                suppression = (
                    "content_duplicate" if content_duplicate
                    else "tdnet_duplicate" if tdnet_duplicate
                    else None
                )
                notification_status = "baseline" if initial_baseline else ("suppressed" if duplicate else "pending")
                if initial_baseline:
                    stats.baseline += 1
                else:
                    stats.new_assets += 1
                    stats.tdnet_suppressed += int(tdnet_duplicate)
                    stats.pending += int(not duplicate)
                if dry_run:
                    continue
                cur = conn.execute("""
                    INSERT INTO company_ir_assets
                        (asset_key,ticker,company_name,asset_type,title,asset_url,normalized_url,
                         source_page_url,first_seen_at,last_seen_at,content_sha256,is_baseline,
                         notified,suppression_reason,notification_status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                """, (key, source.ticker, source.company_name, asset.asset_type, asset.title,
                      asset.asset_url, normalize_url(asset.asset_url), asset.source_page_url,
                      now, now, asset.content_sha256, int(initial_baseline), suppression,
                      notification_status))
                asset_id = cur.lastrowid
                first_seen = now
                if initial_baseline or duplicate or not allow_notifications:
                    continue

            try:
                publish_attempted_ids.add(int(asset_id))
                if publish(source, asset, first_seen, dry_run):
                    stats.notified += 1
                    if not dry_run:
                        conn.execute("""UPDATE company_ir_assets SET notified=1, notified_at=?,
                            notification_status='notified' WHERE id=?""", (now, asset_id))
                else:
                    stats.publish_failed += 1
            except Exception as exc:
                stats.publish_failed += 1
                logger.error("IR_ASSET_PUBLISH_FAILED ticker=%s url=%s reason=%s", source.ticker, asset.asset_url, exc)

        if not dry_run:
            conn.execute("""
                UPDATE company_ir_sources SET baseline_completed_at=COALESCE(baseline_completed_at, ?),
                    last_checked_at=?, last_success_at=?, last_error=NULL, failure_count=0, updated_at=?
                WHERE id=?
            """, (now, now, now, now, source.source_id))
            conn.commit()
        if audit_records is not None:
            audit_records.append({
                "ticker": source.ticker,
                "source_id": source.source_id,
                "source_url": source.source_url,
                "result_status": "success",
                "http_status": 200,
                "failure_reason": None,
                "asset_count": stats.discovered - source_discovered_before,
                "new_asset_count": stats.new_assets - source_new_before,
                "initial_baseline": initial_baseline,
                **telemetry,
                "circuit_open_count": stats.circuit_open_count,
                "circuit_total_wait_seconds": stats.circuit_wait_seconds,
            })

    # A pending asset is durable notification work. It must still be emitted
    # after the gate opens even when the link has since disappeared from HTML.
    if allow_notifications and source_ids is None:
        pending_rows = conn.execute("""
            SELECT a.id,a.ticker,a.company_name,a.asset_type,a.title,a.asset_url,
                   a.source_page_url,a.content_sha256,a.first_seen_at,COALESCE(s.id,0)
            FROM company_ir_assets a
            LEFT JOIN company_ir_sources s
              ON s.ticker=a.ticker AND s.source_url=a.source_page_url
            WHERE a.notification_status='pending' AND a.notified=0
            ORDER BY a.first_seen_at,a.id
        """).fetchall()
        for row in pending_rows:
            asset_id = int(row[0])
            if asset_id in publish_attempted_ids:
                continue
            source = IrSource(int(row[9]), row[1], row[2], row[6])
            asset = IrAsset(row[3], row[4], row[5], row[6], row[7])
            try:
                if publish(source, asset, row[8], dry_run):
                    stats.notified += 1
                    if not dry_run:
                        conn.execute("""UPDATE company_ir_assets SET notified=1,notified_at=?,
                          notification_status='notified' WHERE id=?""", (now, asset_id))
                else:
                    stats.publish_failed += 1
            except Exception as exc:
                stats.publish_failed += 1
                logger.error("IR_PENDING_PUBLISH_FAILED ticker=%s url=%s reason=%s", source.ticker, asset.asset_url, exc)
        if not dry_run:
            conn.commit()
    return stats
