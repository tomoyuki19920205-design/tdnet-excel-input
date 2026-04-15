# 保存方針 (Storage Policy)

## 基本方針

> 「将来使うかも」で何でも保存しない。
> 「再生成困難」「監査必要」「根拠保持必要」のものだけを優先保存する。

## 1. 保存してよいもの

| 対象 | 層 | 根拠 |
|:---|:---|:---|
| 取得元 raw file (PDF, XBRL ZIP) | Raw | 再取得困難、証拠 |
| 文書識別メタデータ (doc_id, ticker, disclosed_date) | Raw | 追跡性 |
| canonical 数値の根拠 (quarterly_results + field_sources) | Normalized | 数値判定の追跡 |
| quarantine 理由 | Normalized | review のため |
| Serving テーブルの最終表示値 | Serving | 利用者向け |
| company_memos | Normalized/Serving | ユーザー入力正本 |
| buyback_events (確定済み) | Normalized | 企業行動確定データ |
| 重要な抽出スコアや選択理由 | Normalized | デバッグ・品質管理 |
| エラー調査に必要な最小限ログ | Raw (meta) | 障害対応 |

## 2. 破棄または短期保持でよいもの

| 対象 | 理由 |
|:---|:---|
| 展開途中の一時 XML/HTML 断片 | 再抽出可能 |
| artifacts/ 配下の一時 CSV/JSONL | 再生成可能 |
| OCR 中間画像 | 再生成可能 |
| パース途中の冗長な JSON | 再計算可能 |
| 再計算可能な候補一覧 (buyback candidate 中間) | scanner で再生成 |
| 大量の全文複製 | Raw にある |
| viewer cache / CDN cache | 再構築可能 |
| ad hoc debug dump | 調査完了後に削除 |

## 3. 保持期間方針

| 対象 | 保持期間 | 補足 |
|:---|:---|:---|
| Raw archive (PDF, ZIP) | **長期** (5年+) | 再取得困難なため |
| canonical 数値 (quarterly_results 等) | **長期** | 正本 |
| quarantine 詳細 | **中期** (1年) | review 解消後は概要のみ |
| processing_log | **中期** (1年) | 取得履歴 |
| artifacts/ 一時出力 | **短期** (30日) | 再生成可能 |
| debug log | **短期** (30-90日) | 調査用 |
| temp extracted XML text | **即時** or 短期 | 再抽出可能 |
| 全文検索用チャンク | 検索機能導入後に別途定義 | — |
| embeddings | 用途確定後にのみ保存 | 高コスト |

## 4. 絶対ルール

> [!CAUTION]
> 以下のルールは例外なく適用する。

1. **全文保存を標準設計にしない** — PDF 本文 / HTML 全文を数値テーブルに混ぜない
2. **全文検索をやるなら専用テーブル / 専用層に分離する** — Search 層
3. **「とりあえず全部DBへ」は禁止** — 保存する理由を明示すること
4. **3年超データの整理ルール** — 将来検討対象として明示的に記録
5. **artifacts/ の一時ファイルは定期削除対象** — `cleanup_intermediate_data.py` で管理

## 5. PDF / XML / OCR / Embeddings の扱い

| 種別 | 保存方針 |
|:---|:---|
| PDF 原本 | Raw archive に永続保存 |
| PDF 本文テキスト | Raw に既に PDF がある。全文は Search 層へ (将来) |
| XBRL/XML 原本 | ZIP アーカイブとして永続保存 |
| 展開済み XML | 再展開可能。短期保持 or 破棄 |
| OCR 結果テキスト | 抽出数値のみ Normalized へ。全文は Search 層 |
| OCR 中間画像 | 即時破棄 or 短期保持 |
| Embeddings | 用途確定後にのみ生成・保存。専用 DB |
