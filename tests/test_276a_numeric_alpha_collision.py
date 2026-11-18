"""Regression: 27600 (Tokyo Electron Device) must never become 276A (CCReB)."""
import json
import sqlite3
import pytest
from src.common_ticker import normalize_ticker, JQUANTS_ALPHA_MAP
from lib.pipeline.canonical_writer import expand_financials_rows
from tools.sync_financials import read_sqlite
from tools.sqlite_to_supabase import _build_financials_rows_from_tdnet

@pytest.mark.parametrize('raw,expected',[('27600','2760'),('276A0','276A'),('2760','2760'),('276A','276A')])
def test_276a_exact_security_identity(raw,expected):
    assert normalize_ticker(raw)==expected

@pytest.mark.parametrize('raw,alpha',sorted(JQUANTS_ALPHA_MAP.items()))
def test_every_retired_alpha_mapping_remains_separate(raw,alpha):
    assert normalize_ticker(raw)==raw[:4]
    assert normalize_ticker(alpha+'0')==alpha
    assert normalize_ticker(raw)!=normalize_ticker(alpha+'0')

def test_jquants_sync_keeps_latest_2760_q1_out_of_276a(tmp_path):
    db=tmp_path/'jquants.db'
    con=sqlite3.connect(db)
    con.execute('''CREATE TABLE jquants_financials_normalized(local_code TEXT,disclosed_date TEXT,current_fiscal_year_end_date TEXT,type_of_current_period TEXT,type_of_document TEXT,net_sales INTEGER,gross_profit INTEGER,operating_profit INTEGER,profit_before_tax INTEGER,raw_json TEXT,fetched_at TEXT)''')
    for code,period,q,date,sales,gp,op in [('27600','2027-03-31','1Q','2026-07-29',60322,9306,4066),('276A0','2026-08-31','FY','2026-10-15',7020,1816.416,1144)]:
        raw={'Code':code,'CurFYEn':period,'CurPerType':q,'Sales':str(int(sales*1000000)),'OP':str(int(op*1000000))}
        con.execute('INSERT INTO jquants_financials_normalized VALUES(?,?,?,?,?,?,?,?,?,?,?)',(code,date,period,q,q+'FinancialStatements_Consolidated_JP',int(sales*1000000),int(gp*1000000),int(op*1000000),None,json.dumps(raw),date))
    con.commit();con.close()
    rows,_=read_sqlite(str(db))
    by_code={r['ticker']:r for r in rows}
    assert set(by_code)=={'2760','276A'}
    assert (by_code['276A']['period'],by_code['276A']['quarter'],by_code['276A']['sales'],by_code['276A']['operating_profit'])==('2026-08-31','FY',7020,1144)
    assert by_code['2760']['sales']==60322
    canonical=[]
    for row in rows:
        expanded,skipped=expand_financials_rows(ticker=row['ticker'],period=row['period'],quarter=row['quarter'],metrics_dict={'sales':row['sales'],'operating_profit':row['operating_profit']},source='jquants')
        assert skipped==0
        canonical.extend(expanded)
    assert len({r['source_row_key'] for r in canonical})==4
    assert all(r['period']=='2026-08-31' for r in canonical if r['ticker']=='276A')

def test_tdnet_bridge_keeps_numeric_and_alpha_issuers_separate():
    rows=[]
    for code,period,q,sales,op in [('27600','2027-03-31','1Q',60322,4066),('276A0','2026-08-31','FY',7020,1144)]:
        rows.append({'id':len(rows)+1,'company_code':code,'fiscal_year_end':period,'quarter':q,'sales':sales*1000000,'gross_profit':None,'operating_profit':op*1000000,'unit':'円','created_at':'2026-10-16','updated_at':'2026-10-16','source_doc_id':'fixture-'+code,'source_url':'https://www.release.tdnet.info/inbs/fixture.pdf','zip_hash':None,'parser_version':'v2'})
    built={r['ticker']:r for r in _build_financials_rows_from_tdnet(rows)}
    assert set(built)=={'2760','276A'}
    assert (built['276A']['period'],built['276A']['sales'],built['276A']['operating_profit'])==('2026-08-31',7020,1144)
    assert built['2760']['sales']==60322
