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
    "投資家情報", "投資家の皆", "ir情報", "irライブラリ", "ir資料室",
    "決算説明会", "決算関連資料", "決算説明資料", "irイベント", "investor",
    "financialresults", "financial-results", "presentation", "library",
)
LIBRARY_TERMS = ("ライブラリ", "資料室", "決算関連資料", "決算説明資料", "library", "presentation")
EVENT_TERMS = ("決算説明会", "irイベント", "event", "webcast")
IGNORED_HOSTS = ("release.tdnet.info", "jpx.co.jp", "jquants.com", "x.com", "youtube.com")
TERMINAL_STATUSES = (
    "discovered", "no_official_url", "ir_not_found", "fetch_failed",
    "js_required", "other_terminal_status",
)
_EXTERNAL_SOURCE_DENY_HOSTS = (
    "youtube.com", "youtu.be", "facebook.com", "x.com", "twitter.com",
    "linkedin.com", "release.tdnet.info",
)
_SOURCE_EXCLUDE_TERMS = (
    "contact", "お問い合わせ", "資料請求", "recruit", "採用", "mailir",
    "メール配信", "search", "検索", "feed=", "sitemap",
)
_SOURCE_RESOURCE_EXTENSIONS = (".css", ".js", ".json", ".xml", ".jpg", ".jpeg", ".png", ".gif", ".svg")


@dataclass(frozen=True)
class CompanySeed:
    ticker: str
    company_name: str
    universe_date: str


