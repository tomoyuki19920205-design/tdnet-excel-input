import json
import sqlite3

import pytest

from src.events.capital_action import (
    classify_capital_action, classify_status, extract_capital_action,
)
from src.events.common_models import DocumentMeta
from src.events.event_pipeline import process_documents
from src.events.tdnet_event_store import build_dedupe_key, build_supabase_row


def extract(title: str, body: str):
    result = extract_capital_action(title, body, disclosure_datetime="2026-09-02 15:30")
    assert result is not None
    return result


def test_normal_share_offering_fixed_ratio():
    e = extract("株式の売出しに関するお知らせ", "売出株式数 3,000,000株\n発行済株式総数 40,000,000株")
    assert e.actions == ["share_offering"]
    assert e.offering_shares == 3_000_000
    assert e.issued_shares_before == 40_000_000
    assert e.offering_ratio == pytest.approx(7.5)


def test_share_offering_with_oa():
    e = extract("株式の売出しに関するお知らせ", "売出株式数 3,000,000株\nオーバーアロットメントによる売出し 450,000株\n発行済株式総数 40,000,000株")
    assert e.offering_oa_shares == 450_000
    assert e.offering_max_shares == 3_450_000
    assert e.offering_max_ratio == pytest.approx(8.625)


def test_domestic_and_overseas_offering():
    e = extract("国内及び海外における株式売出し", "国内売出し 2,000千株\n海外売出し 1,000千株\n売出株式数 3,000千株\n発行済株式総数 50,000千株")
    assert e.offering_shares == 3_000_000
    assert e.offering_ratio == pytest.approx(6.0)


def test_public_capital_increase():
    e = extract("公募増資に関するお知らせ", "新規発行株式数 2,000,000株\n増資前の発行済株式総数 40,000,000株")
    assert e.actions == ["capital_increase"]
    assert e.new_shares_ratio == pytest.approx(5.0)


def test_third_party_capital_increase():
    e = extract("第三者割当増資に関するお知らせ", "第三者割当増資 500,000株\n発行済株式総数 10,000,000株")
    assert e.new_shares == 500_000
    assert e.new_shares_ratio == pytest.approx(5.0)


def test_combined_issue_and_offering_does_not_double_count():
    e = extract("新株式発行及び株式売出しに関するお知らせ", """公募による新株式発行 2,000,000株
第三者割当による新株式発行 300,000株
売出株式数 1,000,000株
オーバーアロットメントによる売出し 150,000株
発行済株式総数 40,000,000株""")
    assert e.actions == ["share_offering", "capital_increase"]
    assert e.max_new_shares == 2_300_000
    assert e.max_new_shares_ratio == pytest.approx(5.75)
    assert e.offering_max_shares == 1_150_000


def test_distribution_planned_has_no_ratio():
    e = extract("立会外分売に関するお知らせ", "分売予定株式数 200,000株\n分売予定期間 2026年9月8日から2026年9月12日\n発行済株式総数 10,000,000株")
    assert e.distribution_shares == 200_000
    assert e.distribution_planned_period == "2026/09/08～2026/09/12"
    assert not any(k.endswith("ratio") and v is not None for k, v in e.to_dict().items())


def test_distribution_execution_details():
    e = extract("立会外分売実施に関するお知らせ", "分売株式数 200,000株\n分売実施日 2026年9月9日\n分売価格 1,245円\n買付申込数量の限度 500株")
    assert e.distribution_date == "2026/09/09"
    assert e.distribution_price_yen == 1245
    assert e.distribution_purchase_limit_shares == 500


def test_follow_up_price_decision_status_and_dates():
    e = extract("株式の売出価格等の決定に関するお知らせ", "売出価格決定日 2026年9月10日\n申込期間 2026年9月11日から2026年9月13日\n受渡期日 2026年9月18日")
    assert e.status == "conditions_decided"
    assert e.status_detail == "売出価格決定"
    assert e.application_period == "2026/09/11～2026/09/13"


