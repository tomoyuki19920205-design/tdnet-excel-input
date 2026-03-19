# TDnet決算自動入力システム

TDnetで公開される決算短信等から累計売上高・粗利益・営業利益を抽出し、共有クラウドExcel（OneDrive同期）の該当セルへ安全に自動入力する常駐スクリプトです。

## 安全機能

| 機能 | 説明 |
|------|------|
| 🔒 誤爆防止 | A列コード→M列150行→N列近傍の3段階で行を特定 |
| 🔒 上書き禁止 | 既存値≠新値の場合は更新せず `conflict_detected` |
| 🔒 二重入力防止 | SQLiteで disclosure_id を管理、同一開示は再処理しない |
| 🔒 共有安全 | ロック時は3,5,8,13,21秒のリトライ後に安全停止 |

## セットアップ

### 1. 依存パッケージのインストール

```bash
cd playground/tdnet-excel-input
pip install -r requirements.txt
```

### 2. 設定ファイルの作成

```bash
copy config.yaml.example config.yaml
```

`config.yaml` を編集：

```yaml
# Excelファイルのパス（スラッシュ表記推奨）
excel_path: "C:/Users/takuy/OneDrive/共有ファイル.xlsx"

# 対象シート名
sheet_name: "PL"

# ウォッチリスト（空で全銘柄）
watch_tickers:
  - "6750"
  - "7203"
```

### 3. 起動

```bash
python src/main.py
# または設定ファイルを指定
python src/main.py path/to/config.yaml
```

### 4. バックグラウンド実行（Windows）

```batch
@echo off
cd /d %~dp0
python src/main.py
```
上記を `start.bat` として保存し、タスクスケジューラに登録。

## Excel仕様

| 列 | 内容 | 例 |
|----|------|----|
| A列 | 企業コード | 6750 |
| M列 | 決算年月 | R8/3 |
| N列 | 四半期 | 1Q, 2Q, 3Q, 4Q |
| O列 | 累計売上 | |
| P列 | 累計粗利 | |
| S列 | 累計営業利益 | |

## エラーステータス一覧

| ステータス | 意味 |
|-----------|------|
| `success` | 正常に書き込み完了 |
| `code_not_in_sheet` | 企業コードがA列に見つからない |
| `missing_term_within_150` | 年度がM列の探索範囲内に見つからない |
| `missing_quarter_near_term` | 四半期がN列の近傍に見つからない |
| `conflict_detected` | 既存値と異なる値→上書き禁止 |
| `file_locked_or_save_failed` | 保存/ロック失敗（5回リトライ後） |
| `parse_failed` | 数値抽出失敗 |
| `download_failed` | ドキュメントのダウンロード失敗 |
| `unconfirmed_year` | 年度を特定できない |

## ログ

- テキストログ: `data/app.log`
- SQLiteDB: `data/state.db`（処理履歴、冪等性管理）

## iXBRL Probe（診断ツール）

ZIP内のiXBRL（`*.ixbrl.htm`）をパースし、売上・営業利益のXBRLコンセプト候補を探索する診断ツールです。

### 使い方（これだけ叩けばOK）

```powershell
.\.venv\Scripts\python.exe tools\ixbrl_probe.py .\data\docs\081220260225568550.zip
```

または `probe_ixbrl.bat` を使用：

```batch
probe_ixbrl.bat .\data\docs\081220260225568550.zip
```

### 出力内容

- ZIP内 `.ixbrl.htm` / `.xbrl` の一覧
- 各ファイルのBOM有無・エンコーディング推定・XMLパース結果
- 売上/営業利益に対応するXBRLコンセプト（name属性）候補
- 単位（unitRef）の手がかり

## XBRL ETL — DB投入手順

XBRL/iXBRL抽出済みの決算データをSQLiteへ投入するETLパイプラインです。

### ⚠️ 重要ルール

| ルール | 説明 |
|--------|------|
| 🔢 **valueは円整数(JPY)** | 百万円・千円等のXBRL値はETLで円に変換して格納 |
| 🔒 **修正開示は上書きしない** | 修正・訂正開示は新しいdisclosureとして追加。分析時は `v_latest_facts` / `v_latest_guidance` ビューで最新値を参照 |
| ♻️ **冪等** | 同一開示（sha256一致）を再投入してもfactsは増えない |

### 1. DB初期化

```powershell
.\.venv\Scripts\python.exe -m tools.migrate_db --db data/xbrl.db
```

### 2. データ投入

```powershell
# 単一ファイル
.\.venv\Scripts\python.exe -m tools.load_results_to_db --db data/xbrl.db --input results/disclosure.json

# ディレクトリ一括
.\.venv\Scripts\python.exe -m tools.load_results_to_db --db data/xbrl.db --input results/
```

### 3. 最新値の参照

```sql
-- 最新の実績値
SELECT * FROM v_latest_facts
WHERE company_id = 1 AND metric = 'NET_SALES';

-- 最新の会社予想
SELECT * FROM v_latest_guidance
WHERE company_id = 1 AND metric = 'NET_SALES';
```

### メトリクスマッピング

| 入力キー | DB metric |
|----------|-----------|
| sales | NET_SALES |
| gross_profit | GROSS_PROFIT |
| op_income | OP_INCOME |
| ordinary_income | ORDINARY_INCOME |

## Project Script Guide

プロジェクト内の全スクリプト・ツールの役割一覧は [docs/project_file_guide.md](docs/project_file_guide.md) を参照。