@dataclass(frozen=True)
class DiscoveredSource:
    url: str
    kind: str
    provenance_url: str
    is_external: bool = False


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
    sources: tuple[DiscoveredSource, ...] = ()


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
        ("provenance_url", "TEXT"),
        ("is_external", "INTEGER NOT NULL DEFAULT 0"),
        ("verified_from_official", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE company_ir_sources ADD COLUMN {name} {definition}")
    company_columns = {row[1] for row in conn.execute("PRAGMA table_info(company_ir_companies)")}
    if "first_discovery_attempted_at" not in company_columns:
        conn.execute("ALTER TABLE company_ir_companies ADD COLUMN first_discovery_attempted_at TEXT")
    for source_id, source_url, official_domain, verified in conn.execute(
        """SELECT s.id,s.source_url,c.official_domain,s.verified_from_official
           FROM company_ir_sources s LEFT JOIN company_ir_companies c ON c.ticker=s.ticker
           WHERE s.status='active'"""
    ):
        host = (urlsplit(source_url).hostname or "").lower()
        source_lower = source_url.lower()
        non_page = any(term in source_lower for term in _SOURCE_EXCLUDE_TERMS) or urlsplit(source_lower).path.endswith(_SOURCE_RESOURCE_EXTENSIONS)
        external = bool(official_domain and not _same_domain(source_url, official_domain))
        if verified and non_page:
            conn.execute(
                "UPDATE company_ir_sources SET status='rejected',last_error='non_monitor_page_candidate' WHERE id=?",
                (source_id,),
            )
        elif verified and external and (
            "." not in host
            or any(host == denied or host.endswith("." + denied) for denied in _EXTERNAL_SOURCE_DENY_HOSTS)
        ):
            conn.execute(
                "UPDATE company_ir_sources SET status='rejected',last_error='invalid_or_denied_external_host' WHERE id=?",
                (source_id,),
            )
        elif verified and external:
            conn.execute("UPDATE company_ir_sources SET is_external=1 WHERE id=?", (source_id,))
    # Monitoring pages, not every IR article: retain at most six active pages
    # per ticker. Historical/excess candidates remain auditable, never deleted.
    tickers_over_cap = conn.execute("""
        SELECT ticker FROM company_ir_sources WHERE status='active'
        GROUP BY ticker HAVING COUNT(*)>6
    """).fetchall()
    kind_rank = {"top": 0, "library": 1, "event": 2}
    for (ticker,) in tickers_over_cap:
        rows = conn.execute("""
            SELECT id,source_url,page_kind,is_external,baseline_completed_at
            FROM company_ir_sources WHERE ticker=? AND status='active'
        """, (ticker,)).fetchall()
        ranked = sorted(rows, key=lambda row: (
            row[4] is None, kind_rank.get(row[2], 9), row[3],
            len(urlsplit(row[1]).path), len(row[1]), row[0],
        ))
        keep = ranked[:6]
        external_rows = [row for row in ranked if row[3]]
        if external_rows and not any(row[3] for row in keep):
            keep[-1] = external_rows[0]
        keep_ids = {row[0] for row in keep}
        conn.executemany(
            "UPDATE company_ir_sources SET status='excess_candidate',last_error='per_ticker_source_cap' WHERE id=?",
            [(row[0],) for row in rows if row[0] not in keep_ids],
        )
    # One-time canonicalization of Phase-2 provisional statuses.
    conn.execute("UPDATE company_ir_companies SET discovery_status='pending' WHERE discovery_status='official_only'")
    conn.execute("UPDATE company_ir_companies SET discovery_status='no_official_url', first_discovery_attempted_at=COALESCE(first_discovery_attempted_at,last_validated_at) WHERE discovery_status='official_url_missing'")
    conn.execute("UPDATE company_ir_companies SET discovery_status='fetch_failed', first_discovery_attempted_at=COALESCE(first_discovery_attempted_at,last_validated_at) WHERE discovery_status='http_failed'")
    conn.execute("UPDATE company_ir_companies SET first_discovery_attempted_at=COALESCE(first_discovery_attempted_at,last_validated_at) WHERE discovery_status IN ('discovered','ir_not_found','fetch_failed','js_required','other_terminal_status','no_official_url')")
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
    raw = (label + " " + url).lower()
    value = re.sub(r"[\s_/]+", "", raw)
    return (
        any(term.replace("-", "").replace(" ", "") in value for term in IR_TERMS)
        or bool(re.search(r"(?:^|[^a-z0-9])ir(?:[^a-z0-9]|$)", raw))
    )


def _monitor_page_candidate(label: str, url: str) -> bool:
    value = (label + " " + url).lower()
    return (
        _ir_related(label, url)
        and not any(term in value for term in _SOURCE_EXCLUDE_TERMS)
        and not urlsplit(url.lower()).path.endswith(_SOURCE_RESOURCE_EXTENSIONS)
    )


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
    before_request: Callable[[], None] | None = None,
) -> DiscoveryResult:
    """Crawl only IR-like links on the official domain, with hard depth/page caps."""
    session = requests.Session()
    session.headers.update({"User-Agent": "tdnet-company-ir-discovery/1.0 (+nightly; bounded)"})
    if fetch is None:
        def fetch(url: str) -> tuple[int, bytes, str]:
            if before_request:
                before_request()
            response = session.get(url, timeout=(3, 7), allow_redirects=True)
            return response.status_code, response.content, response.url
    scope = _host_scope(official_url)
    queue = [(normalize_url(official_url), 0, "official")]
    seen: set[str] = set()
    candidates: dict[str, DiscoveredSource] = {}
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
            soup = BeautifulSoup(body[:10 * 1024 * 1024], "lxml")
            successful_html += 1
            visible_chars += len(soup.get_text(" ", strip=True))
            if depth >= max_depth:
                continue
            for anchor in soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True) or str(anchor.get("aria-label") or "")
                child = normalize_url(urljoin(resolved, str(anchor["href"])))
                if not _monitor_page_candidate(label, child) or not child.startswith(("http://", "https://")):
                    continue
                child_host = (urlsplit(child).hostname or "").lower()
                if child.lower().endswith(".pdf"):
                    continue
                same_domain = _same_domain(child, scope)
                if not same_domain:
                    if "." not in child_host or any(
                        child_host == host or child_host.endswith("." + host)
                        for host in _EXTERNAL_SOURCE_DENY_HOSTS
                    ):
                        continue
                candidates.setdefault(
                    child,
                    DiscoveredSource(child, _kind(label, child), normalize_url(resolved), not same_domain),
                )
                if same_domain:
                    queue.append((child, depth + 1, label))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    if candidates:
        all_values = sorted(candidates.values(), key=lambda item: (
            {"top": 0, "library": 1, "event": 2}.get(item.kind, 9),
            item.is_external, len(urlsplit(item.url).path), len(item.url),
        ))
        selected: list[DiscoveredSource] = []
        for kind in ("top", "library", "event"):
            selected.extend([item for item in all_values if item.kind == kind][:2])
        selected = selected[:6]
        external_values = [item for item in all_values if item.is_external]
        if external_values and not any(item.is_external for item in selected):
            if len(selected) == 6:
                selected[-1] = external_values[0]
            else:
                selected.append(external_values[0])
        values = tuple(dict.fromkeys(selected))
        by_kind = {kind: next((item.url for item in values if item.kind == kind), None) for kind in ("top", "library", "event")}
        top = by_kind["top"] or by_kind["library"] or by_kind["event"]
        return DiscoveryResult("", official_url, top, by_kind["library"], by_kind["event"], "discovered", last_status, sources=values)
    if successful_html and visible_chars < 100:
        status = "js_required"
    elif not successful_html:
        status = "fetch_failed"
    else:
        status = "ir_not_found"
    return DiscoveryResult("", official_url, None, None, None, status, last_status, "; ".join(errors)[:500] or None)


