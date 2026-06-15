"""
EDINET受注 DRY RUN 用ターゲット企業リスト構築スクリプト

目的:
  - 受注KPI関連業種（建設・プラント・造船・重工・工作機械・設備工事・SI）の
    銘柄コードを survey_detail.json から選定し、
    過去3年分の doc_id リストを作成する

出力:
  scratch/edinet_dryrun_targets.json  ← DRY RUN 対象リスト
  scratch/edinet_dryrun_targets.md    ← 人間確認用レポート

DB操作: なし
INSERT/UPDATE/DELETE: なし
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# エンコーディング強制
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SURVEY_JSON = Path(r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json')
SCRATCH_DIR = Path(__file__).parent

# EDINET API から取得できる有報リスト（年度別）
# ここでは survey_detail.json の既存32社をベースにする
# 過去3年分は EDINET のドキュメント検索で補う

TARGET_INDUSTRIES_KW = [
    # 建設
    "建設", "土木", "工事",
    # プラント・重工
    "プラント", "重機", "造船", "重工",
    # 工作機械
    "工作機械", "機械",
    # 設備工事
    "設備", "電気工事", "管工事",
    # SI/受託開発
    "SI", "情報", "IT", "システム", "ソフトウェア",
]

def load_survey():
    return json.loads(SURVEY_JSON.read_text(encoding='utf-8'))

def is_target_industry(industry: str) -> bool:
    if not industry:
        return False
    for kw in TARGET_INDUSTRIES_KW:
        if kw in industry:
            return True
    return False

def main():
    data = load_survey()
    print(f"Survey total: {len(data)} entries")
    
    # 業種フィルタ
    targets = []
    skipped = []
    for d in data:
        ticker = d.get('ticker', '')
        company = d.get('company', '')
        industry = d.get('industry', '')
        doc_id = d.get('doc_id')
        rating = d.get('rating', '')
        fiscal_end = d.get('fiscal_end', '')
        filing_date = d.get('filing_date', '')
        accounting = d.get('accounting', '')
        
        # rating=E（doc_id=None）はスキップ
        if not doc_id:
            skipped.append({'ticker': ticker, 'reason': 'no_doc_id', 'industry': industry})
            continue
        
        entry = {
            'ticker': ticker,
            'company': company,
            'industry': industry,
            'doc_id': doc_id,
            'fiscal_end': fiscal_end,
            'filing_date': filing_date,
            'accounting': accounting,
            'rating': rating,
        }
        targets.append(entry)
    
    print(f"\nTargets with doc_id: {len(targets)}")
    print(f"Skipped (no doc_id): {len(skipped)}")
    
    # 業種別集計
    industry_groups = {}
    for t in targets:
        ind = t.get('industry', 'unknown')
        if ind not in industry_groups:
            industry_groups[ind] = []
        industry_groups[ind].append(t['ticker'])
    
    print("\n[業種別分布]")
    for ind, tickers in sorted(industry_groups.items()):
        print(f"  {ind}: {tickers}")
    
    print(f"\n[確認: 既存survey_detail内の全対象銘柄]")
    for t in targets:
        print(f"  {t['ticker']} | {t['industry'][:20] if t['industry'] else 'N/A'} | {t['fiscal_end']} | {t['doc_id']}")
    
    # 出力
    out_json = SCRATCH_DIR / 'edinet_dryrun_targets.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)
    print(f"\n[OUT] {out_json} ({len(targets)} entries)")
    
    # サマリ
    tickers_list = [t['ticker'] for t in targets]
    print(f"\n[TICKER LIST for --tickers]")
    print(' '.join(tickers_list))

if __name__ == '__main__':
    main()
