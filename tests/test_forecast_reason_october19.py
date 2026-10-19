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


def test_exchange_mirror_disclosures_preserve_new_reason_headings_and_endings():
    cases = (
        ("3760", "140120260409500971", 469, "（２）業績予想公表の理由",
         "来期以降の業績回復に向けた基盤を整備してまいります。", 2),
        ("1786", "140120260317583100", 413, "２． 修正の理由および今後の対応",
         "同社に対して厳格な損害賠償請求を行ってまいります。", 2),
        ("6094", "140120260319585334", 201, "【業績予想修正の理由に関する補足】",
         "公表すべき事項が生じた場合には速やかに開示いたします。", 4),
        ("7725", "140120260409501162", 141, "〈修正の理由〉",
         "通期個別業績予想を修正しております。", 1),
    )
    for ticker, doc_id, length, heading, ending, final_page in cases:
        path = FIXTURE_DIR / f"live_{ticker}_jpx.pdf"
        url = f"https://www2.jpx.co.jp/disc/{ticker}0/{doc_id}.pdf"
        reason = extract_forecast_reason(str(path), url, doc_id, "業績予想の修正に関するお知らせ")
        assert reason["status"] == "available", ticker
        assert len(reason["sections"]) == 1, ticker
        section = reason["sections"][0]
        assert section["heading"] == heading, ticker
        assert len(section["text"]) == length, ticker
        assert "".join(section["text"].split()).endswith("".join(ending.split())), ticker
        assert section["page_end"] == final_page, ticker
        assert "配当予想修正の理由" not in section["text"], ticker


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


def test_reconcile_preserves_audited_unresolved_status_but_accepts_later_pdf_reason():
    url = "https://www.release.tdnet.info/inbs/audited.pdf"
    for status in ("source_unavailable", "extraction_failed", "scope_unverified", "not_present"):
        old_reason = {"version": 1, "status": status, "source_url": url,
                      "sections": [], "evidence": {"audit": "checked"}}
        old = json.dumps({"extracted": {"revision_reason": old_reason}}, ensure_ascii=False)
        new = {"source_url": url, "raw_payload": json.dumps({"extracted": {"revision_reason": None}})}
        _merge_compare_json(new, old)
        assert json.loads(new["raw_payload"])["extracted"]["revision_reason"] == old_reason
        resolved = {"version": 1, "status": "available", "source_url": url,
                    "sections": [{"text": "原文全文。"}]}
        incoming = {"source_url": url, "raw_payload": json.dumps({"extracted": {"revision_reason": resolved}}, ensure_ascii=False)}
        _merge_compare_json(incoming, old)
        assert json.loads(incoming["raw_payload"])["extracted"]["revision_reason"] == resolved


def test_zero_width_and_bracketed_native_headings_end_before_disclosure_note():
    lines = [
        {"text": "１．2027年３月期連結業績予想の修正", "page": 1, "line": 1},
        {"text": "\u200b２．修正の理由\u200b", "page": 1, "line": 2},
        {"text": "売上高が計画を上回りました。", "page": 1, "line": 3},
        {"text": "【業績予想に関する注意事項】", "page": 1, "line": 4},
        {"text": "実際の業績は異なる可能性があります。", "page": 1, "line": 5},
    ]
    result = extract_reason_sections(lines, title="業績予想の修正")
    assert len(result) == 1
    assert result[0]["heading"] == "\u200b２．修正の理由\u200b"
    assert result[0]["text"] == "売上高が計画を上回りました。"
    lines[1]["text"] = "＜修正の理由＞"
    assert extract_reason_sections(lines, title="業績予想の修正")[0]["text"] == "売上高が計画を上回りました。"


def test_unheaded_future_projection_notice_is_not_part_of_forecast_reason():
    lines = [
        {"text": "２．修正の理由", "page": 1, "line": 1},
        {"text": "増収により通期予想を修正いたします。", "page": 1, "line": 2},
        {"text": "", "page": 1, "line": 3},
        {"text": "上記の予想は、本資料の発表日現在において入手可能な情報に基づき作成したものです。", "page": 1, "line": 4},
        {"text": "実際の業績は様々な要因により異なる場合があります。", "page": 1, "line": 5},
    ]
    result = extract_reason_sections(lines, title="業績予想修正")
    assert result[0]["text"] == "増収により通期予想を修正いたします。"


def test_attention_and_above_reason_sentence_are_not_mistaken_for_notes():
    lines = [
        {"text": "２．差異の理由", "page": 1, "line": 1},
        {"text": "注力領域の売上高が減少しました。", "page": 1, "line": 2},
        {"text": "上記の事由により、通期業績予想を修正いたします。", "page": 1, "line": 3},
        {"text": "〔予想に関する留意事項〕", "page": 1, "line": 4},
        {"text": "実際の業績は異なる場合があります。", "page": 1, "line": 5},
    ]
    result = extract_reason_sections(lines, title="業績予想と実績との差異")
    assert result[0]["text"] == "注力領域の売上高が減少しました。\n上記の事由により、通期業績予想を修正いたします。"
    for note_heading in ("【注記事項】", "【将来の見通しに関する記述等について】"):
        lines[3]["text"] = note_heading
        assert extract_reason_sections(lines, title="業績予想と実績との差異")[0]["text"] == result[0]["text"]


def test_legacy_remix_reason_continues_after_wrapped_go_sankou_made_ni():
    # Native lines from the live 2026-08-28 3825 PDF. Previously the
    # continuation line "参考までに" matched a generic reference heading,
    # leaving the saved reason ending with the single character "ご".
    lines = json.loads((FIXTURE_DIR / "live_3825_lines.json").read_text(encoding="utf-8"))
    sections = extract_reason_sections(lines, title="業績予想の修正に関するお知らせ")
    assert len(sections) == 1
    section = sections[0]
    assert (section["page_start"], section["page_end"]) == (1, 3)
    assert len(section["text"]) == 2836
    assert "ご\n参考までに" in section["text"]
    assert "3) 親会社株主に帰属する当期純利益" in section["text"]
    assert section["text"].endswith("上に努めてまいります。")
    assert "※ 上記は" not in section["text"]

    # A real reference heading still ends a reason, including the bracketed
    # 【参考情報】 heading seen in the 9330 disclosure.
    reference = [
        {"text": "２．修正の理由", "page": 1, "line": 1},
        {"text": "売上高の未達により予想を修正いたします。", "page": 1, "line": 2},
        {"text": "【参考情報】", "page": 1, "line": 3},
        {"text": "2026年９月期 領域別売上高情報", "page": 1, "line": 4},
    ]
    assert extract_reason_sections(reference)[0]["text"] == "売上高の未達により予想を修正いたします。"
