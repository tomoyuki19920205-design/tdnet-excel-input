# Source of Truth (正本) 定義

## 概要

同じ意味のデータが複数箇所に存在した場合、**どれを正本とみなすか**を定める。

> [!IMPORTANT]
> 正本以外から逆流して正本を上書きする行為は禁止。

## 1. 生ファイルの正本

| 正本 | 場所 | 補足 |
|:---|:---|:---|
| 取得済み PDF 原本 | `data/docs/*.pdf` | 再解析の基準 |
| XBRL ZIP 原本 | `data/xbrl_archive/` | XBRL fact 再抽出の基準 |
| HTML 原本 | `data/docs/` | HTML 表の再パースに使用 |

**注意**: 派生 CSV や手動 export は正本ではない。

## 2. 数値解釈の正本

| 正本 | 場所 | 補足 |
|:---|:---|:---|
| PL 実績数値 | `decision_db.quarterly_results` | field_sources でソース記録 |
| セグメント数値 | `decision_db.segment_financials` | — |
| J-Quants 正規化済み | `jquants.db.jquants_financials_normalized` | field-level COALESCE merge |

**原則**:
- 「どのタグ / どの抽出器 / どのルールで選んだか」を追跡可能にする
- viewer 表示用テーブル単体を数値解釈の唯一正本にしない
- Supabase は配信先 (Serving) であり、一次正本ではない

## 3. viewer 表示の正本

| 正本 | 場所 | 補足 |
|:---|:---|:---|
| viewer 向け financials | Supabase `financials` | `sync_financials.py` で同期 |
| viewer 向け segment | Supabase `segment_financials` | `sync_segments.py` で同期 |

**注意**:
- **Supabase テーブルに直接手修正して canonical の代わりにしないこと**
- viewer 表示の元は常に Normalized 層
- Supabase はあくまで Serving layer の一部

## 4. メモの正本

| 正本 | 場所 | 補足 |
|:---|:---|:---|
| 会社メモ | `decision_db.company_memos` | ユーザー入力のマスター |

**注意**:
- Excel 貼り付け UI や viewer の表示キャッシュは正本ではない
- Supabase と SQLite の間で双方向 sync する場合は競合解決ルールを明示すること

## 5. 通知の位置づけ

| 種別 | 位置づけ |
|:---|:---|
| Discord 差分通知 | **派生物** — 正本ではない |
| AI 要約文 | **派生物** — 再生成可能 |
| アラート文 | **派生物** — 再生成可能 |

通知は `filing_diff_summaries` から再生成できる。
通知テキストを修正しても DB に逆流させない。

## 6. エクスポートの位置づけ

| 種別 | 位置づけ |
|:---|:---|
| Excel export (`data.xlsx`, `data_jquants.xlsx`) | **派生物** — 正本ではない |
| CSV export (`artifacts/*.csv`) | **派生物** — 一時出力 |
| report markdown | **派生物** — 再生成可能 |
| viewer Parquet | **派生物** — rebuild 可能 |

> [!WARNING]
> Excel export を手で直しても DB に自動で逆流させない設計が原則。
> 手修正が必要な場合は明示的な import フローを経由すること。

## 7. 正本判定チェックリスト

新しくデータを追加する際に確認する:

- [ ] そのデータは「他のどこかから再生成できるか？」→ できるなら派生物
- [ ] 正本として保存するなら、根拠 (source, 抽出元) を記録しているか？
- [ ] 同じ意味のデータが既に別の場所に正本として存在しないか？
- [ ] viewer/export 用のコピーを正本扱いしていないか？
- [ ] 通知やレポートを修正しても逆流しない設計になっているか？
