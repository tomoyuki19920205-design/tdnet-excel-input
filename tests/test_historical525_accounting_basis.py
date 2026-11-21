import json,sqlite3
import pytest
from tools.sync_financials import _QUERY_BASE

def read(rows):
 c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
 c.execute('create table jquants_financials_normalized(local_code text,disclosed_date text,current_fiscal_year_end_date text,type_of_current_period text,type_of_document text,net_sales integer,gross_profit integer,operating_profit integer,raw_json text)')
 for date,kind,gp,op in rows:
  c.execute('insert into jquants_financials_normalized values(?,?,?,?,?,?,?,?,?)',('95520',date,'2025-09-30','FY',kind,16602000000,gp,op,json.dumps({'OP':str(op)if op is not None else'','OdP':'4970000000'if kind.endswith('_JP')else''})))
 return dict(c.execute(_QUERY_BASE.format(where_clause='')).fetchone())

@pytest.mark.parametrize('new_type',['FYFinancialStatements_Consolidated_IFRS','FYFinancialStatements_NonConsolidated_JP'])
def test_no_fallback_across_accounting_basis_or_consolidation(new_type):
 r=read([('2025-10-30','FYFinancialStatements_Consolidated_JP',10120114000,4964000000),('2026-01-05',new_type,None,None)])
 assert r['gross_profit'] is None
 assert r['operating_profit'] is None
 if 'ordinary_profit'in r:assert r['ordinary_profit']is None

def test_sparse_same_basis_correction_retains_previous_fact():
 kind='FYFinancialStatements_Consolidated_IFRS'
 r=read([('2026-01-05',kind,10030209000,4778000000),('2026-02-13',kind,None,None)])
 assert r['gross_profit']==10030209000
 assert r['operating_profit']==4778000000

def test_later_forecast_notice_does_not_change_actual_basis():
 kind='FYFinancialStatements_Consolidated_IFRS'
 r=read([('2026-01-05',kind,10030209000,4778000000),('2026-02-13','EarnForecastRevision',None,None)])
 assert r['gross_profit']==10030209000
 assert r['operating_profit']==4778000000
