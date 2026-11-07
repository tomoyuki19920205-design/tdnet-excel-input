from pathlib import Path
import json
import pytest
from bs4 import BeautifulSoup
from src.segment.semantic_facts import disclosed_metric, monetary_fact
from src.segment.profit_basis import segment_flags, persisted_flags, profit_basis
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
from src.migration.migration_db import MigrationDB

FIX = Path(__file__).parent / 'fixtures' / 'segment_partials'

def test_real_custom_adjusted_profit_and_reverse_missing_sales():
    for source in json.loads((FIX / 'sources.json').read_text(encoding='utf-8')):
        period = '2027-05-31' if source['ticker'] == '4443' else '2027-03-31'
        result = extract_segments_from_xbrl_zip_detailed(str(FIX / (source['doc'] + '.zip')), period=period, quarter='1Q')
        assert result.status == 'success_with_rows'
        assert len(result.segments) == 6
        actual = {(r.period, r.raw_segment_name): r for r in result.segments}
        for expected in source['rows']:
            row = actual[expected['period'], expected['name']]
            assert (row.sales, row.profit) == (expected['sales'], expected['profit'])
            flags = segment_flags(row.raw_json)
            assert flags['profit_basis'] == expected['flags']['profit_basis']
            assert flags['sales_basis'] == expected['flags']['sales_basis']
            assert flags['is_consolidated'] == expected['is_consolidated']
            assert flags['period_start'] == expected['flags']['period_start']

@pytest.mark.parametrize('label,metric', [('セグメント利益又は損失（△）','profit'),('完 成 工 事 高','sales'),('利益率',None),('減損損失',None),('金融収益',None),('一株当たり利益',None),('調整額',None)])
def test_label_requires_disclosed_measure(label, metric):
    soup = BeautifulSoup(f'<table><tr><td>{label}</td><td><ix:nonfraction unitref="JPY" scale="3">123</ix:nonfraction></td></tr></table>', 'html.parser')
    assert disclosed_metric(soup.find('ix:nonfraction'))[0] == metric

@pytest.mark.parametrize('raw,scale,sign,expected',[('89191',3,'-',-89.191),('0',6,None,0),('248684',3,None,248.684),('1.25',6,None,1.25)])
def test_fact_precision_and_scale_are_preserved(raw, scale, sign, expected):
    soup = BeautifulSoup(f'<ix:nonfraction unitref="JPY" scale="{scale}" sign="{sign or ""}">{raw}</ix:nonfraction>', 'html.parser')
    assert monetary_fact(soup.find('ix:nonfraction'), soup)['million_value'] == expected

def test_non_currency_fact_cannot_become_yen():
    soup = BeautifulSoup('<xbrli:unit id="JPYPerShare"><xbrli:measure>iso4217:JPY</xbrli:measure><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit><ix:nonfraction unitref="JPYPerShare" scale="0">123</ix:nonfraction>', 'html.parser')
    assert monetary_fact(soup.find('ix:nonfraction'), soup)['currency'] is None

def test_metadata_survives_same_filing_partial_arrivals_and_rejects_different_basis(tmp_path):
    db = MigrationDB(str(tmp_path / 'segments.db'))
    base = dict(company_code='4443', fiscal_year_end='2027-05-31', quarter='1Q', segment_name='事業', segment_order=1, data_source='backfill_xbrl', tdnet_doc_id='same', extractor_route='xbrl', source_doc_type='earnings_summary', disclosure_date='2026-10-09', row_type='segment')
    meta = {'profit_basis':'adjusted_operating_profit','sales_basis':'including_intersegment','currency':'JPY','is_consolidated':True}
    a = db.upsert_segment_provenance_aware(**base, segment_sales=0, segment_metadata=meta)
    assert a.accepted
    b = db.upsert_segment_provenance_aware(**base, segment_profit=-1.25, segment_metadata=meta)
    assert b.accepted
    row = db._conn.execute('select segment_sales, segment_profit, segment_metadata from segment_financials').fetchone()
    assert row[:2] == (0,-1.25)
    assert persisted_flags({'segment_metadata':row[2]}) == meta
    assert db.upsert_segment_provenance_aware(**base, segment_profit=-1.25, segment_metadata=meta).status == 'no_change'
    conflict = db.upsert_segment_provenance_aware(**base, segment_profit=100, segment_metadata={**meta,'profit_basis':'operating_profit'})
    assert not conflict.accepted
    assert db._conn.execute('select segment_profit from segment_financials').fetchone()[0] == -1.25
    db.close()

def test_known_and_unknown_basis_do_not_relabel_as_operating_profit():
    assert profit_basis('adjusted_operating_profit') == 'adjusted_operating_profit'
    assert profit_basis('セグメント利益（基準未確定）') == 'company_defined_unknown'
