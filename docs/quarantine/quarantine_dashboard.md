# Quarantine Dashboard

## 現在の状態

| review_hint | 件数 | rescue可能性 |
|---|---|---|
| `pdf_no_sales_profit_columns` | 79 | 中 — header正規化/synonym追加で一部改善の余地 |
| `pdf_no_segment_page_candidate` | 45 | 中 — page scoring KW 拡張 |
| `pdf_no_rows_extracted` | 15 | 低 — 行ラベルパターンが特殊 |
| `pdf_no_segment_table_candidate` | 6 | 低 — cid文字化けPDF (OCR必要) |
| `pdf_extraction_failed` | 1 | 低 — PDF破損 |
| **合計** | **146** | |

## カテゴリ別分析

### pdf_no_sales_profit_columns (79件)

**原因**: 列ヘッダーから売上/利益列を特定できない。

**主な失敗パターン**:
1. ヘッダーband 誤認 — 表タイトル行を列ヘッダーと認識
2. 独自形式の列名 — 業界特有の用語 (「正味収入保険料」等)
3. cid文字化け — embedded font PDF

**改善候補**:
- ヘッダーband 検出の改善 (タイトル行スキップ)
- 業界別 synonym プロファイル
- OCR パイプライン

### pdf_no_segment_page_candidate (45件)

**原因**: セグメント情報を含むページが検出できない。

**主な失敗パターン**:
1. セグメントKW不在 — 「事業概況」等の別表現
2. 1ページPDF (決算短信要約のみ)
3. 画像埋め込みPDF

**改善候補**:
- page scoring KW の第5弾拡張
- 文書構造ヒューリスティック

### pdf_no_rows_extracted (15件)

**原因**: 表内にセグメント名行が検出できない。

**主な失敗パターン**:
1. ラベル列の位置が非標準 (右端・中央)
2. ラベルが数字混在 (「第1事業部」等)
3. 行マージによるラベル消失

### pdf_no_segment_table_candidate (6件)

**原因**: ページ内のテーブル候補が閾値未達。

**主な失敗パターン**:
- ほぼ全件 cid 文字化け → OCR 必須

### pdf_extraction_failed (1件)

**原因**: pdfplumber によるテキスト抽出自体が失敗。

---

## parse_quality 内訳

rescued セグメントの品質:

| parse_quality | 意味 | 割合 |
|---|---|---|
| `full` | sales + profit 両方 | ~30% |
| `partial_sales_only` | 売上のみ | ~70% |

### partial → full 昇格の条件

profit 列検出精度の向上が必要:
- 「利益」単独の条件付き昇格 (Phase 5 で一部実装済)
- 値大小比率による profit 推定の閾値調整
- ヘッダーband 修正 (正しい列ヘッダーが data 行に含まれるケース)

---

## 改善の費用対効果

| 改善候補 | 推定rescue | 工数 | ROI |
|---|---|---|---|
| ヘッダーband 改善 | ~15件 | 中 | 高 |
| 業界別synonym | ~10件 | 低 | 高 |
| OCR pipeline | ~12件 | 高 | 中 |
| page KW 5th wave | ~5件 | 低 | 中 |
| AI assisted extraction | ~20件 | 高 | 低 |
