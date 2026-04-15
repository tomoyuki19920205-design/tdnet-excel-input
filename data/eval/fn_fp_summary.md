# FN / FP 分析レポート

## 全体サマリー

| 項目 | 件数 |
|---|---|
| 総 PDF 数 | 89 |
| has_segment_table = yes | 46 |
| has_segment_table = no  | 38 |
| has_segment_table = unknown | 5 |
| **FN 件数** | **20** |
| **FP 件数** | **22** |

---

## FN バケット別件数

| fn_reason_bucket | 件数 |
|---|---|
| bs_cf_guard | 20 |

## FP バケット別件数

| fp_reason_bucket | 件数 |
|---|---|
| bs_cf_like | 14 |
| total_or_report | 7 |
| narrative_like | 1 |

---

## FN 上位10件

| pdf | fn_reason_bucket | quarantine_reason |
|---|---|---|
| 140120260304575669.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580243.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580469.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580491.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580654.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580847.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580921.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580943.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260312580948.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |
| 140120260313581088.pdf | bs_cf_guard | candidate_guard:bs_cf_guard |

## FP 上位10件

| pdf | fp_reason_bucket | segment_names_preview |
|---|---|---|
| 140120260312580991.pdf | total_or_report | 報告 / その他 (注)1 |
| 140120260313581228.pdf | total_or_report | 報告 / 四半期連結 損益計算書 計上額 |
| 140120260313581329.pdf | total_or_report | 報告 / 中間連結損益 計算書計上額 |
| 140120260204547727.pdf | bs_cf_like | 持分法適用会社に対する持分相当額 / その他の包括利益合計 / 中間包括利益 |
| 140120260225568450.pdf | total_or_report | 前連結会計年度 (自 / その他有価証券評価差額金 / 繰延ヘッジ損益 |
| 140120260309578005.pdf | bs_cf_like | 現金及び預金 / 売掛金 / 商品及び製品 |
| 140120260310578917.pdf | bs_cf_like | 以上の結果、当第 / 年同期比 / 現金及び預金 |
| 140120260310578923.pdf | bs_cf_like | その他有価証券評価差額金 / 繰延ヘッジ損益 / その他の包括利益合計 |
| 140120260310579032.pdf | bs_cf_like | その他の包括利益合計 / 中間包括利益 / 親会社株主に係る中間包括利益 |
| 140120260310579206.pdf | total_or_report | I 前中間連結会計期間(自 |

---

## 改善優先順位

1. bs_cf_guard の緩和条件見直し（セグメント表と BS/CF を区別できる追加スコアを導入）
2. BS/CF/包括利益ワードの誤検出遮断（ガード句の拡充）
3. 詳細評価 GT を89件全体に拡充して precision/recall を再計測
