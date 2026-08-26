import sqlite3
import threading
import time

import pytest
import requests

from src.events.common_models import EventRecord
from src.events.tdnet_event_store import build_supabase_row
from src.company_ir_monitor import (
    ASSET_MATERIAL,
    ASSET_VIDEO,
    _ResolverCircuit,
    extract_assets,
    init_db,
    normalize_company_ir_display_title,
    normalize_url,
    run_monitor,
)
import src.company_ir_monitor as company_ir_monitor


SOURCE_URL = "https://example.test/ir/presentation.html"


def page(items, period="2026年3月期"):
    links = "".join(f'<li><a href="{url}">{title}</a></li>' for title, url in items)
    return f"<html><body><section><h2>{period} 決算説明会</h2><ul>{links}</ul></section></body></html>"


def add_source(conn, ticker="4022", company="ラサ工業", url=SOURCE_URL):
    conn.execute(
        """INSERT INTO company_ir_sources
           (ticker,company_name,source_url,status,created_at,updated_at)
           VALUES (?,?,?,'active','2026-01-01','2026-01-01')""",
        (ticker, company, url),
    )
    conn.commit()


@pytest.mark.parametrize(
    "raw_title, expected",
    [
        (
            "2026年08月20日 2027年３月期 第１四半期 決算補足説明資料（5,968KB）",
            "2027年3月期 第1四半期 決算補足説明資料",
        ),
        ("2027年3月期 第1四半期 決算補足説明資料", "2027年3月期 第1四半期 決算補足説明資料"),
        ("2027年３月期 第１四半期 決算補足説明資料", "2027年3月期 第1四半期 決算補足説明資料"),
        ("2026/08/20 2027年3月期 第1四半期 決算補足説明資料(5968KB)", "2027年3月期 第1四半期 決算補足説明資料"),
        ("2026-08-20 2027 年 3 月期 第 1 四半期 決算補足説明資料（約6MB）", "2027年3月期 第1四半期 決算補足説明資料"),
        ("2026年08月20日 2027年3月期 第1四半期 決算補足説明資料", "2027年3月期 第1四半期 決算補足説明資料"),
        ("2027年3月期 第1四半期 決算補足説明資料（1.2MB）", "2027年3月期 第1四半期 決算補足説明資料"),
    ],
)
def test_company_ir_display_title_normalization(raw_title, expected):
    assert normalize_company_ir_display_title(raw_title) == expected


def test_company_ir_display_title_normalization_preserves_substantive_identity():
    titles = {
        normalize_company_ir_display_title("2027年3月期 第1四半期 決算補足説明資料"),
        normalize_company_ir_display_title("2028年3月期 第1四半期 決算補足説明資料"),
        normalize_company_ir_display_title("2027年3月期 第2四半期 決算補足説明資料"),
        normalize_company_ir_display_title("2027年3月期 第1四半期 決算説明会資料"),
    }
    assert len(titles) == 4


def test_default_publish_uses_normalized_title_without_mutating_asset(monkeypatch):
    source = company_ir_monitor.IrSource(1, "6418", "日本金銭機械", SOURCE_URL)
    raw_title = "2026年08月20日 2027年３月期 第１四半期 決算補足説明資料（5,968KB）"
    asset = company_ir_monitor.IrAsset(
        ASSET_MATERIAL,
        raw_title,
        "https://example.test/material.pdf",
        SOURCE_URL,
        "a" * 64,
    )
    saved = []
    monkeypatch.setattr(company_ir_monitor, "save_event_to_supabase", lambda event, **_: saved.append(event) or {"action": "inserted"})

    assert company_ir_monitor._default_publish(source, asset, "2026-08-20T11:38:44+09:00", False)
    assert saved[0].title == "2027年3月期 第1四半期 決算補足説明資料"
    assert asset.title == raw_title


@pytest.fixture
def conn():
    value = sqlite3.connect(":memory:")
    init_db(value)
    yield value
    value.close()


def test_rasa_style_parser_gets_direct_material_and_video_links_only():
    html = page([
        ("決算説明会資料", "/ir/upload/presentation.pdf"),
        ("決算説明会動画", "https://www1.daiwair.jp/qlviewer/4022/index.html"),
        ("決算説明会書き起こし", "/ir/upload/transcript.pdf"),
        ("決算説明会主な質疑応答", "/ir/upload/qa.pdf"),
    ])
    assets = extract_assets(html, "https://www.rasa.co.jp/ir/event/presentation.html")
    assert [(x.asset_type, x.title, x.asset_url) for x in assets] == [
        (ASSET_MATERIAL, "2026年3月期 決算説明会資料", "https://www.rasa.co.jp/ir/upload/presentation.pdf"),
        (ASSET_VIDEO, "2026年3月期 決算説明会動画", "https://www1.daiwair.jp/qlviewer/4022/index.html"),
    ]


