import json
import hashlib
from pathlib import Path
from unittest.mock import patch
import pytest
from src.events.forecast_reason import (extract_reason_sections, extract_forecast_reason,
                                        require_pdf_reader, ForecastReasonEnvironmentError)
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


def test_actual_9310_bullet_dividend_chapter_does_not_leak_into_forecast_reason():
    actual=json.loads((Path(__file__).parent/'fixtures/forecast_reason/bullet_dividend_9310.json').read_text(encoding='utf-8'))
    sections=extract_reason_sections(actual['lines'],title=actual['title'])
    assert len(sections)==1
    assert sections[0]['heading']=='２．修正の理由'
    assert sections[0]['text'].endswith('以上の理由から通期の連結業績予想を上記のとおり修正いたします。')
    assert '配当性向40％' not in sections[0]['text']


def test_actual_7132_reason_after_dividend_table_ends_before_dividend_rationale():
    actual=json.loads((Path(__file__).parent/'fixtures/forecast_reason/combined_scope_7132.json').read_text(encoding='utf-8'))
    sections=extract_reason_sections(actual['lines'],title=actual['title'])
    assert len(sections)==1
    assert sections[0]['text']==actual['expected'][0]['text']
    assert sections[0]['page_start']==sections[0]['page_end']==2
    assert '株主に対する利益還元' not in sections[0]['text']


def test_actual_4246_dividend_reason_after_forecast_is_not_added():
    actual=json.loads((Path(__file__).parent/'fixtures/forecast_reason/dividend_scope_4246.json').read_text(encoding='utf-8'))
    sections=extract_reason_sections(actual['lines'],title=actual['title'])
    assert len(sections)==1
    assert sections[0]['text'].endswith('修正しております。')
    assert '１株につき57円' not in sections[0]['text']


def test_actual_6914_mixed_reason_stops_at_dividend_subheading():
    actual=json.loads((Path(__file__).parent/'fixtures/forecast_reason/combined_scope_6914.json').read_text(encoding='utf-8'))
    sections=extract_reason_sections(actual['lines'],title=actual['title'])
    assert len(sections)==1
    assert '【第2 四半期（中間期）連結業績予想数値の修正】' in sections[0]['text']
    assert '【通期連結業績予想数値の修正】' in sections[0]['text']
    assert sections[0]['text'].endswith('おります。')
    assert '【配当予想の修正】' not in sections[0]['text']


@pytest.mark.parametrize('ticker,tail', [
    ('3710','第３四半期までの実績及び経常利益の予想数値等を踏まえて算定しております。'),
    ('6522','投入を推進し、中長期的な企業価値の向上に努めてまいります。'),
    ('4073','以上のことから、各利益に通期業績予想と実績値との差異が生じることとなりました。'),
    ('6324','円、１中国元 22.52 円を前提としております。'),
    ('5721','用の詳細につきましては、「Ⅰ．営業外費用の計上」に記載のとおりであります。'),
    ('312A','今後、業績予想の算定が可能となった時点で速やかにお知らせいたします。'),
])
def test_actual_unrecognized_heading_variants_keep_the_complete_original(ticker,tail):
    cases=json.loads((Path(__file__).parent/'fixtures/forecast_reason/unrecognized_headings_20261019.json').read_text(encoding='utf-8'))
    actual=next(c for c in cases if c['ticker']==ticker)
    sections=extract_reason_sections(actual['lines'],title=actual['title'])
    assert len(sections)==1
    assert sections[0]['text'].endswith(tail)
    if ticker=='6324': assert '注意事項' not in sections[0]['text']
    if ticker=='3710': assert '配当政策' not in sections[0]['text']
    if ticker=='6522': assert sections[0]['page_end']==2

def test_downward_and_interim_final_paragraphs():
    assert fixture('3536')[0]['text'].endswith('回業績予想と比較して親会社株主に帰属する当期純利益の減少が見込まれております。')
    assert '\n\n' in fixture('3536')[0]['text']
    assert '\n\nなお、' in fixture('8081')[0]['text']
    assert fixture('8081')[0]['text'].endswith('ました数値を変更しておりません。精査完了後に業績予想の修正が必要な際には改めて公表いたします。')

def test_dividend_only_and_missing_do_not_generate_text():
    rows=[dict(text=t,page=1,line=i) for i,t in enumerate(['1. 配当予想の修正','(2) 修正理由','配当だけの理由。','以上'])]
    assert extract_reason_sections(rows)==[]
    assert extract_forecast_reason('', 'https://example.com/original.pdf')['status']=='source_unavailable'


