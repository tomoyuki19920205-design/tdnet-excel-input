import sqlite3

from src.company_ir_source_discovery import (
    CompanySeed, DiscoveryResult, apply_discovery_result, discover_ir_pages,
    discovery_report, init_discovery_db, sync_universe,
)


def test_bounded_same_domain_ir_discovery_registers_top_library_and_event():
    pages = {
        "https://example.co.jp/": b'''<a href="/ir/">IR</a><a href="https://evil.test/ir">IR</a>''',
        "https://example.co.jp/ir/": b'''<a href="library/">IR library</a><a href="event/">IR event</a>''',
        "https://example.co.jp/ir/library/": b"<p>library</p>",
        "https://example.co.jp/ir/event/": b"<p>event</p>",
    }
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, pages[url], url

    result = discover_ir_pages("https://example.co.jp/", fetch=fetch, max_pages=12)
    assert result.status == "discovered"
    assert result.ir_top_url == "https://example.co.jp/ir/"
    assert result.ir_library_url == "https://example.co.jp/ir/library/"
    assert result.ir_event_url == "https://example.co.jp/ir/event/"
    assert all("evil.test" not in url for url in calls)
    assert len(calls) == 4


def test_universe_sync_preserves_discovered_urls_and_source_upsert_is_idempotent():
    conn = sqlite3.connect(":memory:")
    init_discovery_db(conn)
    sync_universe(conn, [CompanySeed("4022", "ラサ工業", "2026-08-19")])
    result = DiscoveryResult(
        "4022", "https://www.rasa.co.jp/", "https://www.rasa.co.jp/ir/",
        "https://www.rasa.co.jp/ir/library/", None, "discovered", 200,
    )
    apply_discovery_result(conn, "4022", result, "tdnet_statement")
    sync_universe(conn, [CompanySeed("4022", "ラサ工業株式会社", "2026-08-20")])
    apply_discovery_result(conn, "4022", result, "tdnet_statement")
    row = conn.execute(
        "SELECT company_name,official_domain,ir_library_url FROM company_ir_companies WHERE ticker='4022'"
    ).fetchone()
    assert row == ("ラサ工業株式会社", "rasa.co.jp", "https://www.rasa.co.jp/ir/library/")
    assert conn.execute("SELECT COUNT(*) FROM company_ir_sources WHERE ticker='4022'").fetchone()[0] == 2
    assert discovery_report(conn)["notifications_emitted"] == 0
    conn.close()


def test_empty_javascript_shell_is_classified_separately():
    result = discover_ir_pages(
        "https://example.co.jp/", fetch=lambda url: (200, b'<div id="app"></div><script src="app.js"></script>', url)
    )
    assert result.status == "js_required"


def test_rediscovery_replaces_a_404_source():
    conn = sqlite3.connect(":memory:")
    init_discovery_db(conn)
    sync_universe(conn, [CompanySeed("4022", "ラサ工業", "2026-08-19")])
    conn.execute("""INSERT INTO company_ir_sources
      (ticker,company_name,source_url,status,last_error,failure_count,created_at,updated_at)
      VALUES ('4022','ラサ工業','https://www.rasa.co.jp/old','active','404 Client Error',1,'x','x')""")
    apply_discovery_result(
        conn, "4022",
        DiscoveryResult("4022", "https://www.rasa.co.jp/", "https://www.rasa.co.jp/ir/",
                        None, None, "discovered", 200),
        "404_rediscovery",
    )
    assert conn.execute(
        "SELECT status FROM company_ir_sources WHERE source_url LIKE '%/old'"
    ).fetchone()[0] == "replaced"
    conn.close()
