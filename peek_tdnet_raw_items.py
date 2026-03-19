import json, sys
from tools.tdnet_api import fetch_tdnet_today  # ← もし名前違ったらエラー出るので、その場合は次の行だけ教えて
items = fetch_tdnet_today()
print("raw_items=", len(items))
# ここは tdnet_ingest と同じ分類関数があるはずなので、それを呼ぶのが理想だが、まずは item の素の中身を覗く
for i,x in enumerate(items[:50],1):
    # 典型キー: id / disclosure_id / code / ticker / title / category 等
    print(i, list(x.keys())[:12], x.get("id") or x.get("disclosure_id"), x.get("code") or x.get("ticker"), x.get("title"))