def test_numbered_reason_with_trailing_parenthetical_is_not_unwrapped():
    rows=[dict(text=t,page=1,line=i) for i,t in enumerate([
        '（2）連結業績予想修正の理由（のれん等の減損損失の計上）',
        '事業計画を見直した結果、損失を計上する見込みとなりました。',
        '以 上'], 1)]
    sections=extract_reason_sections(rows)
    assert len(sections)==1
    assert sections[0]['text']=='事業計画を見直した結果、損失を計上する見込みとなりました。'


def test_reason_etc_heading_keeps_prose_and_stops_at_next_section():
    rows=[dict(text=t,page=1,line=i) for i,t in enumerate([
        '（２）修正の理由等',
        '繰延税金資産を計上する見込みとなりました。',
        '３．配当予想の修正',
        '配当を増額します。'], 1)]
    sections=extract_reason_sections(rows)
    assert len(sections)==1
    assert sections[0]['text']=='繰延税金資産を計上する見込みとなりました。'


def test_parenthesized_next_subsection_stops_before_forecast_table():
    rows=[dict(text=t,page=1,line=i) for i,t in enumerate([
        '（２）修正の理由等',
        '各段階利益が公表数値を上回る見通しであります。',
        '（３）各セグメントにおける営業収益及び営業利益の見通し',
        '営業収益　営業利益　償却前営業利益'], 1)]
    sections=extract_reason_sections(rows)
    assert len(sections)==1
    assert sections[0]['text']=='各段階利益が公表数値を上回る見通しであります。'


@pytest.mark.parametrize('case', json.loads((Path(__file__).parent/'fixtures/forecast_reason/heading_variants_20261019.json').read_text(encoding='utf-8')),
                         ids=lambda case: case['ticker'])
def test_actual_numbered_and_extended_reason_headings(case):
    sections=extract_reason_sections(case['lines'],title=case['title'])
    assert sections==case['expected']
    if case['ticker']=='4307':
        assert sections[0]['page_end']==2
        assert sections[0]['text'].endswith('の資金生成単位として減損テストを実施しています。')
    elif case['ticker']=='7916':
        assert sections[0]['text'].endswith('前回予想を上回りました。')
    else:
        assert sections[0]['text'].endswith('公表数値を上回る見通しであります。')
        assert '（３）各セグメント' not in sections[0]['text']


def test_actual_5563_publication_reason_is_forecast_only():
    case=json.loads((Path(__file__).parent/'fixtures/forecast_reason/publication_reason_5563.json').read_text(encoding='utf-8'))
    sections=extract_reason_sections(case['lines'],title=case['title'])
    assert sections==case['expected']
    assert sections[0]['text'].endswith('1 米ドル157 円を前提としています。')
    assert '株主還元方針' not in sections[0]['text']


def test_missing_pdf_reader_is_environment_failure_not_absent_reason(monkeypatch, tmp_path):
    import sys
    pdf = tmp_path / 'original.pdf'
    pdf.write_bytes(b'%PDF-1.4')
    monkeypatch.setitem(sys.modules, 'pymupdf', None)
    with pytest.raises(ForecastReasonEnvironmentError, match='forecast_reason_pdf_reader_unavailable'):
        extract_forecast_reason(str(pdf), 'https://example.com/original.pdf')


@pytest.mark.parametrize('module', ['scheduler_realtime', 'scheduler_reconcile', 'scheduler_nightly'])
def test_scheduled_jobs_fail_before_processing_when_pdf_reader_missing(monkeypatch, module):
    import importlib
    import sys
    scheduler = importlib.import_module('tools.' + module)
    monkeypatch.setattr(sys, 'argv', [module + '.py', '--dry-run'])
    def fail():
        raise ForecastReasonEnvironmentError('forecast_reason_pdf_reader_unavailable')
    monkeypatch.setattr('src.events.forecast_reason.require_pdf_reader', fail)
    with pytest.raises(ForecastReasonEnvironmentError, match='forecast_reason_pdf_reader_unavailable'):
        scheduler.main()

def test_text_source_does_not_substitute_for_the_original_pdf():
    with patch('src.events.forecast_extractor._extract_forecast_revision_metrics', return_value=ForecastRevisionEvent(revised_eps=0)):
        event=extract_forecast_revision('修正の理由\nHTMLの別文章。')
    assert event.revised_eps==0 and event.revision_reason['status']=='source_unavailable'
    assert event.to_dict()['revision_reason']['sections']==[]

def test_native_reason_is_saved_without_altering_fingerprint_or_summary():
    doc=DocumentMeta(doc_id='d',ticker='8081',company_name='会社',title='業績予想修正',doc_url='https://example.com/d.pdf')
    event=ForecastRevisionEvent(period_label='2027年3月期',revised_eps=62.78,subtype='upward')
    before=_forecast_to_event_record(doc,event)
    event.revision_reason=dict(version=1,status='available',source_url=doc.doc_url,sections=fixture('8081'))
    after=_forecast_to_event_record(doc,event)
    assert before.fingerprint==after.fingerprint and before.summary_text==after.summary_text
    assert json.loads(after.extracted_payload_json)['revision_reason']==event.revision_reason


