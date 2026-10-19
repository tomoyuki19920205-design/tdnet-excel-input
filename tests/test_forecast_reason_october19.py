"""Production disclosures that failed after the October 13 reason rollout."""
import importlib.util
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.events.forecast_reason import extract_forecast_reason, extract_reason_sections
from src.events.forecast_source import exchange_pdf_url
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


def test_exact_id_exchange_fallback_records_both_pdf_sources():
    from src.events.forecast_extractor import extract_forecast_revision

    original = "https://www.release.tdnet.info/inbs/140120260409500971.pdf"
    mirror = "https://www2.jpx.co.jp/disc/37600/140120260409500971.pdf"
    assert exchange_pdf_url(original, "3760") == mirror
    assert exchange_pdf_url(original, "376A") == "https://www2.jpx.co.jp/disc/376A0/140120260409500971.pdf"
    assert exchange_pdf_url(original, "376a") == ""
    assert exchange_pdf_url("https://other.example/inbs/140120260409500971.pdf", "3760") == ""
    with patch("src.events.forecast_extractor.os.unlink"):
        with patch("src.events.forecast_extractor._download_pdf_for_ocr",
                   side_effect=["", str(FIXTURE_DIR / "live_3760_jpx.pdf")]) as downloader:
            event = extract_forecast_revision("", "通期業績予想の公表及び配当予想の修正（無配）に関するお知らせ",
                                              doc_url=original, doc_id="140120260409500971", ticker="3760")
        assert [call.args[0] for call in downloader.call_args_list] == [original, mirror]
    reason = event.revision_reason
    assert reason["status"] == "available"
    assert reason["source_url"] == mirror
    assert reason["original_tdnet_url"] == original
    assert reason["sections"][0]["text"].endswith("来期以降の業績回復に向けた基盤を整備してまいります。")


def test_normal_forecast_pipeline_uses_same_id_exchange_pdf_when_tdnet_is_missing():
    import pymupdf
    from src.events.common_models import DocumentMeta, EventType
    from src.events.event_pipeline import _process_single_document

    doc_id = "140120260317583161"
    original = f"https://www.release.tdnet.info/inbs/{doc_id}.pdf"
    mirror = f"https://www2.jpx.co.jp/disc/78220/{doc_id}.pdf"
    path = FIXTURE_DIR / "live_7822_jpx.pdf"
    with pymupdf.open(path) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
    doc = DocumentMeta(doc_id=doc_id, ticker="7822",
                       title="特別損失（連結子会社における減損損失）の計上、業績予想の修正及び財務制限条項抵触の見込みに関するお知らせ",
                       doc_url=original, disclosure_datetime="2026-03-17T15:30:00+00:00")
    with patch("src.events.event_pipeline._download_and_save_pdf",
               side_effect=[("", ""), (text, str(path))]) as downloader:
        result = _process_single_document(doc, sqlite3.connect(":memory:"),
                                          event_types={EventType.FORECAST_REVISION}, dry_run=True)
    assert [call.args[0] for call in downloader.call_args_list] == [original, mirror]
    assert doc.doc_url == mirror
    assert len(result) == 1 and result[0]["action"] == "dry_run"
    reason = json.loads(result[0]["_event_record"].extracted_payload_json)["revision_reason"]
    assert reason["status"] == "available"
    assert reason["source_url"] == mirror and reason["original_tdnet_url"] == original
    assert reason["sections"][0]["text"].endswith("前回発表予想を大きく下回る見込みとなりました。")


