"""
Supabase PL関連テーブル調査スクリプト
"""
import urllib.request
import urllib.error
import json

SUPABASE_URL = "https://fvkvfekzoebcolssnteo.supabase.co"
SERVICE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ2a3ZmZWt6b2ViY29sc3NudGVvIiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjEwOTYwMywiZXhwIjoyMDg3Njg1NjAzfQ"
    ".gyNrj7Fnr2x2eaqxTcPSVmtXbDE7tAI83s7qx-sdX-A"
)

BASE_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": "Bearer " + SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "count=exact",
}


def check_table_exists(table_name):
    """テーブルの存在確認と件数取得"""
    url = f"{SUPABASE_URL}/rest/v1/{table_name}?select=count&limit=0"
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            content_range = r.headers.get("Content-Range", "?")
            return True, content_range
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return False, body


def get_table_columns(table_name):
    """カラム一覧取得（1件取得してキーを確認）"""
    url = f"{SUPABASE_URL}/rest/v1/{table_name}?limit=1"
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode())
            if data:
                return list(data[0].keys())
            return []
    except urllib.error.HTTPError:
        return []


def get_ticker_count(table_name, ticker="5423"):
    """特定銘柄の件数確認"""
    url = f"{SUPABASE_URL}/rest/v1/{table_name}?ticker=eq.{ticker}&select=count&limit=0"
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            return r.headers.get("Content-Range", "?")
    except urllib.error.HTTPError as e:
        return f"ERROR: {e.read().decode()[:100]}"


def get_latest_record(table_name):
    """最新レコードの日時を確認"""
    # updated_at で試みる
    for date_col in ["updated_at", "created_at", "reported_at", "inserted_at"]:
        url = f"{SUPABASE_URL}/rest/v1/{table_name}?select={date_col}&order={date_col}.desc&limit=3"
        req = urllib.request.Request(url, headers=BASE_HEADERS)
        try:
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read().decode())
                if data and data[0].get(date_col):
                    return date_col, [d.get(date_col) for d in data]
        except urllib.error.HTTPError:
            continue
    return None, []


def get_ticker_pl_data(table_name, ticker="5423"):
    """銘柄のPLデータサンプル取得"""
    url = f"{SUPABASE_URL}/rest/v1/{table_name}?ticker=eq.{ticker}&limit=5"
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError:
        return []


# ============================================================
# メイン調査
# ============================================================
TARGET_TABLES = [
    "financials",
    "canonical_financials",
    "latest_financials",
    "earnings_summaries",
    "tdnet_events",
]

print("=" * 70)
print("Supabase PL関連テーブル調査レポート")
print("=" * 70)

results = {}
for tbl in TARGET_TABLES:
    print(f"\n{'─'*50}")
    print(f"[{tbl}]")
    exists, info = check_table_exists(tbl)
    results[tbl] = {"exists": exists}

    if exists:
        # Content-Range: 0-0/TOTAL の形式
        total = info.split("/")[-1] if "/" in info else info
        print(f"  EXISTS   | 総行数: {total}")
        results[tbl]["total_rows"] = total

        cols = get_table_columns(tbl)
        print(f"  カラム   | {cols}")
        results[tbl]["columns"] = cols

        date_col, dates = get_latest_record(tbl)
        if date_col:
            print(f"  最新日時 | ({date_col}): {dates}")
            results[tbl]["latest_dates"] = dates
        else:
            print(f"  最新日時 | 日付カラム見つからず")

        count_5423 = get_ticker_count(tbl, "5423")
        print(f"  5423件数 | {count_5423}")
        results[tbl]["count_5423"] = count_5423

        # 5423のPLサンプル
        if "financials" in tbl or "earnings" in tbl:
            sample = get_ticker_pl_data(tbl, "5423")
            if sample:
                print(f"  5423サンプル | {json.dumps(sample[0], ensure_ascii=False)[:300]}")
    else:
        print(f"  NOT FOUND | {info[:100]}")

print("\n" + "=" * 70)
print("=== サマリー ===")
for tbl, r in results.items():
    status = "EXISTS" if r["exists"] else "NOT FOUND"
    rows = r.get("total_rows", "-")
    c5423 = r.get("count_5423", "-")
    print(f"  {tbl:<30} {status:<12} rows={rows:<8} 5423={c5423}")
print("=" * 70)

# tdnet_events のカラム詳細確認（もし存在すれば）
print("\n=== tdnet_events 詳細 ===")
if results.get("tdnet_events", {}).get("exists"):
    url = f"{SUPABASE_URL}/rest/v1/tdnet_events?ticker=eq.5423&limit=2"
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode())
            for row in data:
                print(json.dumps(row, ensure_ascii=False, indent=2)[:800])
    except urllib.error.HTTPError as e:
        print(f"ERROR: {e.read().decode()[:200]}")
else:
    print("tdnet_events は存在しないかアクセス不可")
