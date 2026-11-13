"""Transactional, NY-only addition of the published state; preserve all rows."""
import argparse,json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg2
from tools.apply_ny_market_provisional_migration import _load_env
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--production-root',required=True,type=Path);a=p.parse_args()
    _load_env(a.production_root)
    with psycopg2.connect(os.environ['SUPABASE_POSTGRES_URL'],connect_timeout=10) as con:
        with con.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='30000ms'")
            def snapshot():
                cur.execute("SELECT stable_key,quality_status,md5(report_markdown) FROM canonical_ny_market_reports ORDER BY stable_key")
                reports=cur.fetchall()
                cur.execute('SELECT (SELECT count(*) FROM canonical_news_events),(SELECT count(*) FROM canonical_sector_reports)')
                return reports,cur.fetchone()
            before=snapshot()
            cur.execute((Path(__file__).resolve().parents[1]/'migrations/025_ny_market_published.sql').read_text(encoding='utf8'))
            if before!=snapshot():raise RuntimeError('migration changed existing data')
    print(json.dumps({'status':'applied','ny_rows_preserved':len(before[0]),'other_counts_preserved':list(before[1])}))
if __name__=='__main__':main()