def apply_discovery_result(conn: sqlite3.Connection, ticker: str, result: DiscoveryResult, origin: str) -> list[int]:
    init_discovery_db(conn)
    now = _now_iso()
    official_domain = _host_scope(result.official_url or "") or None
    conn.execute("""
        UPDATE company_ir_companies SET official_url=?,official_domain=?,ir_top_url=?,
          ir_library_url=?,ir_event_url=?,discovery_status=?,source_origin=?,
          last_validated_at=?,last_http_status=?,last_error=?,updated_at=?,
          first_discovery_attempted_at=COALESCE(first_discovery_attempted_at,?) WHERE ticker=?
    """, (result.official_url, official_domain, result.ir_top_url, result.ir_library_url,
          result.ir_event_url, result.status, origin, now, result.http_status,
          result.error, now, now, ticker))
    source_ids: list[int] = []
    company = conn.execute("SELECT company_name FROM company_ir_companies WHERE ticker=?", (ticker,)).fetchone()
    if company:
        discovered_sources = result.sources or tuple(
            DiscoveredSource(url, kind, result.official_url or url, False)
            for kind, url in (("top", result.ir_top_url), ("library", result.ir_library_url), ("event", result.ir_event_url))
            if url
        )
        current_urls = {item.url for item in discovered_sources}
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
        for item in discovered_sources:
            conn.execute("""
                INSERT INTO company_ir_sources
                  (ticker,company_name,source_url,status,created_at,updated_at,page_kind,discovered_from,
                   provenance_url,is_external,verified_from_official)
                VALUES (?,?,?,'active',?,?,?,?,?,?,1)
                ON CONFLICT(ticker,source_url) DO UPDATE SET status='active',page_kind=excluded.page_kind,
                  discovered_from=excluded.discovered_from,provenance_url=excluded.provenance_url,
                  is_external=excluded.is_external,verified_from_official=1,updated_at=excluded.updated_at
            """, (ticker, company[0], item.url, now, now, item.kind, origin,
                  item.provenance_url, int(item.is_external)))
            source_id = conn.execute(
                "SELECT id FROM company_ir_sources WHERE ticker=? AND source_url=?",
                (ticker, item.url),
            ).fetchone()[0]
            source_ids.append(int(source_id))
    conn.commit()
    return source_ids


def discovery_report(conn: sqlite3.Connection) -> dict[str, int]:
    init_discovery_db(conn)
    scalar = lambda sql: int(conn.execute(sql).fetchone()[0])
    status_counts = {
        status: scalar(f"SELECT COUNT(*) FROM company_ir_companies WHERE discovery_status='{status}'")
        for status in ("pending", *TERMINAL_STATUSES)
    }
    total = scalar("SELECT COUNT(*) FROM company_ir_companies")
    result = {
        "tse_target_companies": scalar("SELECT COUNT(*) FROM company_ir_companies"),
        "discovery_status_total": sum(status_counts.values()),
        "discovery_status_reconciled": int(total == sum(status_counts.values())),
        "first_discovery_pass_complete": int(
            status_counts["pending"] == 0 and total == sum(status_counts.values())
        ),
        "official_site_success": scalar("SELECT COUNT(*) FROM company_ir_companies WHERE official_url IS NOT NULL"),
        "baseline_success": scalar("""SELECT COUNT(*) FROM company_ir_companies c
          WHERE EXISTS (SELECT 1 FROM company_ir_sources s WHERE s.ticker=c.ticker AND s.status='active')
            AND NOT EXISTS (SELECT 1 FROM company_ir_sources s WHERE s.ticker=c.ticker
                            AND s.status='active' AND s.baseline_completed_at IS NULL)"""),
        "assets_discovered": scalar("SELECT COUNT(*) FROM company_ir_assets"),
        "pending_notifications": scalar("SELECT COUNT(*) FROM company_ir_assets WHERE notification_status='pending'"),
        "notifications_emitted": scalar("SELECT COUNT(*) FROM company_ir_assets WHERE notified=1"),
    }
    result.update({f"status_{key}": value for key, value in status_counts.items()})
    return result