@pytest.mark.parametrize('name,title,expected_sha', [
    ('live_4813_difference_1.pdf', '営業外収益、営業外費用及び特別損失の計上、並びに2026年１月期通期（連結・個別）業績の前期実績値との差異に関するお知らせ', '04b6006b9f82bf75f889d951ba82809369fb5ee490df7308d42405a214e9a8e5'),
    ('live_4813_difference_2.pdf', '2027年１月期中間連結会計期間の業績予想値と実績値との差異に関するお知らせ', 'dcf5fc37ef94ad8ad278e85f7b99e8e58ccdd64cada1870b343530b46bdede3d'),
])
def test_after_table_difference_matches_reviewed_full_original(name,title,expected_sha):
    pdf=Path(__file__).parent/'fixtures/forecast_reason'/name
    result=extract_forecast_reason(str(pdf),'https://example.com/'+name,title=title)
    assert result['status']=='available'
    assert result['method']=='native_pdf_difference_after_table_v2'
    full='\n\n'.join(section['text'] for section in result['sections'])
    assert hashlib.sha256(full.encode()).hexdigest()==expected_sha
    assert all(s['text_sha256']==hashlib.sha256(s['text'].encode()).hexdigest() for s in result['sections'])


def test_multiple_corrections_are_matched_to_their_own_locations():
    base=Path(__file__).parent/'fixtures/forecast_reason'
    first=extract_forecast_reason(str(base/'live_6574_correction_1.pdf'),
                                  'https://example.com/first.pdf',title='（訂正）業績予想と実績値との差異に関するお知らせ')
    assert first['method']=='native_pdf_corrected_locations_v2'
    assert len(first['sections'])==2
    assert [s['page_start'] for s in first['sections']]==[8,8]
    assert 'ネイル事業' in first['sections'][0]['heading']
    assert 'コンサルティング事業' in first['sections'][1]['heading']
    assert '前回発表予想2,390' not in '\n'.join(s['text'] for s in first['sections'])
    assert '1,738 百万円' in first['sections'][1]['text']
    individual=extract_forecast_reason(str(base/'live_6574_individual_correction.pdf'),
                                       'https://example.com/individual.pdf',title='（訂正）個別業績の差異に関するお知らせ')
    assert individual['method']=='native_pdf_corrected_locations_v2'
    assert len(individual['sections'])==5
    assert [s['page_start'] for s in individual['sections']]==[3,4,4,5,6]
    assert '営業外費用' in individual['sections'][0]['text']
    assert '3,990 百万円悪化' in individual['sections'][2]['text']
    assert '訂正前' not in '\n'.join(s['text'] for s in individual['sections'])
    full=extract_forecast_reason(str(base/'live_6574_correction_2.pdf'),
                                 'https://example.com/full.pdf',title='（再訂正）業績予想と実績値との差異に関するお知らせ')
    assert full['method']=='native_pdf_corrected_fulltext_v2'
    assert len(full['sections'])==1 and full['sections'][0]['page_start']==6
    assert full['sections'][0]['text'].endswith('水準で着地しております。')


def test_attached_subsidiary_note_is_read_only_when_it_exists():
    base=Path(__file__).parent/'fixtures/forecast_reason'
    enex=extract_forecast_reason(str(base/'live_8001_enex_attached.pdf'),
        'https://example.com/enex.pdf', title='当社子会社（伊藤忠エネクス株式会社）の個別業績の前期実績との差異に関するお知らせ')
    assert enex['method']=='native_pdf_attached_individual_note_v2'
    assert enex['sections'][0]['page_start']==3
    assert enex['sections'][0]['text'].strip()=='子会社からの配当金増加により、当期純利益は前年度実績を上回りました。'
    prima=extract_forecast_reason(str(base/'live_8001_prima_attached.pdf'),
        'https://example.com/prima.pdf', title='当社子会社（プリマハム株式会社）の個別業績の前期実績との差異に関するお知らせ')
    assert prima['status']=='extraction_failed' and prima['sections']==[]

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
    for status in ('extraction_failed','scope_unverified','source_unavailable'):
        classified={**unavailable,'status':status}
        classified_row={**before,'raw_payload':{'extracted':{**raw['extracted'],'revision_reason':classified},'raw':raw['raw']}}
        classified_plan={**plan,'raw_payload_before_sha256':digest(classified_row['raw_payload']),
                         'expected_existing_reason_sha256':digest(classified)}
        assert build_patch(classified_row,classified_plan)['raw_payload']==amended
