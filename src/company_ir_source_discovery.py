"""Bounded discovery and maintenance of official company IR source pages.

The J-Quants ``market_data_universe`` table remains the company master.  This
module stores only discovery metadata and source-page registrations; it never
copies the universe into another independently maintained master.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from src.cache.cache_manager import get_path, make_cache_key
from src.company_ir_monitor import _now_iso, init_db, normalize_url

TSE_CURRENT_MARKETS = {"0105", "0111", "0112", "0113"}
IR_TERMS = (
    "ir", "投資家情報", "投資家の皆", "ir情報", "irライブラリ", "ir資料室",
    "決算説明会", "決算関連資料", "決算説明資料", "irイベント", "investor",
    "financialresults", "financial-results", "presentation", "library",
)
LIBRARY_TERMS = ("ライブラリ", "資料室", "決算関連資料", "決算説明資料", "library", "presentation")
EVENT_TERMS = ("決算説明会", "irイベント", "event", "webcast")
IGNORED_HOSTS = ("release.tdnet.info", "jpx.co.jp", "jquants.com", "x.com", "youtube.com")


@dataclass(frozen=True)
class CompanySeed:
    ticker: str
    company_name: str
    universe_date: str


@dataclass(frozen=True)
class DiscoveryResult:
    ticker: str
    official_url: str | None
    ir_top_url: str | None
    ir_library_url: str | None
    ir_event_url: str | None
    status: str
    http_status: int | None = None
    error: str | None = None


def init_discovery_db(conn: sqlite3.Connection) -> None:
    init_db(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company_ir_companies (
            ticker TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            universe_date TEXT NOT NULL,
            official_url TEXT,
            official_domain TEXT,
            ir_top_url TEXT,
            ir_library_url TEXT,
            ir_event_url TEXT,
            discovery_status TEXT NOT NULL DEFAULT 'pending',
            source_origin TEXT,
            last_validated_at TEXT,
            last_http_status INTEGER,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_company_ir_companies_status
          ON company_ir_companies(discovery_status);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(company_ir_sources)")}
    for name, definition in (
        ("page_kind", "TEXT NOT NULL DEFAULT 'library'"),
        ("discovered_from", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE company_ir_sources ADD COLUMN {name} {definition}")
    conn.commit()


def load_tse_universe(jquants_db: str | Path) -> list[CompanySeed]:
    uri = "file:" + Path(jquants_db).resolve().as_posix() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        latest = conn.execute("SELECT MAX(date) FROM market_data_universe").fetchone()[0]
        placeholders = ",".join("?" for _ in TSE_CURRENT_MARKETS)
        rows = conn.execute(
            f"""SELECT ticker,company_name,date FROM market_data_universe
                WHERE date=? AND is_ordinary_stock=1 AND market_code IN ({placeholders})
                ORDER BY ticker""",
            (latest, *sorted(TSE_CURRENT_MARKETS)),
        ).fetchall()
        return [CompanySeed(str(t), str(n), str(d)) for t, n, d in rows]
    finally:
        conn.close()


def sync_universe(conn: sqlite3.Connection, companies: Iterable[CompanySeed]) -> int:
    """Synchronize identity from J-Quants while preserving all discovered URLs."""
    init_discovery_db(conn)
    now = _now_iso()
    count = 0
    for company in companies:
        conn.execute("""
            INSERT INTO company_ir_companies
              (ticker,company_name,universe_date,discovery_status,created_at,updated_at)
            VALUES (?,?,?,'pending',?,?)
            ON CONFLICT(ticker) DO UPDATE SET
              company_name=excluded.company_name, universe_date=excluded.universe_date,
              updated_at=excluded.updated_at
        """, (company.ticker, company.company_name, company.universe_date, now, now))
        count += 1
    conn.commit()
    return count


def latest_tdnet_documents(jquants_db: str | Path) -> dict[str, str]:
    """Return latest company-submitted financial-statement TDnet document IDs."""
    uri = "file:" + Path(jquants_db).resolve().as_posix() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("""
            WITH ranked AS (
              SELECT local_code, json_extract(raw_json,'$.DiscNo') AS disc_no,
                     ROW_NUMBER() OVER (
                       PARTITION BY substr(local_code,1,4)
                       ORDER BY disclosed_date DESC, rowid DESC
                     ) AS rn
              FROM jquants_financials_normalized
              WHERE json_extract(raw_json,'$.DiscNo') IS NOT NULL
            )
            SELECT substr(local_code,1,4),disc_no FROM ranked WHERE rn=1
        """).fetchall()
        return {str(t): (str(d) if str(d).startswith("1401") else "1401" + str(d)) for t, d in rows}
    finally:
        conn.close()


def extract_official_url_from_tdnet_pdf(data: bytes) -> str | None:
    """Extract the issuer homepage printed on page one of a TDnet statement."""
    try:
        page = PdfReader(io.BytesIO(data)).pages[0]
        text = page.extract_text() or ""
        urls = re.findall(r"https?://[^\s<>\]\[）)]+", text)
        for ref in page.get("/Annots") or []:
            uri = ((ref.get_object().get("/A") or {}).get("/URI"))
            if uri:
                urls.append(str(uri))
    except Exception:
        return None
    candidates = []
    for raw in urls:
        url = normalize_url(raw.rstrip(".,、。"))
        host = (urlsplit(url).hostname or "").lower()
        if url.startswith(("http://", "https://")) and host and not any(x in host for x in IGNORED_HOSTS):
            candidates.append(url)
    if not candidates:
        return None
    return min(dict.fromkeys(candidates), key=lambda u: (len(urlsplit(u).path), len(u)))


def cached_tdnet_pdf(cache_root: str | Path, document_id: str) -> bytes | None:
    key = make_cache_key("tdnet_pdf", doc_id=document_id)
    path = Path(cache_root) / "pdf" / f"{key}.pdf"
    return path.read_bytes() if path.exists() else None


def _host_scope(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_domain(url: str, official_scope: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return bool(host and (host == official_scope or host.endswith("." + official_scope)))


def _ir_related(label: str, url: str) -> bool:
    value = re.sub(r"[\s_/]+", "", (label + " " + url)).lower()
    return any(term.replace("-", "").replace(" ", "") in value for term in IR_TERMS)


def _kind(label: str, url: str) -> str:
    value = (label + " " + url).lower()
    if any(term.lower() in value for term in EVENT_TERMS):
        return "event"
    if any(term.lower() in value for term in LIBRARY_TERMS):
        return "library"
    return "top"


def discover_ir_pages(
    official_url: str,
    *,
    fetch: Callable[[str], tuple[int, bytes, str]] | None = None,
    max_depth: int = 2,
    max_pages: int = 4,
) -> DiscoveryResult:
    """Crawl only IR-like links on the official domain, with hard depth/page caps."""
    session = requests.Session()
    session.headers.update({"User-Agent": "tdnet-company-ir-discovery/1.0 (+nightly; bounded)"})
    if fetch is None:
        def fetch(url: str) -> tuple[int, bytes, str]:
            response = session.get(url, timeout=(3, 7), allow_redirects=True)
            return response.status_code, response.content, response.url
    scope = _host_scope(official_url)
    queue = [(normalize_url(official_url), 0, "official")]
    seen: set[str] = set()
    found: dict[str, str] = {}
    last_status: int | None = None
    errors: list[str] = []
    successful_html = 0
    visible_chars = 0
    while queue and len(seen) < max_pages:
        url, depth, seed_label = queue.pop(0)
        if url in seen or depth > max_depth or not _same_domain(url, scope):
            continue
        seen.add(url)
        try:
            status, body, resolved = fetch(url)
            last_status = status
            if status != 200:
                errors.append(f"HTTP {status}: {url}")
                continue
            if not _same_domain(resolved, scope):
                continue
            soup = BeautifulSoup(body, "html.parser")
            successful_html += 1
            visible_chars += len(soup.get_text(" ", strip=True))
            if depth and _ir_related(seed_label, resolved):
                found.setdefault(_kind(seed_label, resolved), normalize_url(resolved))
            if depth >= max_depth:
                continue
            for anchor in soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True) or str(anchor.get("aria-label") or "")
                child = normalize_url(urljoin(resolved, str(anchor["href"])))
                if _same_domain(child, scope) and _ir_related(label, child):
                    queue.append((child, depth + 1, label))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    if found:
        top = found.get("top") or found.get("library") or found.get("event")
        return DiscoveryResult("", official_url, top, found.get("library"), found.get("event"), "discovered", last_status)
    if successful_html and visible_chars < 100:
        status = "js_required"
    elif not successful_html:
        status = "http_failed"
    else:
        status = "ir_not_found"
    return DiscoveryResult("", official_url, None, None, None, status, last_status, "; ".join(errors)[:500] or None)


def apply_discovery_result(conn: sqlite3.Connection, ticker: str, result: DiscoveryResult, origin: str) -> None:
    init_discovery_db(conn)
    now = _now_iso()
    official_domain = _host_scope(result.official_url or "") or None
    conn.execute("""
        UPDATE company_ir_companies SET official_url=?,official_domain=?,ir_top_url=?,
          ir_library_url=?,ir_event_url=?,discovery_status=?,source_origin=?,
          last_validated_at=?,last_http_status=?,last_error=?,updated_at=? WHERE ticker=?
    """, (result.official_url, official_domain, result.ir_top_url, result.ir_library_url,
          result.ir_event_url, result.status, origin, now, result.http_status,
          result.error, now, ticker))
    company = conn.execute("SELECT company_name FROM company_ir_companies WHERE ticker=?", (ticker,)).fetchone()
    if company:
        current_urls = {url for url in (result.ir_top_url, result.ir_library_url, result.ir_event_url) if url}
        if current_urls:
            for source_id, source_url in conn.execute(
                "SELECT id,source_url FROM company_ir_sources WHERE ticker=? AND last_error LIKE '%404%'",
                (ticker,),
            ):
                if source_url not in current_urls:
                    conn.execute(
                        "UPDATE company_ir_sources SET status='replaced',updated_at=? WHERE id=?",
                        (now, source_id),
                    )
        for kind, url in (("top", result.ir_top_url), ("library", result.ir_library_url), ("event", result.ir_event_url)):
            if not url:
                continue
            conn.execute("""
                INSERT INTO company_ir_sources
                  (ticker,company_name,source_url,status,created_at,updated_at,page_kind,discovered_from)
                VALUES (?,?,?,'active',?,?,?,?)
                ON CONFLICT(ticker,source_url) DO UPDATE SET status='active',page_kind=excluded.page_kind,
                  discovered_from=excluded.discovered_from,updated_at=excluded.updated_at
            """, (ticker, company[0], url, now, now, kind, origin))
    conn.commit()


def discovery_report(conn: sqlite3.Connection) -> dict[str, int]:
    init_discovery_db(conn)
    scalar = lambda sql: int(conn.execute(sql).fetchone()[0])
    return {
        "tse_target_companies": scalar("SELECT COUNT(*) FROM company_ir_companies"),
        "official_site_success": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE official_url IS NOT NULL"),
        "ir_page_discovered": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='discovered'"),
        "ir_page_not_found": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status IN ('ir_not_found','official_url_missing')"),
        "http_failed": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='http_failed'"),
        "js_unavailable": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='js_required'"),
        "baseline_success": scalar("""SELECT COUNT(*) FROM company_ir_companies c
          WHERE EXISTS (SELECT 1 FROM company_ir_sources s WHERE s.ticker=c.ticker AND s.status='active')
            AND NOT EXISTS (SELECT 1 FROM company_ir_sources s WHERE s.ticker=c.ticker
                            AND s.status='active' AND s.baseline_completed_at IS NULL)"""),
        "assets_discovered": scalar("SELECT COUNT(*) FROM company_ir_assets"),
        "notifications_emitted": scalar("SELECT COUNT(*) FROM company_ir_assets WHERE notified=1"),
    }
