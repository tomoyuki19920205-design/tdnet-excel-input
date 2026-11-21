from pathlib import Path
import pytest
from src.pl_actual_evidence import ActualPeriod
from lib.pipeline import actual_export_guard as guard


@pytest.mark.parametrize('prefix',['https://www.release.tdnet.info/inbs/',
    'https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/'])
def test_redirected_original_cannot_reexport_bad_period(tmp_path,monkeypatch,prefix):
    name='140120260501516201.pdf';(tmp_path/name).write_bytes(b'fixture')
    monkeypatch.setattr(guard,'_read_evidence',lambda *a:('2026年9月期 第2四半期決算短信','2551',ActualPeriod('2026-09-20','2Q','2025-09-21','2026-03-20'),{},''))
    row={'id':115792,'company_code':'2551','fiscal_year_end':'2026-09-30','quarter':'2Q','source_url':prefix+name}
    assert guard.assess_local_actual(row,tmp_path)==(False,'fiscal_end_conflict')
    row['fiscal_year_end']='2026-09-20'
    assert guard.assess_local_actual(row,tmp_path)[0]
    row['company_code']='25510'
    assert guard.assess_local_actual(row,tmp_path)[0]


@pytest.mark.parametrize('raw',['276A','276A0'])
def test_valid_alphanumeric_issuer_is_preserved(tmp_path,monkeypatch,raw):
    name='140120261015551313.pdf';(tmp_path/name).write_bytes(b'fixture')
    monkeypatch.setattr(guard,'_read_evidence',lambda *a:('2026年8月期 決算短信','276A',ActualPeriod('2026-08-31','FY','2025-09-01','2026-08-31'),{},''))
    assert guard.assess_local_actual({'company_code':raw,'fiscal_year_end':'2026-08-31','quarter':'FY','source_url':'https://www.release.tdnet.info/inbs/'+name},tmp_path)[0]


def test_incomplete_original_does_not_guess_a_repair(tmp_path):
    row={'id':1,'company_code':'1234','fiscal_year_end':'2026-03-20','quarter':'2Q'}
    assert guard.assess_local_actual(row,tmp_path)==(True,'unverified_no_official_pdf_reference')


def test_standalone_fourth_quarter_is_not_exported_as_full_year(tmp_path,monkeypatch):
    name='140120261015551313.pdf';(tmp_path/name).write_bytes(b'fixture')
    monkeypatch.setattr(guard,'_read_evidence',lambda *a:('2026年8月期 第4四半期決算短信','1234',None,{},''))
    row={'company_code':'1234','fiscal_year_end':'2026-08-31','quarter':'4Q','source_url':'https://www.release.tdnet.info/inbs/'+name}
    assert guard.assess_local_actual(row,tmp_path)==(False,'standalone_quarter_not_cumulative')
