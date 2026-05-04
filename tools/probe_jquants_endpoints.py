#!/usr/bin/env python3
"""
probe_jquants_endpoints.py — J-Quants 詳細財務エンドポイント探索

目的: HTTP200になる正しいURLを見つける。
Usage:
    .venv\\Scripts\\python -X utf8 tools\\probe_jquants_endpoints.py
"""
from __future__ import annotations

import io, json, os, sys, time
from pathlib import Path

import requests

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "encoding") and _s.encoding and \
            _s.encoding.lower() not in ("utf-8", "utf8"):
        import io as _io
        obj = _io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace")
        if _s is sys.stdout:
            sys.stdout = obj
        else:
            sys.stderr = obj

sys.path.insert(0, str(Path(__file__).parent))
from jquants_auth import get_auth_headers

# 6861 キーエンス
LOCAL_CODE = "68610"

# 調査対象エンドポイント
CANDIDATES = [
    # V2
    ("GET", "https://api.jquants.com/v2/fins/fs_details",       {"code": LOCAL_CODE}),
    ("GET", "https://api.jquants.com/v2/fins/details",          {"code": LOCAL_CODE}),
    ("GET", "https://api.jquants.com/v2/fins/statements",       {"code": LOCAL_CODE}),
    ("GET", "https://api.jquants.com/v2/fins/financials",       {"code": LOCAL_CODE}),
    # V1
    ("GET", "https://api.jquants.com/v1/fins/fs_details",       {"code": LOCAL_CODE}),
    ("GET", "https://api.jquants.com/v1/fins/details",          {"code": LOCAL_CODE}),
    ("GET", "https://api.jquants.com/v1/fins/statements",       {"code": LOCAL_CODE}),
    # date パラメータ版
    ("GET", "https://api.jquants.com/v2/fins/statements",       {"date": "2026-04-24"}),
    ("GET", "https://api.jquants.com/v2/fins/fs_details",       {"date": "2026-04-24"}),
    ("GET", "https://api.jquants.com/v1/fins/statements",       {"date": "2026-04-24"}),
]

def main():
    headers = get_auth_headers()
    session = requests.Session()
    found = []

    print("\n" + "="*65)
    print("  J-Quants エンドポイント探索")
    print("="*65)

    for method, url, params in CANDIDATES:
        try:
            r = session.get(url, params=params, headers=headers, timeout=15)
            status = r.status_code
            body_preview = r.text[:200].replace("\n", " ")
            mark = "✅" if status == 200 else ("⚠️ " if status in (400, 404) else "❌")
            print(f"\n{mark} {status}  {url}")
            print(f"   params: {params}")
            print(f"   body:   {body_preview}")

            if status == 200:
                data = r.json()
                # トップレベルキー
                top_keys = list(data.keys()) if isinstance(data, dict) else ["(list)"]
                print(f"   top_keys: {top_keys}")

                # データ配列を特定
                items = None
                for k in top_keys:
                    v = data[k]
                    if isinstance(v, list) and v:
                        items = v
                        print(f"   data_key: '{k}'  len={len(v)}")
                        break

                if items:
                    sample = items[0]
                    print(f"   sample_keys ({len(sample)}): {sorted(sample.keys())}")
                    # gross_profit 候補
                    gp_keys = [k for k in sample.keys()
                               if "gross" in k.lower() or "gp" in k.lower()
                               or "売上総" in k]
                    print(f"   gross_profit 候補: {gp_keys}")
                    for k in gp_keys:
                        print(f"     {k} = {sample[k]}")

                found.append((url, params, data))
            time.sleep(0.5)

        except Exception as e:
            print(f"\n❌ ERROR  {url}")
            print(f"   {e}")

    session.close()

    print("\n" + "="*65)
    print(f"  結果: HTTP200 = {len(found)} エンドポイント")
    for url, params, _ in found:
        print(f"    ✅ {url}  params={params}")
    print("="*65)

    if not found:
        print("\n  全エンドポイントが失敗しました。")
        print("  考えられる原因:")
        print("    1. JQUANTS_API_KEY がPremiumプランでない")
        print("    2. /fins/statements は日付指定が必要 (date=YYYY-MM-DD)")
        print("    3. エンドポイント名が変更された")
        print("\n  次の対応:")
        print("    - /v2/fins/summary?date=2026-04-24 が200なら認証OK")
        print("    - J-Quants公式ドキュメントで詳細財務APIを確認してください")
        print("      https://jpx-jquants.com/")

if __name__ == "__main__":
    main()
