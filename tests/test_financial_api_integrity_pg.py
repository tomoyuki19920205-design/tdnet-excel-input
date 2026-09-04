"""Run the exact API migration against connection-local temporary fixtures."""
import os
from pathlib import Path
import pytest

def test_actual_forecast_api_contract_in_temporary_schema():
    url=os.environ.get('FINANCIAL_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('FINANCIAL_TEST_POSTGRES_URL is required for temporary PostgreSQL fixture')
    import psycopg2
    conn=psycopg2.connect(url,connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='20s'")
            cur.execute('CREATE TEMP TABLE canonical_financials (LIKE public.canonical_financials INCLUDING ALL) ON COMMIT DROP')
            cur.execute('SET LOCAL search_path=pg_temp')
            migration=(Path(__file__).resolve().parents[1]/'migrations/017_financial_period_forecast_integrity.sql').read_text(encoding='utf-8')
            cur.execute(migration.replace('CREATE OR REPLACE VIEW','CREATE OR REPLACE TEMP VIEW'))
            def add(key,quarter,value,source='summary_xbrl',disclosed='2024-11-01',metric='sales',sequence=0,doc=None,unit='millions_jpy'):
                cur.execute('''INSERT INTO canonical_financials
                    (ticker,period,quarter,metric,value,unit,source,source_priority,source_row_key,
                     disclosure_datetime,created_at,updated_at,revision_sequence,document_type,recency_key)
                    VALUES ('1234','2024-10-31',%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s)''',
                    (quarter,metric,value,unit,source,key,disclosed,disclosed,disclosed,sequence,doc,key))
            add('q1','1Q',36000)
            add('q2','2Q',76000)
            add('q3','3Q',120000)
            add('bad-copy','FY',36000)
            add('bad-scale','FY',36000000000)
            add('bad-future','FY',160000,disclosed='2024-09-04')
            add('old','FY',150000,'jquants_forecast_fy','2024-06-04')
            add('guidance','FY',155000,'tdnet_forecast','2024-09-04',doc='earnings_guidance')
            add('revision','FY',160000,'tdnet_forecast','2024-09-04',doc='forecast_revision')
            add('op','FY',12550,'tdnet_forecast','2024-09-04',metric='operating_profit',doc='forecast_revision')
            cur.execute('SELECT quarter,sales FROM api_latest_financials_canonical ORDER BY quarter')
            assert cur.fetchall()==[('1Q',36000),('2Q',76000),('3Q',120000)]
            cur.execute('SELECT sales,operating_profit,ordinary_profit FROM api_latest_financials_canonical_forecast')
            assert cur.fetchone()==(160000,12550,None)
            # A later-arriving old disclosure cannot overwrite a revision.
            add('late-arriving-old','FY',145000,'tdnet_forecast','2024-06-05',doc='forecast_revision')
            add('revision-2','FY',165000,'tdnet_forecast','2024-09-04',sequence=2,doc='forecast_revision')
            cur.execute('SELECT sales FROM api_latest_financials_canonical_forecast')
            assert cur.fetchone()[0]==165000
    finally:
        conn.rollback()
        conn.close()
