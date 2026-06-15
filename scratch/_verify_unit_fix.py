"""
修正後 DRY RUN JSON の詳細確認
特に 6254/6258/6315/6466 の source_unit と正規化値を確認する
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 最新の DRY RUN JSON
import glob
SCRATCH = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\scratch")
jsons = sorted(SCRATCH.glob("edinet_orders_*.json"), reverse=True)
latest = jsons[0]
print(f"Using: {latest}\n")

data = json.loads(latest.read_text(encoding='utf-8'))

FOCUS = {"6254", "6258", "6315", "6466", "7013", "6834", "6141"}

print("=" * 70)
print("全31社 source_unit サマリ")
print("=" * 70)
unit_buckets = {}
for d in data:
    u = d.get("unit", "NONE")
    if u not in unit_buckets:
        unit_buckets[u] = []
    unit_buckets[u].append(d["ticker"])

for u, tickers in sorted(unit_buckets.items(), key=lambda x: x[0] or ""):
    print(f"  {u or 'None':15s}: {tickers}")

print("\n" + "=" * 70)
print("フォーカス企業詳細 (6254/6258/6315/6466/7013/6834/6141)")
print("=" * 70)
for d in data:
    if d.get("ticker") not in FOCUS:
        continue
    print(f"\n[{d['ticker']}] {d.get('company', '')}")
    print(f"  unit (raw)       : {d.get('unit')}")
    print(f"  confidence       : {d.get('confidence')}")
    print(f"  orders_received  : {d.get('orders_received')}  (raw)")
    print(f"  order_backlog    : {d.get('order_backlog')}  (raw)")
    print(f"  snippet (first 80): {str(d.get('snippet',''))[:80]}")

# Transformer で変換した結果も確認するために run_edinet_orders.py の変換ロジックを直接呼ぶ
print("\n" + "=" * 70)
print("Transformer 変換後の確認（source_unit / normalized value）")
print("=" * 70)

sys.path.insert(0, str(Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input")))
from src.edinet_orders.transformer import transform_to_db_row
import json as _json

SURVEY_JSON = Path(r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json')
survey = _json.loads(SURVEY_JSON.read_text(encoding='utf-8'))
fiscal_map = {d["ticker"]: d.get("fiscal_end") for d in survey}

for d in data:
    if d.get("ticker") not in FOCUS:
        continue
    ticker = d["ticker"]
    row = transform_to_db_row(d, fiscal_end=fiscal_map.get(ticker))
    print(f"\n[{ticker}]")
    print(f"  source_unit      : {row['source_unit']}")
    print(f"  raw_orders       : {row['raw_orders_received']}")
    print(f"  orders_received  : {row['orders_received']}  (百万円)")
    print(f"  raw_backlog      : {row['raw_order_backlog']}")
    print(f"  order_backlog    : {row['order_backlog']}  (百万円)")
    print(f"  null_reason      : {row['null_reason']}")
    print(f"  confidence       : {row['confidence']}")
