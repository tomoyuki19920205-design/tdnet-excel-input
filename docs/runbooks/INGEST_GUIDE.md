# TDNET Ingest — 運用ガイド

## 使い方

```powershell
# 通常実行（DB書き込み）
.\.venv\Scripts\python.exe tools\tdnet_ingest.py

# dry-run（DB書き込みなし）
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --dry-run

# 企業コード指定
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --company-code 0812

# 失敗時ダンプ
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --dump-on-error data\dumps

# ローカルZIPリプレイ（ネットワーク不使用）
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --replay data\docs\test.zip
```

---

## ステータス定義

| ステータス | 意味 | exit code |
|---|---|---|
| `inserted` | 新規レコードDB挿入 | 0 |
| `updated` | 既存レコード更新（訂正開示等） | 0 |
| `no_change` | 同一データ、変更なし | 0 |
| `dry_run` | 抽出成功（DBに書き込まず） | 0 |
| `skipped` | 仕様通りスキップ（下記参照） | 0 |
| `error` | 抽出失敗 | 1 |

### スキップ理由

| 理由 | 意味 |
|---|---|
| `SKIP_ALREADY_PROCESSED` | 同一 `disclosure_id` が処理済み（冪等性ガード） |
| `SKIP_NOT_TANSHIN_TITLE` | タイトルが決算短信でない（説明資料/質疑応答/再掲載等） |

---

## KPIの見方

```
[INGEST] run=ingest-xxx processed=8 success=3 skipped=5 error=0 (tanshin=8)
```

| 項目 | 定義 |
|---|---|
| `processed` | 決算短信フィルタ後の処理対象件数 |
| `success` | `inserted` + `updated` + `no_change` + `dry_run` の合計 |
| `skipped` | 仕様通りスキップ（上記理由のいずれか） |
| `error` | 抽出失敗（要調査） |
| `tanshin` | DisclosureType=FINANCIAL_STATEMENT の件数 |

### 成功率の計算

```
抽出成功率 = success / (success + error)
# skippedは母数に含めない（仕様通りの除外であるため）
```

### 監視の目安

- `error > 0` → **要調査** （`--dump-on-error` → `--replay` で再現）
- `SKIP_NOT_TANSHIN_TITLE` 件数が多い → フィルタ or fetcher 側の改善検討
- `success = 0` かつ `error = 0` → TDNETに新着決算短信がないだけ（正常）

---

## 抽出パイプライン

```
TDNET API → 開示一覧取得
  ↓
DisclosureType = FINANCIAL_STATEMENT のみ通過
  ↓
ルート1: XBRL（ZIP内iXBRL/XBRL展開 → ix:nonFraction解析）
  ↓ 失敗時
ルート2: PDF（タイトルが「決算短信」の場合のみ）
  ↓ タイトルがnon-tanshin
スキップ（SKIP_NOT_TANSHIN_TITLE）
```

## トラブルシューティング

```powershell
# 1. 失敗ZIPをダンプ
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --dry-run --dump-on-error data\dumps

# 2. ダンプされたZIPをリプレイ
.\.venv\Scripts\python.exe tools\tdnet_ingest.py --replay data\dumps\xxx.zip

# 3. 未知タグのログを確認
# → "[XBRL] 未知の財務タグ検出:" で検索
# → タグ辞書 (_XBRL_TAG_MAP in extractor.py) に追加
```
