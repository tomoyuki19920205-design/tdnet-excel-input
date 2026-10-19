"""Production disclosures that failed after the October 13 reason rollout."""
import importlib.util
import json
from pathlib import Path

from src.events.forecast_reason import extract_forecast_reason, extract_reason_sections
from src.events.tdnet_event_store import _merge_compare_json


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "forecast_reason"
CASES = {row["ticker"]: row for row in json.loads((FIXTURE_DIR / "october19.json").read_text(encoding="utf-8"))}


def sections(ticker):
    case = CASES[ticker]
    return extract_reason_sections(case["lines"], title=case["title"])


def test_real_pdf_requires_available_native_reader_and_preserves_forecast_not_dividend():
    assert importlib.util.find_spec("pymupdf") is not None
    path = FIXTURE_DIR / "live_6658.pdf"
    url = "https://www.release.tdnet.info/inbs/140120261016552356.pdf"
    reason = extract_forecast_reason(str(path), url, path.stem,
                                     "2027年３月期連結業績予想(上方)及び期末配当予想(増配)の修正に関するお知らせ")
    assert reason["status"] == "available"
    assert len(reason["sections"]) == 1
    assert reason["sections"][0]["heading"] == "(修正の理由)"
    assert reason["sections"][0]["text"].endswith("に公表いたしました通期連結業績予想を上方修正することといたしました。")
    assert "シライファン" not in reason["sections"][0]["text"]


def test_numbered_periods_inside_reason_are_kept_through_final_sentence():
    result = sections("9478")
    assert len(result) == 1
    body = result[0]["text"]
    assert "１．第２四半期連結累計期間" in body and "２．通期" in body
    assert body.endswith("修正の必要が生じた場合には、速やかに公表いたします。")
    assert "業績等の予想に関する注意事項" not in body


def test_interim_full_year_and_bases_are_kept_but_following_dividend_is_excluded():
    body = sections("1814")[0]["text"]
    assert "（１）2027 年３月期第２四半期累計期間" in body
    assert "（２）2027 年３月期通期" in body
    assert body.count("（連結業績予想）") == 2
    assert body.count("（個別業績予想）") == 2
    assert body.endswith("連結業績予想と同様の理由により修正いたします。")
    assert "配当予想の修正" not in body


def test_reference_table_and_dividend_subsection_do_not_replace_the_reason():
    assert "（ご参考）" not in sections("6384")[0]["text"]
    result = sections("3065")
    assert len(result) == 1
    assert "業績予想" in result[0]["context"][0]
    assert result[0]["text"].endswith("第２四半期累計期間の実績を反映した内容に修正いたします。")
    assert "配当予想を未定" not in result[0]["text"]


def test_unheaded_difference_explanation_and_separate_consolidated_individual_reasons():
    assert sections("6522")[0]["heading"] == "２．通期連結業績予想との差異について"
    assert sections("6522")[0]["text"].endswith("当期純損失は前回予想から58百万円縮小し、271百万円となりました。")
    result = sections("3189")
    assert len(result) == 2
    assert "個別業績" not in result[0]["text"]
    assert "持株会社" in result[1]["text"]


def test_previously_unfilled_october13_notice_and_plain_reason_heading():
    assert sections("2379")[0]["text"].endswith("来期の売上・利益を成長軌道に戻してまいります。")
    assert sections("6142")[0]["heading"] == "２．理由"
    assert sections("6142")[0]["text"].endswith("中間純利益は前回公表の予想数値よりも上回りました。")


def test_new_forecast_event_carries_exact_pdf_reason_into_saved_payload():
    from src.events.forecast_extractor import extract_forecast_revision

    path = FIXTURE_DIR / "live_6658.pdf"
    url = "https://www.release.tdnet.info/inbs/140120261016552356.pdf"
    event = extract_forecast_revision("", "2027年３月期連結業績予想(上方)及び期末配当予想(増配)の修正に関するお知らせ",
                                      pdf_path=str(path), doc_url=url, doc_id=path.stem)
    persisted = json.loads(event.to_json())
    assert persisted["revision_reason"]["status"] == "available"
    assert persisted["revision_reason"]["source_url"] == url
    assert persisted["revision_reason"]["sections"][0]["text"].endswith(
        "に公表いたしました通期連結業績予想を上方修正することといたしました。")


def test_reconcile_preserves_saved_reason_when_later_parse_fails():
    url = "https://www.release.tdnet.info/inbs/test.pdf"
    old_reason = {"status": "available", "source_url": url, "sections": [{"text": "原文の末尾。"}]}
    old = json.dumps({"extracted": {"revision_reason": old_reason}}, ensure_ascii=False)
    new = {"source_url": url, "raw_payload": json.dumps({"extracted": {"revision_reason": {"status": "unavailable", "source_url": url}}})}
    _merge_compare_json(new, old)
    assert json.loads(new["raw_payload"])["extracted"]["revision_reason"] == old_reason
    foreign = {"source_url": "https://www.release.tdnet.info/inbs/other.pdf",
               "raw_payload": json.dumps({"extracted": {"revision_reason": {"status": "unavailable"}}})}
    _merge_compare_json(foreign, old)
    assert json.loads(foreign["raw_payload"])["extracted"]["revision_reason"]["status"] == "unavailable"