def test_pipeline_with_cached_text_links_to_exchange_pdf_for_reason():
    import pymupdf
    from src.events.common_models import DocumentMeta, EventType
    from src.events.event_pipeline import _process_single_document

    doc_id = "140120260317583161"
    original = f"https://www.release.tdnet.info/inbs/{doc_id}.pdf"
    mirror = f"https://www2.jpx.co.jp/disc/78220/{doc_id}.pdf"
    path = FIXTURE_DIR / "live_7822_jpx.pdf"
    with pymupdf.open(path) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
    doc = DocumentMeta(doc_id=doc_id, ticker="7822",
                       title="特別損失（連結子会社における減損損失）の計上、業績予想の修正及び財務制限条項抵触の見込みに関するお知らせ",
                       doc_url=original, disclosure_datetime="2026-03-17T15:30:00+00:00",
                       text_body=text)
    with patch("src.events.forecast_extractor.os.unlink"):
        with patch("src.events.forecast_extractor._download_pdf_for_ocr",
                   side_effect=["", str(path)]) as downloader:
            result = _process_single_document(doc, sqlite3.connect(":memory:"),
                                              event_types={EventType.FORECAST_REVISION}, dry_run=True)
    assert [call.args[0] for call in downloader.call_args_list] == [original, mirror]
    assert doc.doc_url == mirror
    assert result[0]["action"] == "dry_run"
    reason = json.loads(result[0]["_event_record"].extracted_payload_json)["revision_reason"]
    assert reason["source_url"] == mirror and reason["original_tdnet_url"] == original


def test_numbered_periods_inside_reason_are_kept_through_final_sentence():
    result = sections("9478")
    assert len(result) == 1
    body = result[0]["text"]
    assert "１．第２四半期連結累計期間" in body and "２．通期" in body
    assert body.endswith("修正の必要が生じた場合には、速やかに公表いたします。")
    assert "業績等の予想に関する注意事項" not in body


def test_unnumbered_reason_stops_at_next_numbered_forecast_chapter():
    texts = [
        "１．2026年３月期連結業績予想と実績値との差異",
        "差異の理由",
        "売上高が前回予想を上回りました。",
        "２．2026年３月期個別業績予想と実績値との差異",
        "売上高 営業利益 経常利益",
        "差異の理由",
        "個別業績も前回予想を上回りました。",
    ]
    lines = [dict(text=text, page=1, line=index) for index, text in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title="業績予想と実績値との差異に関するお知らせ")
    assert [section["text"] for section in result] == [
        "売上高が前回予想を上回りました。",
        "個別業績も前回予想を上回りました。",
    ]


def test_bullet_dividend_chapter_cannot_capture_forecast_reason():
    texts = [
        "・業績予想の修正について", "修正の理由", "売上高が前回発表予想を上回る見込みです。",
        "・配当予想について", "修正の理由", "安定配当の方針を踏まえました。",
    ]
    lines = [dict(text=text, page=1, line=index) for index, text in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title="業績予想及び配当予想の修正")
    assert len(result) == 1
    assert result[0]["text"] == "売上高が前回発表予想を上回る見込みです。"


def test_unnumbered_dividend_chapter_on_next_page_stops_original_reason():
    texts = [
        "（差異の理由）", "売上高は前回予想を若干下回る結果となりました。",
        "利益面は前回予想を上回りました。", "期末配当の内容",
        "決定額 直近の配当予想 前期実績", "（理由）",
        "前回の配当予想から増額します。",
    ]
    lines = [dict(text=value, page=1 if index < 4 else 2,
                  line=index if index < 4 else index - 3)
             for index, value in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title="業績予想と実績との差異及び剰余金の配当")
    assert len(result) == 1
    assert result[0]["text"] == "売上高は前回予想を若干下回る結果となりました。\n利益面は前回予想を上回りました。"
    assert result[0]["page_end"] == 1


def test_subsidiary_dividend_as_profit_cause_remains_in_forecast_reason():
    texts = [
        "２．差異の理由", "営業利益は前期を上回りました。", "",
        "また、前期は一部の連結子会社からのみ配当金を受け取っていましたが、当期は前期より多くの",
        "連結子会社から配当金を受け取ったことにより、経常利益、当期純利益が増加しました。",
        "", "なお、期末配当予想は変更ありません。",
    ]
    lines = [dict(text=value, page=1, line=index) for index, value in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title="個別業績と前期実績との差異")
    assert len(result) == 1
    assert result[0]["text"].endswith("連結子会社から配当金を受け取ったことにより、経常利益、当期純利益が増加しました。")
    assert "期末配当予想" not in result[0]["text"]


