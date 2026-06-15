# EDINET受注データ 拡張DRY RUN 計画書

> **作成日**: 2026-06-15  
> **ステータス**: 設計（未実行）  
> **DB変更**: なし

---

## 0. 現時点の確認結果（31社・1年度分 DRY RUN）

### 統計（2025年度、31社）

| 指標 | 件数 | 備考 |
|---|---|---|
| Total rows | 31 | 全社1件ずつ |
| confidence: high | 17 | |
| confidence: medium | 10 | |
| confidence: low | 4 | 5985 / 6101 / 6266 / 6323 |
| source_unit: million_yen | 19 | |
| source_unit: unknown | 7 | 単位未検出 |
| source_unit: billion_yen | 1 | |
| source_unit: thousand_yen | 4 | |
| orders_received 取得成功 | 17/31 | |
| order_backlog 取得成功 | 14/31 | |
| construction_carryover | 4/31 | |
| completed_construction | 4/31 | |
| rpo | 7/31 | |

### 問題点

| # | 問題 | 対象銘柄 | 原因 |
|---|---|---|---|
| 1 | **unit_unclear（単位不明）** | 6254 / 6258 / 6315 / 6466 / 6834 の一部 | source_unit=None なのに値が取れている（円単位のまま） |
| 2 | **confidence=low** | 5985 / 6101 / 6266 / 6323 | テーブルから受注KPIを検出できず |
| 3 | **source_unit=unknown でも値あり** | IHI(7013) など | 単位変換できず null に落ちている可能性あり |
| 4 | **orders_received=None が多い** | 建設系4社・重工3社など | 受注高ではなく受注残高しかない場合、または検出失敗 |

---

## 1. Phase 1: 現 31社の精度改善（優先）

### 目標

> **合格基準**: 目視確認で8〜9割以上まとも / 単位ミスなし / セグメント値誤採用なし

### 確認すべき個別ケース

| ticker | 問題 | 確認項目 |
|---|---|---|
| 6254 | source_unit=None/unknown, orders_received=94,531,888（異常に大きい） | **円単位を百万円と誤認している可能性**。有報の単位表記を確認 |
| 6258 | source_unit=None/unknown, orders_received=79,512,424（同上） | 同上 |
| 6315 | source_unit=None/unknown, orders_received=47,429,464 | 同上 |
| 6466 | source_unit=None/unknown, orders_received=13,322,341 | 同上 |
| 6834 | source_unit=None, orders_received=21,380,102（千円単位で正解） | 変換済みなら21,380百万円が正しい。raw_or との整合確認 |
| 7013 IHI | orders_received=13,768, source_unit=unknown | IHIの受注高は数兆円規模のはず。単位不明で変換なし → null になるべき |
| 6141 DMG | orders_received=5,234, source_unit=yen | 約52億円（ユーロ建て換算？）。有報確認要 |
| 1969 | conf=high, orders_received=None | 受注残はあるが受注高はない可能性。RPOのみ? |
| 6981 | conf=medium, orders_received=None | 検出失敗。village: 製造業で受注高あるはず |

### 疑わしい値（単位ミス候補）

```
6254 orders_received=94,531,888  ← 945億円（百万円）か 9,453万円（円）か
6258 orders_received=79,512,424  ← 795億円か 7,951万円か
6315 orders_received=47,429,464  ← 474億円か 4,742万円か
6466 orders_received=13,322,341  ← 133億円か 1,332万円か
```

→ これらが **円単位のまま保存されている**場合は深刻なバグ。

---

## 2. Phase 2: 過去3年分拡張 DRY RUN

### 前提確認

| 項目 | 現状 |
|---|---|
| EDINETキャッシュ | 17,946 doc_id あり |
| 対象銘柄 | 31社（survey_detail.json） |
| 現在の年度 | 2025年度（1年度のみ） |
| 過去3年 | 2023年度・2024年度・2025年度 |

### 必要な作業

#### Step 1: 各社の過去3年分の doc_id を特定

EDINET API から `docTypeCode=120`（有価証券報告書）を検索し、  
対象31社の `edinetCode` に対応する doc_id を年度別に取得する。

