from dataclasses import replace
import pytest
from lib.pipeline.financial_integrity import duration_quarter, normalize_amount
from lib.pipeline.canonical_writer import expand_financials_rows
from lib.pipeline.forecast_sync import ForecastDTO, select_latest_forecasts, expand_forecast_rows
from src.extractor import _detect_quarter_from_context
from src.events.forecast_eps_table import forecast_eps_pair
from src.events.forecast_extractor import _return_with_eps_log
from src.events.forecast_models import ForecastRevisionEvent

@pytest.mark.parametrize('end,quarter', [('2024-01-31','1Q'),('2024-04-30','2Q'),('2024-07-31','3Q'),('2024-10-31','FY')])
def test_duration_and_actual_rows(end,quarter):
    assert duration_quarter('2023-11-01',end)==quarter
    meta={'CurrentYearDuration':{'start':'2023-11-01','end':end}}
    assert _detect_quarter_from_context('CurrentYearDuration',meta)==('4Q' if quarter=='FY' else quarter)
    rows,_=expand_financials_rows(ticker='1234',period='2024-10-31',quarter=quarter,
        metrics_dict={'sales':36000000000,'operating_profit':2300000000},source='summary_xbrl',unit='JPY')
    assert {r['quarter'] for r in rows}=={quarter}
    assert rows[0]['value']==36000
    assert all(r['unit']=='millions_jpy' for r in rows)

def test_same_year_end_does_not_make_three_month_context_fy():
    assert duration_quarter('2024-08-01','2024-10-31',fiscal_end='2024-10-31')=='1Q'
    assert duration_quarter('2024-08-01','2024-10-31',fiscal_start='2023-11-01',fiscal_end='2024-10-31') is None

@pytest.mark.parametrize('unit,value', [('JPY',36000000000),('千円',36000000),('百万円',36000),('millions_jpy',36000)])
def test_normalize_once(unit,value):
    amount=normalize_amount(value,unit)
    assert amount==36000
    assert normalize_amount(amount,'millions_jpy')==36000
    assert normalize_amount(value,'unknown') is None

def test_missing_fy_never_fills_from_first_quarter():
    rows,_=expand_financials_rows(ticker='1234',period='2099-10-31',quarter='FY',
        metrics_dict={'sales':36000,'operating_profit':2300},source='pdf_table',unit='millions_jpy')
    assert rows==[]
    rows,_=expand_financials_rows(ticker='1234',period='2099-10-31',quarter='3Q',
        metrics_dict={'sales':100000,'operating_profit':None,'ordinary_profit':7000},source='pdf',unit='millions_jpy')
    assert {r['metric'] for r in rows}=={'sales','ordinary_profit'}

def dto(**kwargs):
    base=ForecastDTO('1234','2024-10-31','sales',150000,'2024-09-04T14:00:00+09:00','document-z',
        'tdnet_forecast',False,'specified_period','J_GAAP','earnings_guidance')
    return replace(base,**kwargs)

def test_latest_disclosure_revision_sequence_and_simultaneous_documents():
    old=dto(disclosure_datetime='2024-06-04',value=140000,correction_flag=True)
    guidance=dto()
    revision=dto(filing_id='document-a',value=160000,document_type='forecast_revision')
    for candidates in ([old,revision,guidance],[guidance,revision,old]):
        assert select_latest_forecasts(candidates)==[revision]
    second=replace(revision,revision_sequence=2,value=165000)
    assert select_latest_forecasts([second,revision])==[second]
    forecast=expand_forecast_rows([revision])[0]
    actual=expand_financials_rows(ticker='1234',period='2024-10-31',quarter='FY',metrics_dict={'sales':155000},source='pdf')[0][0]
    assert actual['source_row_key'] != forecast['source_row_key']

EPS_TABLE='''通期連結業績予想
売上高 営業利益 経常利益 親会社株主帰属純利益 1株当たり当期純利益
前回発表予想(A)
百万円 150,000 百万円 10,000 百万円 11,000 百万円 8,000 円銭 499.82
今回修正予想(B)
160,000 12,000 13,000 9,000 538.05
増減額
参考 前期実績 387.63
配当予想 今回修正予想 90円00銭
'''

def test_eps_never_uses_dividend_or_prior_actual():
    assert forecast_eps_pair(EPS_TABLE)==(499.82,538.05)
    result=_return_with_eps_log(ForecastRevisionEvent(revised_eps=90,latest_full_year_eps=387.63),EPS_TABLE)
    assert result.revised_eps==538.05 and result.latest_full_year_eps==538.05
    assert result.eps_validated
    assert forecast_eps_pair('1株当たり配当金\n今回修正予想 90円00銭') is None


def test_eps_header_may_be_interleaved_by_pdf_text_reading_order():
    text=EPS_TABLE.replace("売上高 営業利益 経常利益 親会社株主帰属純利益 1株当たり当期純利益", "1 株 当 た り\n売上高 営業利益 経常利益 帰属する\n当期純利益")
    assert forecast_eps_pair(text)==(499.82,538.05)

def test_realtime_revision_sync_uses_document_keys_and_validated_eps(monkeypatch):
    import json, sqlite3
    from lib.pipeline.forecast_sync import sync_document_forecasts
    from lib.pipeline import db
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE events (source_doc_id TEXT,ticker TEXT,event_type TEXT,title TEXT,disclosure_datetime TEXT,extracted_payload_json TEXT)')
    conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',('doc','1234','forecast_revision','業績予想修正','2024-09-04 14:00',json.dumps({
        'period_label':'2024年10月期','revised_sales':160000,'revised_op':12550,'revised_eps':538.05,'eps_validated':True})))
    captured=[]
    monkeypatch.setattr(db,'load_env',lambda:None)
    monkeypatch.setattr(db,'get_supabase_write_config',lambda:{'mock':True})
    monkeypatch.setattr(db,'supabase_upsert',lambda table,rows,**kwargs:captured.extend(rows) or {'written':len(rows),'errors':0})
    result=sync_document_forecasts(conn,['doc'])
    assert result['written']==3
    assert {r['source'] for r in captured}=={'tdnet_forecast'}
    assert {r['filing_id'] for r in captured}=={'doc'}
    assert next(r for r in captured if r['metric']=='eps')['unit']=='yen_per_share'
    assert sync_document_forecasts(conn,['unrelated'])['written']==0


def test_short_fiscal_year_requires_both_fiscal_boundaries():
    meta={'CurrentYearDuration':{'start':'2024-01-01','end':'2024-09-30',
        'fiscal_start':'2024-01-01','fiscal_end':'2024-09-30'}}
    assert _detect_quarter_from_context('CurrentYearDuration',meta)=='4Q'