@pytest.mark.parametrize("title,status", [
    ("（訂正）公募増資に関するお知らせ", "corrected"),
    ("立会外分売中止に関するお知らせ", "cancelled"),
])
def test_correction_and_cancellation(title, status):
    assert classify_status(title) == status


def test_ratio_unavailable_is_explicit():
    e = extract("公募増資に関するお知らせ", "新規発行株式数 2,000,000株")
    assert e.new_shares_ratio is None
    assert e.ratio_unavailable_reason == "発行済株式数を確認できず"


def test_full_width_and_thousand_share_units():
    e = extract("株式の売出しに関するお知らせ", "売出株式数 ３，０００千株\n発行済株式総数 ４０，０００千株")
    assert e.offering_shares == 3_000_000
    assert e.issued_shares_before == 40_000_000


@pytest.mark.parametrize("title", [
    "株式分割に関するお知らせ", "株式報酬制度の導入について",
    "譲渡制限付株式報酬としての自己株式処分について",
    "新株予約権の発行に関するお知らせ", "転換社債型新株予約権付社債の発行",
])
def test_negative_capital_increase_cases(title):
    assert "capital_increase" not in classify_capital_action(title, "")


def test_same_disclosure_is_idempotent(tmp_path):
    db = str(tmp_path / "events.db")
    doc = DocumentMeta(doc_id="20260902555555", source_doc_id="20260902555555", ticker="1234", title="株式の売出しに関するお知らせ", disclosure_datetime="2026-09-02 15:30", text_body="売出株式数 3,000,000株\n発行済株式総数 40,000,000株")
    first = process_documents([doc], db, dry_run=False, event_types={"capital_action"})
    second = process_documents([doc], db, dry_run=False, event_types={"capital_action"})
    assert first.saved == 1
    assert second.saved == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from events where event_type='capital_action'").fetchone()[0] == 1


def test_db_fallback_uses_latest_pre_disclosure_value(tmp_path):
    db = str(tmp_path / "shares.db")
    with sqlite3.connect(db) as conn:
        conn.execute("create table per_share_data(ticker text, disclosed_date text, shares_outstanding integer)")
        conn.executemany("insert into per_share_data values(?,?,?)", [("1234", "2026-08-01", 20_000_000), ("1234", "2026-09-03", 99_000_000)])
    e = extract_capital_action("公募増資に関するお知らせ", "新規発行株式数 1,000,000株", ticker="1234", disclosure_datetime="2026-09-02 15:30", db_path=db)
    assert e and e.issued_shares_before == 20_000_000
    assert e.issued_shares_ratio_source == "per_share_data"
    assert e.new_shares_ratio == pytest.approx(5.0)


def test_supabase_payload_contract_and_document_dedupe():
    doc = DocumentMeta(doc_id="local", source_doc_id="20260902555555", ticker="1234", company_name="テスト株式会社", title="新株式発行及び株式売出しに関するお知らせ", disclosure_datetime="2026-09-02 15:30", doc_url="https://example.test/disclosure.pdf")
    result = process_documents([DocumentMeta(**{**doc.__dict__, "text_body": "新規発行株式数 2,000,000株\n売出株式数 1,000,000株\n発行済株式総数 40,000,000株"})], ":memory:", dry_run=True, event_types={"capital_action"})
    record = result.details[0]["_event_record"]
    row, raw, dedupe, category, _ = build_supabase_row(record)
    assert category == "capital_action"
    assert row["event_type"] == "capital_action"
    assert row["notify_to_discord"] is False
    assert raw["extracted"]["issued_shares_ratio_source"] == "disclosure"
    assert raw["extracted"]["new_shares_ratio"] == pytest.approx(5.0)
    assert dedupe == build_dedupe_key(record)
    assert row["pdf_url"] == doc.doc_url
