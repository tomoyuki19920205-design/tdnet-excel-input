#!/usr/bin/env python3
"""
最小疎通テスト: J-Quants /v1/token/auth_user で refreshToken 取得のみ確認。
共通関数・キャッシュを一切使わない単独スクリプト。

使い方:
  python tools/test_jquants_auth_minimal.py
"""
import os
import json
from pathlib import Path

import requests

# ── .env 読み込み ──
env_path = Path(__file__).parent.parent / ".env"
print(f"[1] .env path: {env_path}")
print(f"    exists: {env_path.exists()}")

if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

mail = os.environ.get("JQUANTS_MAIL_ADDRESS", "").strip()
password = os.environ.get("JQUANTS_PASSWORD", "").strip()

# ── デバッグ出力 (秘匿) ──
if mail:
    at_idx = mail.find("@")
    if at_idx > 0:
        masked_mail = mail[:3] + "***" + mail[at_idx:]
    else:
        masked_mail = mail[:3] + "***"
else:
    masked_mail = "(empty)"

print(f"[2] JQUANTS_MAIL_ADDRESS: '{masked_mail}'  len={len(mail)}")
print(f"    JQUANTS_PASSWORD: len={len(password)}  first_char='{password[:1] if password else ''}'")

if not mail or not password:
    print("\nERROR: JQUANTS_MAIL_ADDRESS / JQUANTS_PASSWORD が .env にありません")
    exit(1)

# ── 方式1: requests.post(url, json=payload) ──
url = "https://api.jquants.com/v1/token/auth_user"
payload = {"mailaddress": mail, "password": password}

print(f"\n[3] URL: {url}")
print(f"    payload keys: {list(payload.keys())}")
print(f"    method: POST")

print("\n--- 方式1: json=payload ---")
resp1 = requests.post(url, json=payload, timeout=30)
print(f"    status: {resp1.status_code}")
print(f"    headers sent: Content-Type={resp1.request.headers.get('Content-Type')}")
body1 = resp1.text[:300]
print(f"    response: {body1}")

# ── 方式2: data=json.dumps(payload), Content-Type明示 ──
print("\n--- 方式2: data=json.dumps, Content-Type: application/json ---")
headers2 = {"Content-Type": "application/json"}
resp2 = requests.post(url, data=json.dumps(payload), headers=headers2, timeout=30)
print(f"    status: {resp2.status_code}")
print(f"    headers sent: Content-Type={resp2.request.headers.get('Content-Type')}")
body2 = resp2.text[:300]
print(f"    response: {body2}")

# ── 方式3: form-encoded ──
print("\n--- 方式3: data=payload (form-encoded) ---")
resp3 = requests.post(url, data=payload, timeout=30)
print(f"    status: {resp3.status_code}")
print(f"    headers sent: Content-Type={resp3.request.headers.get('Content-Type')}")
body3 = resp3.text[:300]
print(f"    response: {body3}")

# ── 結果まとめ ──
print("\n=== SUMMARY ===")
for i, resp in enumerate([resp1, resp2, resp3], 1):
    status = resp.status_code
    has_token = "refreshToken" in resp.text
    print(f"  方式{i}: HTTP {status}  refreshToken={has_token}")

if any("refreshToken" in r.text for r in [resp1, resp2, resp3]):
    print("\n SUCCESS: refreshToken 取得成功!")
else:
    print("\n FAILED: 全方式で refreshToken 取得失敗")
    print("  → J-Quantsアカウントのプラン選択状態・API利用権限を確認してください")
