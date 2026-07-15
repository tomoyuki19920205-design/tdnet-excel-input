import copy
import json

from src.events.tdnet_event_store import EventRecord, build_supabase_row


def test_build_supabase_row_recursively_excludes_xbrl_path_without_mutating_input():
    source_url = "https://www.release.tdnet.info/inbs/140120260714592943.pdf"
    raw_payload = {
        "extracted": {
            "xbrl_path": r"C:\Users\tester\AppData\Local\Temp\archive\581A.zip",
            "sales": 41_446_000_000,
            "operating_income": 7_041_000_000,
            "note": "keep raw extracted",
        },
        "source_marker": "keep raw",
    }
    extracted_payload = {
        "xbrl_path": r"C:\Users\tester\AppData\Local\Temp\worker\581A.zip",
        "sales": 41_446_000_000,
        "operating_income": 7_041_000_000,
        "guidance_sales": 48_500_000_000,
        "guidance_operating_income": 13_000_000_000,
        "note": "keep extracted",
    }
    event = EventRecord(
        source_doc_id="140120260714592943",
        ticker="581A",
        company_name="Ｇ－ＧＯ",
        disclosure_datetime="2026-07-14 15:30:00+09:00",
        title="2026年5月期 決算短信〔日本基準〕（連結）",
        doc_url=source_url,
        event_type="earnings",
        subtype="FY",
        importance=60,
        raw_payload_json=json.dumps(raw_payload, ensure_ascii=False),
        extracted_payload_json=json.dumps(extracted_payload, ensure_ascii=False),
    )
    before = copy.deepcopy(event)

    row, _, dedupe_key, _, _ = build_supabase_row(event, client=None)
    persisted = json.loads(row["raw_payload"])

    assert event == before
    assert "xbrl_path" in json.loads(event.raw_payload_json)["extracted"]
    assert "xbrl_path" in json.loads(event.extracted_payload_json)

    assert "xbrl_path" not in persisted["raw"]["extracted"]
    assert "xbrl_path" not in persisted["extracted"]
    assert r"C:\Users\tester\AppData\Local\Temp" not in json.dumps(
        persisted, ensure_ascii=False
    )

    assert row["source_url"] == source_url
    assert row["pdf_url"] == source_url
    assert row["ticker"] == "581A"
    assert row["event_type"] == "earnings"
    assert row["status"] == "active"
    assert dedupe_key
    assert persisted["raw"]["extracted"]["sales"] == 41_446_000_000
    assert persisted["raw"]["extracted"]["operating_income"] == 7_041_000_000
    assert persisted["raw"]["source_marker"] == "keep raw"
    assert persisted["extracted"]["guidance_sales"] == 48_500_000_000
    assert persisted["extracted"]["guidance_operating_income"] == 13_000_000_000
    assert persisted["extracted"]["note"] == "keep extracted"
