from __future__ import annotations

import json
import sqlite3

from src.events.common_models import DocumentMeta, EventType
from src.events.common_storage import ensure_events_table
from src.events.dividend_extractor import extract_dividend_revision
from src.events.dividend_policy import detect_dividend_policy_change
from src.events.event_pipeline import _dividend_to_event_record, _process_single_document
from src.events.tdnet_event_store import build_supabase_row


def test_amount_only_revision_is_not_policy_change():
    result = detect_dividend_policy_change(
        "2026年3月期期末配当予想の修正に関するお知らせ",
        "業績が当初予想を上回るため、期末配当を20円から25円に修正します。",
    )
    assert result.detected is False


def test_dividend_revision_plus_dividend_policy_change():
    event = extract_dividend_revision(
        "従来の配当性向30%を見直し、配当性向40%を目標とする方針に変更します。",
        "配当方針の変更及び配当予想の修正に関するお知らせ",
    )
    assert event.policy_change_detected is True
    assert event.policy_change_scope == "dividend_policy"
    assert event.policy_change_label == "配当方針変更"
    assert event.policy_change_metrics[0]["summary"] == "配当性向：30% → 40%"


def test_dividend_revision_plus_shareholder_return_policy_change():
    event = extract_dividend_revision(
        "株主還元方針を変更し、配当と自己株式取得を合わせた総還元性向50%を目標とします。",
        "株主還元方針の変更及び配当予想の修正に関するお知らせ",
    )
    assert event.policy_change_detected is True
    assert event.policy_change_scope == "shareholder_return_policy"
    assert event.policy_change_label == "還元方針変更"


def test_doe_introduction_is_detected():
    result = detect_dividend_policy_change(
        "配当方針の変更に関するお知らせ",
        "新たな配当方針としてDOE3.0%を目安とする基準を導入します。",
    )
    assert result.detected is True
    assert any(m["kind"] == "doe" and m["after"] == 3.0 for m in result.metrics)


def test_payout_ratio_change_extracts_before_and_after():
    result = detect_dividend_policy_change(
        "配当方針の変更に関するお知らせ",
        "配当性向の目標を従来の30%から40%へ引き上げます。",
    )
    metric = next(m for m in result.metrics if m["kind"] == "payout_ratio")
    assert metric["before"] == 30.0
    assert metric["after"] == 40.0
    assert metric["summary"] == "配当性向：30% → 40%"


def test_progressive_dividend_introduction_is_detected():
    result = detect_dividend_policy_change(
        "配当方針の変更に関するお知らせ",
        "2027年3月期より累進配当方針を導入します。",
    )
    assert result.detected is True
    assert any(m["kind"] == "progressive_dividend" for m in result.metrics)


def test_existing_policy_restatement_is_not_detected():
    result = detect_dividend_policy_change(
        "剰余金の配当に関するお知らせ",
        "当社は従来より配当性向30%を目標としております。配当方針に変更はございません。",
    )
    assert result.detected is False


def test_commemorative_or_special_dividend_only_is_not_policy_change():
    for title in (
        "創立50周年記念配当に関するお知らせ",
        "特別配当の実施に関するお知らせ",
    ):
        result = detect_dividend_policy_change(title, "今回限りの一時的な配当を実施します。")
        assert result.detected is False


def test_policy_change_stays_one_secondary_event_not_duplicate_card():
    conn = sqlite3.connect(":memory:")
    ensure_events_table(conn)
    doc = DocumentMeta(
        doc_id="policy-composite-1",
        ticker="9999",
        company_name="Test",
        disclosure_datetime="2026-08-24T16:00:00+09:00",
        title="配当方針の変更及び配当予想の修正に関するお知らせ",
    )
    results = _process_single_document(
        doc,
        conn,
        dry_run=False,
        pre_fetched={
            "text": "配当方針を変更し、DOE3%を導入します。前回予想20円、今回修正予想25円。",
            "pdf_path": "",
        },
    )
    dividend_results = [r for r in results if r.get("event_type") == EventType.DIVIDEND_REVISION]
    assert len(dividend_results) == 1
    rows = conn.execute("SELECT event_type, extracted_payload_json FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == EventType.DIVIDEND_REVISION
    assert json.loads(rows[0][1])["policy_change_detected"] is True


def test_245a_real_disclosure_policy_and_card_payload():
    title = "配当方針の変更及び2026年８月期配当予想の修正（初配）に関するお知らせ"
    text = """
    １．配当方針の変更
    当社は、株主への利益還元を重要な課題の一つと認識しておりますが、企業価値を継続的に拡大し、
    安定した事業の継続のための財政状態及び将来の事業拡大に必要な内部留保の充実を図ることが重要であると
    考え、これまで配当を実施しておりませんでした。
    一方で、当社の事業基盤、収益力及び財務基盤の強化が進展したことを踏まえ、株主の皆様への利益還元の
    充実を図るため、配当方針を変更するとともに、当社では初となる2026年８月期に係る剰余金の配当を
    実施する方針を決定いたしました。
    今後につきましては、成長投資を継続しながら、継続的な株主還元の実施に努めてまいります。
    ２．配当予想の修正
    前回予想 0円00銭 0円00銭 0円00銭
    今回修正予想 － 未定 未定
    期末配当予想を0円00銭から未定に修正するものであります。
    """
    event = extract_dividend_revision(text, title)
    assert event.policy_change_detected is True
    assert event.policy_change_label == "配当方針変更"
    assert event.policy_change_summary == "2026年8月期より初配を実施"
    assert "これまで配当を実施しておりませんでした" in event.policy_change_before
    assert "配当方針を変更" in event.policy_change_after

    record = _dividend_to_event_record(
        DocumentMeta(
            doc_id="140120260824524841",
            ticker="245A",
            company_name="Ｇ－ＩＮＧＳ",
            disclosure_datetime="2026-08-24T16:00:00+09:00",
            title=title,
            doc_url="https://www.release.tdnet.info/inbs/140120260824524841.pdf",
        ),
        event,
    )
    assert record.event_type == EventType.DIVIDEND_REVISION
    assert record.summary_text.startswith("配当修正／配当方針変更")
    assert "初配を実施" in record.summary_text
    row, raw_payload, _, category, _ = build_supabase_row(record)
    assert category == "dividend"
    assert row["event_type"] == "dividend"
    assert row["event_subtype"] == event.subtype
    assert raw_payload["extracted"]["policy_change_detected"] is True
    assert raw_payload["extracted"]["policy_change_label"] == "配当方針変更"
    assert raw_payload["text_extract_status"] == "ok"
    assert raw_payload["text_empty"] is False


def test_explicit_title_survives_empty_pdf_text():
    event = extract_dividend_revision(
        "",
        "配当方針の変更及び2026年８月期配当予想の修正（初配）に関するお知らせ",
    )
    assert event.policy_change_detected is True
    assert event.policy_change_label == "配当方針変更"
