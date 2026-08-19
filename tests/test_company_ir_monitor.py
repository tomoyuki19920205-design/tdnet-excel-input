import sqlite3

import pytest

from src.events.common_models import EventRecord
from src.events.tdnet_event_store import build_supabase_row
from src.company_ir_monitor import (
    ASSET_MATERIAL,
    ASSET_VIDEO,
    extract_assets,
    init_db,
    run_monitor,
)


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