```
必要なマッピング:
  ticker(4桁) → edinetCode → doc_id(年度別)
```

**課題**: 現在 `survey_detail.json` には ticker と doc_id(最新年度のみ)があるが、  
`edinetCode` は含まれていない。EDINET API で取得が必要。

#### Step 2: キャッシュ取得

過去2年分（2023・2024年度）の xbrl.zip をキャッシュに追加する。  
例: `python scripts/fetch_edinet_zip.py --doc-ids S1xxx S1yyy ...`

#### Step 3: multi-year survey_detail を生成

```json
[
  {"ticker": "1812", "doc_id": "S100W14C", "fiscal_end": "2025-03-31", ...},  // 2025年度
  {"ticker": "1812", "doc_id": "S100XXXX", "fiscal_end": "2024-03-31", ...},  // 2024年度
  {"ticker": "1812", "doc_id": "S100YYYY", "fiscal_end": "2023-03-31", ...},  // 2023年度
  ...
]
```

#### Step 4: DRY RUN 実行

```bash
python run_edinet_orders.py --dry-run \
  --save-json scratch/edinet_dryrun_3yr.json
```

#### Step 5: 合格基準確認

- [ ] 単位ミスがほぼない（source_unit=unknown のうち値あり件数が 1割以下）
- [ ] セグメント値を全社値として誤採用していない
- [ ] confidence=low に説明可能な null_reason がある
- [ ] 目視確認で8〜9割以上まとも

---

## 3. 先に対処すべき問題（Phase 1 完了前に Phase 2 に進まない）

> [!CAUTION]
> **単位ミスの疑いがある場合、DB 保存は禁止**。
> 6254 / 6258 / 6315 / 6466 の値を目視確認してから進める。

### 確認手順

```bash
# 1. 最新 DRY RUN JSON を確認
cat scratch/edinet_orders_20260615_080538.json | python -c "
import json,sys
data=json.load(sys.stdin)
for d in data:
    if d['ticker'] in ['6254','6258','6315','6466','6834']:
        print(d['ticker'], d.get('unit'), d.get('orders_received'), d.get('snippet','')[:100])
"

# 2. 有報の単位表記を直接確認
# → data/edinet_cache/{doc_id}/xbrl.zip を展開してテーブルヘッダーを確認
```

---

## 4. Phase 3: 300社拡大の条件

| 条件 | 基準 |
|---|---|
| Phase 1 合格 | 31社で8割以上まとも |
| Phase 2 合格 | 3年分で単位ミスなし |
| 銘柄追加方法 | EDINET 全件から business_description に受注KPIキーワードを持つ銘柄を抽出 |
| キャッシュ | 300社 × 3年 = 900 doc_id のダウンロードが必要 |

---

## 5. 実装上の懸念事項

### extractor.py の既知の弱点

| 弱点 | 影響 |
|---|---|
| セグメント別受注高を連結合計と誤認するリスク | セクションヘッダーの確認が不十分な場合 |
| 単位検出が失敗しても値を返す | unit_unclear なのに数値が出てしまう |
| テキストfallback が精度低 | medium confidence でも実際は誤り |
| 複数テーブルがある場合の優先順位 | 最初のテーブルを使うロジックが想定外を返す |

### 改善提案（Phase 1 完了後）

1. `source_unit=unknown` の場合は値を None にする（現状は値が残る）
2. テーブルヘッダーのセグメント検出強化（「○○事業」「○○セグメント」があればスキップ）
3. 合計行の検出精度向上（「合計」「計」だけでなく「連結」「グループ合計」も対象）

---

## 6. 次のアクション（優先順）

```
[必須] 1. 6254/6258/6315/6466 の単位を有報で目視確認
[必須] 2. 単位ミスがある場合は extractor.py を修正
[推奨] 3. 31社の confidence=low 4件の null_reason を説明できるか確認
[推奨] 4. セグメント値誤採用がないか、大きな値の企業を確認
[次段] 5. 合格後、過去3年分の doc_id 取得スクリプト作成
[次段] 6. multi-year DRY RUN 実行
[最後] 7. 合格後のみ DB 保存
```