def test_first_run_twenty_assets_is_baseline_without_notifications(conn):
    add_source(conn)
    html = page([(f"第{i}回 決算説明会資料", f"/p{i}.pdf") for i in range(20)])
    published = []
    stats = run_monitor(conn, fetch=lambda _: html, tdnet_lookup=lambda _: [],
                        publish=lambda *args: published.append(args) or True,
                        now_iso="2026-08-18T19:00:00+09:00")
    assert stats.baseline == 20
    assert stats.notified == 0
    assert published == []
    assert conn.execute("select count(*) from company_ir_assets where is_baseline=1").fetchone()[0] == 20


def test_unchanged_after_baseline_emits_zero(conn):
    add_source(conn)
    html = page([("決算説明会資料", "/old.pdf")])
    run_monitor(conn, fetch=lambda _: html, now_iso="2026-08-17T19:00:00+09:00")
    stats = run_monitor(conn, fetch=lambda _: html, tdnet_lookup=lambda _: [],
                        publish=lambda *_: True, now_iso="2026-08-18T19:00:00+09:00")
    assert stats.new_assets == stats.notified == 0


@pytest.mark.parametrize("numbered_host", ["www2.example.test", "www3.example.test"])
def test_pdf_numbered_www_host_alias_is_one_identity(conn, numbered_host):
    add_source(conn)
    title = "2026年3月期 決算説明会資料"
    baseline = page([(title, "https://www.example.test/ir/archive/result.pdf")])
    alias = page([(title, f"https://{numbered_host}/ir/archive/result.pdf")])
    run_monitor(conn, fetch=lambda _: baseline, now_iso="2026-08-17T19:00:00+09:00")

    stats = run_monitor(
        conn,
        fetch=lambda _: alias,
        allow_notifications=True,
        publish=lambda *_: pytest.fail("numbered www PDF alias must not publish"),
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert normalize_url(f"https://{numbered_host}/ir/archive/result.pdf") == (
        "https://www.example.test/ir/archive/result.pdf"
    )
    assert stats.new_assets == stats.pending == stats.notified == 0
    assert conn.execute("SELECT COUNT(*) FROM company_ir_assets").fetchone()[0] == 1


def test_new_source_baseline_can_persist_pdf_hash_for_future_alias_dedup(conn):
    add_source(conn)
    baseline = page([("2026年3月期 決算説明会資料", "/original.pdf")])
    run_monitor(
        conn,
        fetch=lambda _: baseline,
        hash_initial_baseline=True,
        pdf_hasher=lambda _: "9" * 64,
        now_iso="2026-08-17T19:00:00+09:00",
    )
    assert conn.execute(
        "SELECT content_sha256,notification_status FROM company_ir_assets"
    ).fetchone() == ("9" * 64, "baseline")

    current = baseline + page(
        [("2026年3月期 決算説明会資料", "/cms-replacement.pdf")]
    )
    stats = run_monitor(
        conn,
        fetch=lambda _: current,
        allow_notifications=True,
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "9" * 64,
        publish=lambda *_: pytest.fail("same baseline content must not publish"),
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert stats.new_assets == 1
    assert stats.pending == stats.notified == 0
    assert conn.execute(
        "SELECT notification_status,suppression_reason FROM company_ir_assets "
        "WHERE asset_url LIKE '%cms-replacement.pdf'"
    ).fetchone() == ("suppressed", "content_duplicate")


def test_global_gate_keeps_post_baseline_asset_pending(conn):
    add_source(conn)
    old = page([("決算説明会資料", "/old.pdf")], "2025年3月期")
    current = old + page([("決算説明会資料", "/new.pdf")], "2026年3月期")
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    published = []
    stats = run_monitor(
        conn, fetch=lambda _: current, allow_notifications=False,
        tdnet_lookup=lambda _: [], publish=lambda *args: published.append(args) or True,
        pdf_hasher=lambda _: "c" * 64,
        now_iso="2026-08-18T19:00:00+09:00",
    )
    assert stats.notified == 0 and stats.baseline == 0 and stats.pending == 1
    assert published == []
    assert conn.execute(
        "SELECT is_baseline,notification_status FROM company_ir_assets WHERE asset_url LIKE '%/new.pdf'"
    ).fetchone() == (0, "pending")


def test_pending_asset_publishes_once_after_gate_opens_even_if_link_disappears(conn):
    add_source(conn)
    old = page([("決算説明会資料", "/old.pdf")], "2025年3月期")
    current = old + page([("決算説明会資料", "/new.pdf")], "2026年3月期")
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    run_monitor(conn, fetch=lambda _: current, allow_notifications=False,
                tdnet_lookup=lambda _: [], pdf_hasher=lambda _: "d" * 64,
                now_iso="2026-08-18T19:00:00+09:00")
    sent = []
    first = run_monitor(conn, fetch=lambda _: old, allow_notifications=True,
                        publish=lambda _s, asset, *_: sent.append(asset.asset_url) or True,
                        now_iso="2026-08-19T19:00:00+09:00")
    second = run_monitor(conn, fetch=lambda _: old, allow_notifications=True,
                         publish=lambda *_: True, now_iso="2026-08-20T19:00:00+09:00")
    assert first.notified == 1 and second.notified == 0
    assert sent == ["https://example.test/new.pdf"]
    assert conn.execute(
        "SELECT notification_status FROM company_ir_assets WHERE asset_url LIKE '%/new.pdf'"
    ).fetchone()[0] == "notified"


@pytest.mark.parametrize("title,url,expected_type", [
    ("第1四半期決算説明資料", "/new.pdf", ASSET_MATERIAL),
    ("決算説明会動画", "https://youtu.be/example", ASSET_VIDEO),
])
def test_new_material_or_video_notifies_once_then_never_again(conn, title, url, expected_type):
    add_source(conn)
    old = page([("決算説明会資料", "/old.pdf")], "2025年3月期")
    current = old + page([(title, url)], "2026年3月期")
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    sent = []
    first = run_monitor(conn, fetch=lambda _: current, tdnet_lookup=lambda _: [],
                        pdf_hasher=lambda _: "a" * 64,
                        publish=lambda _s, asset, *_: sent.append(asset) or True,
                        now_iso="2026-08-18T19:00:00+09:00")
    second = run_monitor(conn, fetch=lambda _: current, tdnet_lookup=lambda _: [],
                         publish=lambda *_: True, now_iso="2026-08-19T19:00:00+09:00")
    assert first.new_assets == first.notified == 1
    assert sent[0].asset_type == expected_type
    assert second.notified == 0


def test_unresolvable_company_material_is_never_published(conn):
    add_source(conn)
    run_monitor(conn, fetch=lambda _: page([]), now_iso="2026-08-17T19:00:00+09:00")
    sent = []
    stats = run_monitor(
        conn,
        fetch=lambda _: page([("第1四半期決算説明資料", "/missing.pdf")]),
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: None,
        publish=lambda *args: sent.append(args) or True,
        now_iso="2026-08-18T19:00:00+09:00",
    )
    assert stats.notified == 0 and stats.url_unverified == 1
    assert sent == []
    assert conn.execute(
        "SELECT notification_status,suppression_reason FROM company_ir_assets "
        "WHERE asset_url LIKE '%/missing.pdf'"
    ).fetchone() == ("url_unverified", "url_unverified")


def test_url_unverified_company_material_retries_and_publishes_when_valid(conn):
    add_source(conn)
    run_monitor(conn, fetch=lambda _: page([]), now_iso="2026-08-17T19:00:00+09:00")
    current = page([("第1四半期決算説明資料", "/delayed.pdf")])
    first = run_monitor(
        conn, fetch=lambda _: current, tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: None, now_iso="2026-08-18T19:00:00+09:00",
    )
    sent = []
    second = run_monitor(
        conn, fetch=lambda _: current, tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "9" * 64,
        publish=lambda _source, asset, *_: sent.append(asset.asset_url) or True,
        now_iso="2026-08-19T19:00:00+09:00",
    )
    assert first.notified == 0 and first.url_unverified == 1
    assert second.notified == 1 and sent == ["https://example.test/delayed.pdf"]


def test_tdnet_same_title_suppresses_company_event(conn):
    add_source(conn)
    run_monitor(conn, fetch=lambda _: page([]), now_iso="2026-08-17T19:00:00+09:00")
    current = page([("決算説明資料", "/company-version.pdf")])
    sent = []
    stats = run_monitor(
        conn, fetch=lambda _: current,
        tdnet_lookup=lambda _: [{"headline": "2026年3月期 決算説明資料", "pdf_url": "https://tdnet.test/other.pdf"}],
        pdf_hasher=lambda _: "b" * 64,
        publish=lambda *args: sent.append(args) or True,
        now_iso="2026-08-18T19:00:00+09:00",
    )
    assert stats.tdnet_suppressed == 1
    assert stats.notified == 0
    assert sent == []


def test_gate_off_tdnet_duplicate_is_suppressed_not_pending(conn):
    add_source(conn)
    run_monitor(conn, fetch=lambda _: page([]), now_iso="2026-08-17T19:00:00+09:00")
    stats = run_monitor(
        conn, fetch=lambda _: page([("決算説明資料", "/duplicate.pdf")]),
        allow_notifications=False,
        tdnet_lookup=lambda _: [{"headline": "2026年3月期 決算説明資料"}],
        pdf_hasher=lambda _: "e" * 64,
        now_iso="2026-08-18T19:00:00+09:00",
    )
    assert stats.tdnet_suppressed == 1 and stats.pending == stats.notified == 0
    assert conn.execute(
        "SELECT notification_status FROM company_ir_assets WHERE asset_url LIKE '%duplicate.pdf'"
    ).fetchone()[0] == "suppressed"


def test_same_pdf_at_alias_urls_keeps_one_pending_and_suppresses_alias(conn):
    add_source(conn)
    run_monitor(conn, fetch=lambda _: page([]), now_iso="2026-08-17T19:00:00+09:00")
    current = page([
        ("第1四半期決算説明資料", "/presentation.pdf"),
        ("第1四半期決算説明資料", "/alias/presentation.pdf"),
    ])
    stats = run_monitor(
        conn,
        fetch=lambda _: current,
        allow_notifications=False,
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "f" * 64,
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert stats.new_assets == 2
    assert stats.pending == 1
    assert stats.tdnet_suppressed == 0
    assert conn.execute(
        "SELECT notification_status,suppression_reason FROM company_ir_assets "
        "WHERE asset_url LIKE '%/presentation.pdf' ORDER BY id"
    ).fetchall() == [("pending", None), ("suppressed", "content_duplicate")]


def test_baseline_only_does_not_reclassify_completed_source_assets(conn):
    add_source(conn)
    old = page([("決算説明会資料", "/old.pdf")], "2025年3月期")
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    calls = []
    stats = run_monitor(conn, baseline_only=True, allow_notifications=False,
                        fetch=lambda url: calls.append(url) or old)
    assert stats.sources == 0 and calls == []


def test_one_404_does_not_stop_other_company(conn):
    add_source(conn, "1111", "失敗社", "https://bad.test/ir")
    add_source(conn, "4022", "ラサ工業", SOURCE_URL)

    def fetch(url):
        if "bad.test" in url:
            raise RuntimeError("404 Client Error")
        return page([("決算説明会資料", "/ok.pdf")])

    stats = run_monitor(conn, fetch=fetch, now_iso="2026-08-18T19:00:00+09:00")
    assert stats.sources == 2
    assert stats.failed_sources == 1
    assert stats.baseline == 1
    failed = conn.execute("select failure_count,last_error from company_ir_sources where ticker='1111'").fetchone()
    assert failed[0] == 1 and "404" in failed[1]


def test_audit_records_capture_each_source_without_changing_monitor_semantics(conn):
    add_source(conn, "1111", "失敗社", "https://bad.test/ir")
    add_source(conn, "4022", "ラサ工業", SOURCE_URL)

    def fetch(url):
        if "bad.test" in url:
            raise RuntimeError("404 Client Error")
        return page([("決算説明会資料", "/ok.pdf")])

    records = []
    stats = run_monitor(
        conn,
        fetch=fetch,
        dry_run=True,
        audit_records=records,
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert stats.sources == 2 and stats.failed_sources == 1
    assert [(row["ticker"], row["result_status"]) for row in records] == [
        ("1111", "fetch_failed"),
        ("4022", "success"),
    ]
    assert records[0]["failure_reason"].startswith("RuntimeError: 404")
    assert records[1]["asset_count"] == 1
    assert records[1]["new_asset_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM company_ir_assets").fetchone()[0] == 0


def test_default_fetch_retries_transient_timeout_then_succeeds(conn, monkeypatch):
    add_source(conn)
    calls = []

    def flaky_fetch(_url, _session):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.Timeout("temporary resolver timeout")
        return page([("決算説明会資料", "/ok.pdf")]).encode()

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", flaky_fetch)
    stats = run_monitor(
        conn,
        max_workers=1,
        request_interval_seconds=0,
        fetch_attempts=3,
        retry_backoff_seconds=(),
    )
    assert len(calls) == 3
    assert stats.failed_sources == 0 and stats.baseline == 1


def test_default_fetch_does_not_retry_404(conn, monkeypatch):
    add_source(conn)
    calls = []

    def missing_fetch(_url, _session):
        calls.append(1)
        response = requests.Response()
        response.status_code = 404
        error = requests.exceptions.HTTPError("404 Client Error", response=response)
        raise error

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", missing_fetch)
    stats = run_monitor(
        conn,
        max_workers=1,
        request_interval_seconds=0,
        fetch_attempts=3,
        retry_backoff_seconds=(),
    )
    assert len(calls) == 1 and stats.failed_sources == 1


def test_default_fetch_retries_403_once_with_standard_browser_user_agent():
    class Response:
        def __init__(self, status_code, content=b""):
            self.status_code = status_code
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                response = requests.Response()
                response.status_code = self.status_code
                raise requests.HTTPError(response=response)

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response(403) if len(self.calls) == 1 else Response(200, b"<html>IR</html>")

    session = Session()
    body = company_ir_monitor._default_fetch("https://example.com/ir", session)

    assert body == b"<html>IR</html>"
    assert len(session.calls) == 2
    assert "User-Agent" not in session.calls[0][1].get("headers", {})
    assert session.calls[1][1]["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_normalize_url_removes_php_session_identity_parameter():
    first = company_ir_monitor.normalize_url(
        "https://example.com/ir/document.pdf?PHPSESSID=first&year=2026"
    )
    second = company_ir_monitor.normalize_url(
        "https://example.com/ir/document.pdf?year=2026&PHPSESSID=second"
    )

    assert first == second == "https://example.com/ir/document.pdf?year=2026"


@pytest.mark.parametrize(
    "baseline_url, alias_url",
    [
        (
            "https://example.com/material.pdf?1787271203=",
            "https://example.com/material.pdf?1787271686=",
        ),
        (
            "https://example.com/material.pdf?PHPSESSID=first",
            "https://example.com/material.pdf?PHPSESSID=second",
        ),
    ],
)
def test_post_baseline_cachebuster_alias_is_not_a_new_asset(conn, baseline_url, alias_url):
    add_source(conn)
    title = "2026年3月期 決算説明会資料"
    run_monitor(
        conn,
        fetch=lambda _: page([(title, baseline_url)]),
        now_iso="2026-08-17T19:00:00+09:00",
    )

    stats = run_monitor(
        conn,
        fetch=lambda _: page([(title, alias_url)]),
        allow_notifications=True,
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "1" * 64,
        publish=lambda *_: pytest.fail("URL alias must not publish"),
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert stats.new_assets == stats.pending == stats.notified == 0
    assert conn.execute("SELECT COUNT(*) FROM company_ir_assets").fetchone()[0] == 1


def test_post_baseline_content_hash_duplicate_is_suppressed(conn):
    add_source(conn)
    title = "2026年3月期 決算説明会資料"
    run_monitor(
        conn,
        fetch=lambda _: page([(title, "/baseline.pdf")]),
        now_iso="2026-08-17T19:00:00+09:00",
    )
    conn.execute("UPDATE company_ir_assets SET content_sha256=?", ("2" * 64,))
    conn.commit()

    stats = run_monitor(
        conn,
        fetch=lambda _: page([(title, "/baseline.pdf"), (title, "/alias.pdf")]),
        allow_notifications=False,
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "2" * 64,
        now_iso="2026-08-18T19:00:00+09:00",
    )

    assert stats.new_assets == 1 and stats.pending == stats.notified == 0
    assert conn.execute(
        "SELECT notification_status,suppression_reason FROM company_ir_assets "
        "WHERE asset_url LIKE '%/alias.pdf'"
    ).fetchone() == ("suppressed", "content_duplicate")


def test_unavailable_publisher_keeps_pending_and_reports_failure(conn):
    add_source(conn)
    old = page([("2025年3月期 決算説明会資料", "/old.pdf")])
    current = old + page([("2026年3月期 決算説明会資料", "/new.pdf")])
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    run_monitor(
        conn,
        fetch=lambda _: current,
        allow_notifications=False,
        tdnet_lookup=lambda _: [],
        pdf_hasher=lambda _: "3" * 64,
        now_iso="2026-08-18T19:00:00+09:00",
    )

    stats = run_monitor(
        conn,
        fetch=lambda _: old,
        allow_notifications=True,
        publish=lambda *_: False,
        now_iso="2026-08-19T19:00:00+09:00",
    )

    assert stats.publish_failed == 1 and stats.notified == 0
    assert conn.execute(
        "SELECT notification_status,notified FROM company_ir_assets "
        "WHERE asset_url LIKE '%/new.pdf'"
    ).fetchone() == ("pending", 0)


def test_default_fetch_limits_parallelism_per_host(conn, monkeypatch):
    for index in range(4):
        add_source(conn, str(5000 + index), f"会社{index}", f"https://same.test/ir/{index}")
    lock = threading.Lock()
    active = 0
    peak = 0

    def measured_fetch(_url, _session):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return page([]).encode()

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", measured_fetch)
    stats = run_monitor(
        conn,
        max_workers=4,
        request_interval_seconds=0,
        per_host_concurrency=1,
    )
    assert stats.failed_sources == 0 and peak == 1


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def dns_error(host="broken.test"):
    return requests.exceptions.ConnectionError(
        f"NameResolutionError: Failed to resolve '{host}' ([Errno 11001] getaddrinfo failed)"
    )


def test_one_dns_host_does_not_open_global_circuit():
    circuit = _ResolverCircuit(threshold_hosts=3)
    for _ in range(5):
        circuit.record_dns_failure("one.test")
    assert circuit.open_count == 0


def test_multiple_dns_hosts_open_global_circuit_within_window():
    clock = FakeClock()
    circuit = _ResolverCircuit(
        threshold_hosts=3, window_seconds=15, cooldown_seconds=45,
        clock=clock, sleep=clock.sleep,
    )
    for host in ("one.test", "two.test", "three.test"):
        circuit.record_dns_failure(host)
        clock.value += 2
    assert circuit.open_count == 1
    assert circuit.total_wait_seconds == 45


def test_circuit_resumes_after_cooldown_and_successful_probe():
    clock = FakeClock()
    circuit = _ResolverCircuit(
        threshold_hosts=3, window_seconds=15, cooldown_seconds=5,
        clock=clock, sleep=clock.sleep,
    )
    for host in ("one.test", "two.test", "three.test"):
        circuit.record_dns_failure(host)
    probe, waited = circuit.before_request()
    assert probe and waited == 5
    circuit.finish_request(probe, dns_failed=False)
    assert circuit.before_request()[0] is False


def test_dns_failures_use_one_delayed_retry_and_recover(conn, monkeypatch):
    for index, host in enumerate(("one.test", "two.test", "three.test")):
        add_source(conn, str(6000 + index), f"会社{index}", f"https://{host}/ir")
    calls = {}

    def recovering_fetch(url, _session):
        calls[url] = calls.get(url, 0) + 1
        if calls[url] <= 3:
            raise dns_error(url)
        return page([]).encode()

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", recovering_fetch)
    records = []
    stats = run_monitor(
        conn,
        max_workers=3,
        request_interval_seconds=0,
        fetch_attempts=3,
        retry_backoff_seconds=(),
        resolver_cooldown_seconds=0,
        audit_records=records,
    )
    assert stats.failed_sources == 0
    assert stats.delayed_retries == stats.delayed_retry_successes == 3
    assert stats.circuit_open_count >= 1
    assert all(value == 4 for value in calls.values())
    assert all(row["initial_result"] == "fetch_failed" for row in records)
    assert all(row["final_result"] == "success" and row["delayed_retry"] for row in records)


def test_404_does_not_open_circuit_or_enter_delayed_queue(conn, monkeypatch):
    add_source(conn)
    calls = []

    def missing_fetch(_url, _session):
        calls.append(1)
        response = requests.Response()
        response.status_code = 404
        raise requests.exceptions.HTTPError("404 Client Error", response=response)

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", missing_fetch)
    stats = run_monitor(conn, request_interval_seconds=0, retry_backoff_seconds=())
    assert len(calls) == 1
    assert stats.failed_sources == 1
    assert stats.delayed_retries == stats.circuit_open_count == 0


def test_persistent_dns_failure_has_finite_attempts(conn, monkeypatch):
    add_source(conn)
    calls = []

    def always_dns(_url, _session):
        calls.append(1)
        raise dns_error()

    monkeypatch.setattr(company_ir_monitor, "_default_fetch", always_dns)
    stats = run_monitor(
        conn,
        max_workers=1,
        request_interval_seconds=0,
        fetch_attempts=3,
        retry_backoff_seconds=(),
        resolver_cooldown_seconds=0,
    )
    assert len(calls) == 4
    assert stats.failed_sources == 1 and stats.delayed_retries == 1
    assert stats.delayed_retry_successes == 0


def test_resolver_circuit_state_is_thread_safe():
    circuit = _ResolverCircuit(threshold_hosts=3, cooldown_seconds=0.02)
    for host in ("one.test", "two.test", "three.test"):
        circuit.record_dns_failure(host)
    probes = []
    lock = threading.Lock()

    def worker():
        probe, _waited = circuit.before_request()
        if probe:
            time.sleep(0.01)
        circuit.finish_request(probe, dns_failed=False)
        with lock:
            probes.append(probe)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(probes) == 1 and circuit.open_count == 1


def test_ambiguous_general_ir_material_is_not_classified():
    html = page([
        ("IR資料", "/generic.pdf"),
        ("中期経営計画説明資料", "/plan.pdf"),
        ("個人投資家説明会資料", "/retail.pdf"),
        ("Financial Results Presentation", "/valid.pdf"),
    ])
    assets = extract_assets(html, SOURCE_URL)
    assert [x.asset_url for x in assets] == ["https://example.test/valid.pdf"]


def test_same_url_changed_body_does_not_false_notify_when_title_period_unchanged(conn):
    add_source(conn)
    html = page([("決算説明会資料", "/stable.pdf")])
    run_monitor(conn, fetch=lambda _: html, now_iso="2026-08-17T19:00:00+09:00")
    stats = run_monitor(conn, fetch=lambda _: html, pdf_hasher=lambda _: "changed",
                        tdnet_lookup=lambda _: [], publish=lambda *_: True,
                        now_iso="2026-08-18T19:00:00+09:00")
    assert stats.notified == 0


def test_same_url_reused_for_a_new_fiscal_period_can_notify(conn):
    add_source(conn)
    old = page([("決算説明会資料", "/stable.pdf")], "2025年3月期")
    new = page([("決算説明会資料", "/stable.pdf")], "2026年3月期")
    run_monitor(conn, fetch=lambda _: old, now_iso="2026-08-17T19:00:00+09:00")
    stats = run_monitor(conn, fetch=lambda _: new, pdf_hasher=lambda _: "new-period",
                        tdnet_lookup=lambda _: [], publish=lambda *_: True,
                        now_iso="2026-08-18T19:00:00+09:00")
    assert stats.new_assets == stats.notified == 1


def test_pdf_cachebuster_and_render_dimensions_do_not_create_duplicates(conn):
    add_source(conn)
    first = page([("決算説明会資料", "/stable.pdf?1234567890abcdef=")])
    run_monitor(conn, fetch=lambda _: first, now_iso="2026-08-17T19:00:00+09:00")
    second = page([("決算説明会資料", "/stable.pdf?fedcba9876543210=&h=500&w=800")])
    stats = run_monitor(conn, fetch=lambda _: second, allow_notifications=False,
                        tdnet_lookup=lambda _: [], now_iso="2026-08-18T19:00:00+09:00")
    assert stats.new_assets == stats.pending == 0
    assert conn.execute("SELECT count(*) FROM company_ir_assets").fetchone()[0] == 1


@pytest.mark.parametrize("event_type", ["company_ir_material", "company_ir_video"])
def test_existing_event_store_preserves_company_ir_type_link_and_blocks_discord(event_type):
    event = EventRecord(
        ticker="4022", company_name="ラサ工業", event_type=event_type,
        title="2026年3月期 決算説明会資料", doc_url="https://example.test/direct",
        disclosure_datetime="2026-08-19T19:00:00+09:00",
    )
    row, *_ = build_supabase_row(event)
    assert row["event_type"] == event_type
    assert row["source_url"] == "https://example.test/direct"
    assert row["pdf_url"] == "https://example.test/direct"
    assert row["notify_to_discord"] is False
