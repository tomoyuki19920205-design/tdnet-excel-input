import json
from pathlib import Path
from unittest.mock import patch
import pytest
from src.events.forecast_reason import extract_reason_sections, extract_forecast_reason
from src.events.forecast_models import ForecastRevisionEvent
from src.events.forecast_extractor import extract_forecast_revision
from src.events.event_pipeline import _forecast_to_event_record
from src.events.common_models import DocumentMeta
from tools.backfill_forecast_reasons import build_patch, digest

FIXTURES = json.loads((Path(__file__).parent/'fixtures/forecast_reason/october13.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('fixture', FIXTURES, ids=lambda f:f['ticker'])
def test_real_pdf_sections_and_unabridged_source_lines(fixture):
    sections = extract_reason_sections(fixture['lines'], title=fixture['title'])
    assert sections == fixture['expected']
    assert sections
    for section in sections:
        # Every nonblank body line is an original native PDF line, in source order.
        source = [r['text'] for r in fixture['lines'] if
                  (r['page'],r['line']) > (section['page_start'],section['line_start']) and
                  (r['page'],r['line']) <= (section['page_end'],section['line_end'])]
        assert section['text'].splitlines() == source[:len(section['text'].splitlines())]

def fixture(ticker): return next(f for f in FIXTURES if f['ticker']==ticker)['expected']

def test_long_buyback_issuer_reason_keeps_bullets_decimal_and_final_exchange_row():
    s=fixture('6432')[0]
    assert s['page_start']==1 and s['page_end']==2
    assert '以上により' in s['text'] and '② 連結業績予想' in s['text'] and '③ 為替レート' in s['text']
    assert '21.8％減' in s['text'] and '⚫' in s['text'] and '・ 米国関税の還付' in s['text']
    assert s['text'].endswith('人民元  23.13 円  22.30 円  21.20 円')
    assert '株主の皆様に対する利益配分' not in s['text']
    assert 'こと等により、\n売上高' in s['text']  # continuous paragraph despite different glyph boxes

def test_multiple_periods_and_bases_are_labeled_without_cross_contamination():
    sections=fixture('8247')
    assert len(sections)==4
    for i, s in enumerate(sections):
        assert ('連結業績について' if i<2 else '個別業績について') in s['context'][0]
        assert ('中間期' if i%2==0 else '通期業績予想の修正') in s['context'][1]
        assert '３．2027' not in s['text']
        assert s['text'].endswith('前回予想を下回りました。' if i%2==0 else '当初予想を修正いたします。')

def test_downward_and_interim_final_paragraphs():
    assert fixture('3536')[0]['text'].endswith('回業績予想と比較して親会社株主に帰属する当期純利益の減少が見込まれております。')
    assert '\n\n' in fixture('3536')[0]['text']
    assert '\n\nなお、' in fixture('8081')[0]['text']
    assert fixture('8081')[0]['text'].endswith('ました数値を変更しておりません。精査完了後に業績予想の修正が必要な際には改めて公表いたします。')

def test_dividend_only_and_missing_do_not_generate_text():
    rows=[dict(text=t,page=1,line=i) for i,t in enumerate(['1. 配当予想の修正','(2) 修正理由','配当だけの理由。','以上'])]
    assert extract_reason_sections(rows)==[]
    assert extract_forecast_reason('', 'https://example.com/original.pdf')['status']=='unavailable'

def test_text_source_does_not_substitute_for_the_original_pdf():
    with patch('src.events.forecast_extractor._extract_forecast_revision_metrics', return_value=ForecastRevisionEvent(revised_eps=0)):
        event=extract_forecast_revision('修正の理由\nHTMLの別文章。')
    assert event.revised_eps==0 and event.revision_reason['status']=='unavailable'
    assert event.to_dict()['revision_reason']['sections']==[]

def test_native_reason_is_saved_without_altering_fingerprint_or_summary():
    doc=DocumentMeta(doc_id='d',ticker='8081',company_name='会社',title='業績予想修正',doc_url='https://example.com/d.pdf')
    event=ForecastRevisionEvent(period_label='2027年3月期',revised_eps=62.78,subtype='upward')
    before=_forecast_to_event_record(doc,event)
    event.revision_reason=dict(version=1,status='available',source_url=doc.doc_url,sections=fixture('8081'))
    after=_forecast_to_event_record(doc,event)
    assert before.fingerprint==after.fingerprint and before.summary_text==after.summary_text
    assert json.loads(after.extracted_payload_json)['revision_reason']==event.revision_reason

def test_reason_repair_preserves_numeric_payload_and_rejects_concurrent_or_foreign_data():
    raw={'extracted':{'previous_eps':70.09,'revised_eps':29.15},'raw':{'title':'原文'}}
    before={'id':'n','dedupe_key':'key','event_type':'forecast','source_url':'https://example.com/n.pdf','raw_payload':raw}
    reason={'version':1,'status':'available','source_url':before['source_url'],'pdf_sha256':'a'*64,'sections':fixture('470A')}
    plan={**before,'raw_payload_before_sha256':digest(raw),'reason':reason}
    amended=build_patch(before,plan)['raw_payload']
    assert amended['extracted']['previous_eps']==70.09 and amended['extracted']['revised_eps']==29.15
    assert raw['extracted'].get('revision_reason') is None
    for invalid in ({**plan,'source_url':'https://example.com/foreign.pdf'},{**plan,'raw_payload_before_sha256':'changed'}):
        with pytest.raises(AssertionError):build_patch(before,invalid)
    filled={**before,'raw_payload':amended}
    replacement={**plan,'raw_payload_before_sha256':digest(amended)}
    with pytest.raises(AssertionError):build_patch(filled,replacement)
    replacement['expected_existing_reason_sha256']=digest(reason)
    with pytest.raises(AssertionError):build_patch(filled,replacement)
    reviewed={**replacement,'allow_replace_available':True}
    assert build_patch(filled,reviewed)['raw_payload']==amended
    unavailable={**reason,'status':'unavailable','sections':[]}
    stale={**before,'raw_payload':{'extracted':{**raw['extracted'],'revision_reason':unavailable},'raw':raw['raw']}}
    recovery={**plan,'raw_payload_before_sha256':digest(stale['raw_payload']),
              'expected_existing_reason_sha256':digest(unavailable)}
    assert build_patch(stale,recovery)['raw_payload']==amended
