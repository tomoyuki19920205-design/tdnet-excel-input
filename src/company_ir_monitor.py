"""Nightly-only monitor for earnings presentation materials on company IR sites.

Only compact metadata is persisted.  HTML, PDF bodies, and video bodies are
never stored.  The first successful crawl of each source is a baseline and
therefore never emits viewer events.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
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
_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}


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
    tdnet_suppressed: int = 0
    notified: int = 0
    publish_failed: int = 0


def _now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).lower()


def normalize_url(value: str) -> str:
    """Stable URL identity without fragments and common tracking parameters."""
    parts = urlsplit((value or "").strip())
    query = [
        (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


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


def _context_for(node: Tag) -> str:
    """Return a short structural context, primarily for fiscal-period headings."""
    chunks: list[str] = []
    for parent in list(node.parents)[:4]:
        if isinstance(parent, Tag):
            text = parent.get_text(" ", strip=True)
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
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[tuple[str, str], IrAsset] = {}
    for tag, attr in [(tag, "href") for tag in soup.find_all("a", href=True)] + [
        (tag, "src") for tag in soup.find_all(["iframe", "source"], src=True)
    ]:
        raw_url = str(tag.get(attr) or "").strip()
        if not raw_url or raw_url.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        asset_url = normalize_url(urljoin(source_page_url, raw_url))
        title = tag.get_text(" ", strip=True) or str(tag.get("title") or tag.get("aria-label") or "").strip()
        context = _context_for(tag)
        asset_type = classify_asset(title, asset_url, context)
        if not asset_type:
            continue
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
    """)
    conn.commit()


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


def _asset_key(ticker: str, asset: IrAsset) -> str:
    # URL is the primary identity.  A fiscal-period suffix permits the rare,
    # intentional reuse of one URL for a genuinely new period, while wording
    # changes within the same period do not generate noisy new alerts.
    period = _PERIOD_RE.search(asset.title or "")
    period_key = normalize_text(period.group(0)) if period else ""
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
    response = session.get(url, timeout=(5, 30), allow_redirects=True)
    response.raise_for_status()
    # Let BeautifulSoup detect Japanese encodings from the raw response.  Some
    # IR servers omit/incorrectly declare charset, making response.text mojibake.
    return response.content


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
    record = EventRecord(
        source_doc_id=_asset_key(source.ticker, asset),
        ticker=source.ticker,
        company_name=source.company_name,
        disclosure_datetime=first_seen_at,
        title=asset.title,
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
) -> RunStats:
    """Crawl every active source independently; one failure never stops the run."""
    init_db(conn)
    now = now_iso or _now_iso()
    session = requests.Session()
    session.headers.update({"User-Agent": "tdnet-company-ir-monitor/1.0 (+nightly; metadata-only)"})
    fetch = fetch or (lambda url: _default_fetch(url, session))
    tdnet_lookup = tdnet_lookup or (lambda ticker: _default_tdnet_lookup(ticker, session))
    publish = publish or _default_publish
    pdf_hasher = pdf_hasher or (lambda url: hash_remote_pdf(url, session))
    stats = RunStats()
    rows = conn.execute("""
        SELECT id, ticker, company_name, source_url, baseline_completed_at
        FROM company_ir_sources WHERE status='active' ORDER BY ticker, id
    """).fetchall()
    tdnet_cache: dict[str, Sequence[Mapping[str, object]]] = {}
    for source_row in rows:
        source = IrSource(int(source_row[0]), source_row[1], source_row[2], source_row[3])
        baseline_mode = source_row[4] is None
        stats.sources += 1
        try:
            html = fetch(source.source_url)
            assets = extract_assets(html, source.source_url)
        except Exception as exc:
            stats.failed_sources += 1
            logger.error("IR_SOURCE_FETCH_FAILED ticker=%s URL=%s reason=%s", source.ticker, source.source_url, exc)
            if not dry_run:
                conn.execute("""
                    UPDATE company_ir_sources SET last_checked_at=?, last_error=?,
                        failure_count=failure_count+1, updated_at=? WHERE id=?
                """, (now, str(exc)[:500], now, source.source_id))
                conn.commit()
            continue

        stats.discovered += len(assets)
        if source.ticker not in tdnet_cache and not baseline_mode:
            try:
                tdnet_cache[source.ticker] = tdnet_lookup(source.ticker)
            except Exception as exc:
                logger.warning("IR_TDNET_DEDUP_LOOKUP_FAILED ticker=%s reason=%s", source.ticker, exc)
                tdnet_cache[source.ticker] = []

        for raw_asset in assets:
            key = _asset_key(source.ticker, raw_asset)
            existing = conn.execute("""
                SELECT id, first_seen_at, content_sha256, is_baseline, notified, suppression_reason
                FROM company_ir_assets WHERE asset_key=?
            """, (key,)).fetchone()
            asset = raw_asset
            if existing:
                if not dry_run:
                    conn.execute("UPDATE company_ir_assets SET last_seen_at=? WHERE id=?", (now, existing[0]))
                if existing[4] or existing[3] or existing[5]:
                    continue
                asset = IrAsset(raw_asset.asset_type, raw_asset.title, raw_asset.asset_url,
                                raw_asset.source_page_url, existing[2])
                first_seen = existing[1]
                asset_id = existing[0]
            else:
                content_hash = None
                if not baseline_mode and raw_asset.asset_type == ASSET_MATERIAL:
                    content_hash = pdf_hasher(raw_asset.asset_url)
                asset = IrAsset(raw_asset.asset_type, raw_asset.title, raw_asset.asset_url,
                                raw_asset.source_page_url, content_hash)
                duplicate = False if baseline_mode else is_tdnet_duplicate(asset, tdnet_cache.get(source.ticker, []))
                suppression = "tdnet_duplicate" if duplicate else None
                if baseline_mode:
                    stats.baseline += 1
                else:
                    stats.new_assets += 1
                    stats.tdnet_suppressed += int(duplicate)
                if dry_run:
                    continue
                cur = conn.execute("""
                    INSERT INTO company_ir_assets
                        (asset_key,ticker,company_name,asset_type,title,asset_url,normalized_url,
                         source_page_url,first_seen_at,last_seen_at,content_sha256,is_baseline,
                         notified,suppression_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                """, (key, source.ticker, source.company_name, asset.asset_type, asset.title,
                      asset.asset_url, normalize_url(asset.asset_url), asset.source_page_url,
                      now, now, asset.content_sha256, int(baseline_mode), suppression))
                asset_id = cur.lastrowid
                first_seen = now
                if baseline_mode or duplicate:
                    continue

            try:
                if publish(source, asset, first_seen, dry_run):
                    stats.notified += 1
                    if not dry_run:
                        conn.execute("UPDATE company_ir_assets SET notified=1, notified_at=? WHERE id=?", (now, asset_id))
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
    return stats
