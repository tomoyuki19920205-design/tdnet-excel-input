import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, r'C:\Users\takuy\OneDrive\tdnet-excel-input')
load_dotenv(r'C:\Users\takuy\OneDrive\tdnet-excel-input\.env')
load_dotenv(r'C:\Users\takuy\OneDrive\company-memo-app\.env.local')
from supabase import create_client

client = create_client(os.environ.get('NEXT_PUBLIC_SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('NEXT_PUBLIC_SUPABASE_ANON_KEY'))

def list_missing_prev_q():
    # Fetch recent earnings
    res = client.table('tdnet_events').select('ticker, raw_payload').eq('event_type', 'earnings').order('created_at', desc=True).limit(200).execute()
    missing_2q = []
    missing_3q = []
    
    if res.data:
        for r in res.data:
            rp = r['raw_payload']
            if isinstance(rp, str):
                try:
                    rp = json.loads(rp)
                except:
                    continue
            ext = rp.get('extracted', {})
            q = ext.get('quarter')
            comp = rp.get('notification_compare_json')
            if q in ('2Q', '3Q') and comp is None:
                if q == '2Q':
                    missing_2q.append(r['ticker'])
                else:
                    missing_3q.append(r['ticker'])
                    
    print(f"--- 過去前QデータがDBに無い銘柄一覧 (最近の200件中) ---")
    print(f"2Q銘柄 (前Q:1Q欠落) {len(missing_2q)}件: {', '.join(missing_2q)}")
    print(f"3Q銘柄 (前Q:2Q欠落) {len(missing_3q)}件: {', '.join(missing_3q)}")

if __name__ == "__main__":
    list_missing_prev_q()