def test_separate_individual_publication_reason_after_forecast_table():
    texts = [
        "１．通期連結業績予想の修正", "＜修正の理由＞", "売上高は前回予想を下回ります。",
        "２．通期個別業績予想の公表", "売上高 営業利益 経常利益", "＜新たに公表することとなった理由＞",
        "ロイヤリティーの減収により前期実績を下回る見通しです。",
        "３．配当予想の修正", "＜修正の理由＞", "配当性向を踏まえて増配します。",
    ]
    lines = [dict(text=value, page=1, line=index) for index, value in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title="通期連結業績予想の修正、通期個別業績予想の公表及び配当予想の修正")
    assert [section['text'] for section in result] == [
        "売上高は前回予想を下回ります。", "ロイヤリティーの減収により前期実績を下回る見通しです。",
    ]
    assert '通期個別業績予想の公表' in result[1]['context'][0]


def test_two_basis_tables_before_two_reasons_keep_matching_context_and_stop_at_next_subsection():
    texts = [
        '2027年４月期中間期連結業績予想数値の修正', '売上高 営業利益',
        '2027年４月期中間期個別業績予想数値の修正', '売上高 営業利益',
        '(１)中間期連結業績予想の修正理由', '連結売上高が上振れました。',
        '(２)中間期個別業績予想の修正理由', '個別売上高も上振れました。',
        '(３)通期の連結及び個別業績予想について', '通期予想は変更しておりません。',
    ]
    lines = [dict(text=value, page=1, line=index) for index, value in enumerate(texts, 1)]
    result = extract_reason_sections(lines, title='業績予想の修正')
    assert [s['text'] for s in result] == ['連結売上高が上振れました。', '個別売上高も上振れました。']
    assert '連結' in result[0]['context'][0] and '個別' not in result[0]['context'][0]
    assert '個別' in result[1]['context'][0] and '連結' not in result[1]['context'][0]


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


def test_reason_final_line_before_blank_and_note_is_preserved():
    # Native lines from the live 2737 PDF: a caution follows the forecast
    # reason after a blank line, and an old lookahead removed its last line.
    lines = [
        {"text": "修正の理由", "page": 1, "line": 1},
        {"text": "生成AI関連製品の需要拡大が業績を押し上げました。", "page": 1, "line": 2},
        {"text": "業績予想を修正するものであります。", "page": 1, "line": 3},
        {"text": "", "page": 1, "line": 4},
        {"text": "（注）業績予想は現時点で入手可能な情報に基づきます。", "page": 1, "line": 5},
        {"text": "実際の業績は異なる場合があります。", "page": 1, "line": 6},
    ]
    result = extract_reason_sections(lines, title="業績予想の修正")
    assert result[0]["text"] == ("生成AI関連製品の需要拡大が業績を押し上げました。\n"
                                 "業績予想を修正するものであります。")


def test_centered_low_footer_and_unheaded_projection_note_are_excluded(tmp_path):
    import pymupdf
    from src.events.forecast_reason import read_pdf_lines

    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((80, 730), "2. Reason: revenue rose.")
    page.insert_text((295, 775), "1")
    path = tmp_path / "footer.pdf"
    pdf.save(path)
    pdf.close()
    assert all(row["text"].strip() != "1" for row in read_pdf_lines(str(path)))

    lines = [
        {"text": "２．修正の理由", "page": 1, "line": 1},
        {"text": "売上減少を織り込み修正しました。", "page": 1, "line": 2},
        {"text": "", "page": 1, "line": 3},
        {"text": "本業績予想については、現時点で入手可能な情報に基づきます。", "page": 1, "line": 4},
        {"text": "実際の業績は異なる場合があります。", "page": 1, "line": 5},
    ]
    assert extract_reason_sections(lines)[0]["text"] == "売上減少を織り込み修正しました。"


