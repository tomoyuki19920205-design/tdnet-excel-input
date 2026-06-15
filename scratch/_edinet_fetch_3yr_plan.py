"""
EDINET有報 過去3年分 doc_id 検索スクリプト

目的:
  対象31社について、過去3年分（2023年度・2024年度・2025年度相当）の
  有報 doc_id を EDINET API から取得し、
  multi-year 用 survey リストを生成する

DB操作: なし（EDINET API 読み取りのみ）
INSERT/UPDATE/DELETE: なし

注意:
  - EDINET API レート制限: 過剰アクセスしない
  - キャッシュ(data/edinet_cache)にすでにあれば再ダウンロード不要
"""
import json
import time
import requests
from pathlib import Path
from datetime import datetime

SCRATCH_DIR = Path(__file__).parent
CACHE_DIR = Path(__file__).parent.parent / "data" / "edinet_cache"
EDINET_DOC_API = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"

# 対象31社（survey_detail.jsonから取得済み）
TICKERS_WITH_INDUSTRY = [
    ("1762", "建設"), ("1802", "建設"), ("1812", "建設"),
    ("1952", "プラント・設備工事"), ("1969", "プラント・設備工事"),
    ("5631", "中小型・受注産業"),
    ("5805", "電子部品・電線・精密"), ("5985", "電子部品・電線・精密"),
    ("6101", "工作機械"), ("6103", "工作機械"), ("6104", "中小型・受注産業"), ("6141", "工作機械"),
    ("6254", "中小型・受注産業"), ("6258", "産業機械・自動化"),
    ("6266", "産業機械・自動化"), ("6315", "半導体製造装置"),
    ("6323", "産業機械・自動化"), ("6370", "中小型・受注産業"),
    ("6466", "中小型・受注産業"), ("6492", "中小型・受注産業"),
    ("6594", "電子部品・電線・精密"), ("6834", "電子部品・電線・精密"), ("6981", "電子部品・電線・精密"),
    ("7011", "重工・防衛・造船"), ("7013", "重工・防衛・造船"), ("7014", "重工・防衛・造船"),
    ("7735", "半導体製造装置"), ("8035", "半導体製造装置"),
    ("9682", "IT受託・SI"), ("9719", "IT受託・SI"), ("9749", "IT受託・SI"),
]

# 検索対象期間（有報提出年）
SEARCH_YEARS = [2023, 2024, 2025]  # 各年の4/1〜翌年3/31で有報提出分

def fetch_edinet_docs_for_year(year: int, edinetcode: str | None = None) -> list[dict]:
    """EDINET から年次有報(docTypeCode=120)の doc_id を取得"""
    results = []
    # 1月から12月まで月別に検索（API は1日1日検索が基本）
    # レート制限: 1秒間隔
    for month in range(1, 13):
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        date_str = f"{year}-{month:02d}-{last_day:02d}"
        try:
            resp = requests.get(
                EDINET_DOC_API,
                params={"date": date_str, "type": 2},  # type=2: 有価証券報告書等
                timeout=30
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for result in data.get("results", []):
                if result.get("docTypeCode") == "120":  # 120=有価証券報告書
                    results.append({
                        "doc_id": result.get("docID"),
                        "edinetCode": result.get("edinetCode"),
                        "filerName": result.get("filerName"),
                        "periodEnd": result.get("periodEnd"),
                        "submitDateTime": result.get("submitDateTime"),
                    })
            time.sleep(0.5)  # レート制限配慮
        except Exception as e:
            print(f"  WARN: {date_str} fetch failed: {e}")
    return results

def main():
    print("=" * 60)
    print("EDINET 過去3年分 doc_id 検索")
    print("DB操作: なし（READ ONLY）")
    print("=" * 60)
    
    # まず EDINET コードと ticker のマッピングが必要
    # → 現在の survey_detail.json にはない
    # → 別途 EDINET コードマスターが必要
    
    print("\n[NOTE] このスクリプトは設計書として保存します。")
    print("実際の実行には EDINET code ↔ ticker のマッピングが必要です。")
    print("")
    print("[PLAN]")
    print("Step 1: EDINET の URL から edinetCode を取得")
    print("  例: https://disclosure.edinet-fsa.go.jp/api/v2/companies.json")
    print("      ?type=2&category=EDINETCode&subcategory=...")
    print("")
    print("Step 2: 過去3年分の doc_id を月別に検索")
    print(f"  対象年: {SEARCH_YEARS}")
    print(f"  docTypeCode: 120（有価証券報告書）")
    print("")
    print("Step 3: 各 doc_id の xbrl.zip をキャッシュに保存")
    print(f"  cache dir: {CACHE_DIR}")
    print("")
    print("Step 4: multi-year survey_detail.json を生成")
    print(f"  出力: scratch/survey_detail_3yr.json")
    print("")
    print("Step 5: DRY RUN 実行")
    print("  python run_edinet_orders.py --dry-run")
    print("       --save-json scratch/edinet_dryrun_3yr.json")
    
    # キャッシュの現状確認
    cache_count = len(list(CACHE_DIR.iterdir())) if CACHE_DIR.exists() else 0
    print(f"\n[CACHE] {CACHE_DIR}")
    print(f"  現在のキャッシュ: {cache_count} doc_id")
    
    # 既存 survey の doc_id と一致するキャッシュ確認
    from pathlib import Path as P
    survey_json = P(r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json')
    survey = json.loads(survey_json.read_text(encoding='utf-8'))
    
    cached_ok = []
    cached_ng = []
    for d in survey:
        doc_id = d.get('doc_id')
        if not doc_id:
            continue
        if (CACHE_DIR / doc_id).exists():
            cached_ok.append(doc_id)
        else:
            cached_ng.append(doc_id)
    
    print(f"\n[既存31社 キャッシュ確認]")
    print(f"  キャッシュあり: {len(cached_ok)}")
    print(f"  キャッシュなし: {len(cached_ng)}")
    if cached_ng:
        print(f"  NG list: {cached_ng}")

if __name__ == '__main__':
    main()