def test_forecast_overview_keeps_reason_across_pages_and_stops_at_note():
    lines = [
        {"text": "３．連結業績予想の概要について", "page": 1, "line": 1},
        {"text": "連結対象の業績を織り込みました。", "page": 1, "line": 2},
        {"text": "売上高は前回予想より減少しました。", "page": 2, "line": 1},
        {"text": "※予想は現時点で入手可能な情報に基づきます。", "page": 2, "line": 2},
    ]
    result = extract_reason_sections(lines, title="連結業績予想の公表に関するお知らせ")
    assert len(result) == 1
    assert result[0]["text"] == "連結対象の業績を織り込みました。\n売上高は前回予想より減少しました。"
    assert result[0]["page_end"] == 2


def test_dividend_and_outlook_notes_after_reason_are_excluded():
    lines = [
        {"text": "（２）修正の理由", "page": 1, "line": 1},
        {"text": "需要拡大により通期予想を上方修正しました。", "page": 1, "line": 2},
        {"text": "", "page": 1, "line": 3},
        {"text": "なお、配当予想については変更いたしません。", "page": 1, "line": 4},
        {"text": "【見通しに関する留意事項】", "page": 1, "line": 5},
    ]
    for note in ("なお、配当予想については変更いたしません。",
                 "なお、今回の業績予想修正に伴う配当予想の修正はありません。",
                 "また、期末配当予想は据え置きます。"):
        lines[3]["text"] = note
        assert extract_reason_sections(lines, title="業績予想の修正と配当予想")[0]["text"] == lines[1]["text"]

    lines[3]["text"] = "また、受取配当金の増加も経常利益を押し上げました。"
    lines[4]["text"] = "以上の状況から業績予想を修正いたします。"
    assert extract_reason_sections(lines, title="業績予想の修正")[0]["text"].endswith(lines[4]["text"])


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


def test_alternate_reason_headings_require_forecast_context():
    cases = [
        ("２．運用状況及び１口当たり分配金の予想の修正について", "２. 開示の理由"),
        ("１．2026年３月期通期の個別業績予想", "業績予想の理由"),
        ("2026年３月期通期連結業績予想値と実績値との差異", "［予想との差異要因］"),
        ("2026年12月期通期連結業績予想の修正", "（理由）"),
        ("2026年３月期通期連結業績予想値と実績値との差異", "理由"),
    ]
    for parent, heading in cases:
        lines = [
            {"text": parent, "page": 1, "line": 1},
            {"text": heading, "page": 1, "line": 2},
            {"text": "需要の増加により予想との差異が生じました。", "page": 1, "line": 3},
            {"text": "以上", "page": 1, "line": 4},
        ]
        sections = extract_reason_sections(lines, title="業績予想と実績の差異")
        assert len(sections) == 1
        assert sections[0]["text"] == lines[2]["text"]
        lines[0]["text"] = "１．配当予想の修正について"
        assert extract_reason_sections(lines, title="業績予想と配当予想の修正") == []


def test_unlabeled_dividend_aside_does_not_cut_later_forecast_prose():
    lines = [
        {"text": "２．修正の理由", "page": 1, "line": 1},
        {"text": "売上高の減少により通期予想を修正いたします。", "page": 1, "line": 2},
        {"text": "", "page": 1, "line": 3},
        {"text": "なお、配当予想は据え置きます。", "page": 1, "line": 4},
        {"text": "", "page": 1, "line": 5},
        {"text": "翌期の業績予想は精査して公表します。", "page": 1, "line": 6},
        {"text": "以上", "page": 1, "line": 7},
    ]
    reason = extract_reason_sections(lines)[0]['text']
    assert reason.endswith(lines[5]['text'])
