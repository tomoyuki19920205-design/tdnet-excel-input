# GT ラベル再監査レポート（残り FN 9件）

対象件数: 9 件

---

## 1. 対象一覧

| pdf | current | audited | reason | action |
|---|---|---|---|---|
| 140120260304575669.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260312580469.pdf | yes | **no** | no_segment_table_found | change_yes_to_no |
| 140120260312580921.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260312580948.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260313581230.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260313581307.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260313581490.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |
| 140120260313581606.pdf | yes | **yes** | actual_segment_table_found | keep_yes |
| 140120260313581778.pdf | yes | **no** | explicit_single_segment_omission | change_yes_to_no |

---

## 2. 集計

- **yes → no 修正候補**: 8 件
- **yes 維持**: 1 件
- **unknown**: 0 件

---

## 3. 各PDF 根拠サマリ

### 140120260304575669.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: 当社は、不動産投資ポータルサイト事業の単一セグメントであるため、記載を省略しております。
- 推奨: `change_yes_to_no`

### 140120260312580469.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `no_segment_table_found`
- 根拠: (ヘッダーキーワードなし)
- 推奨: `change_yes_to_no`

### 140120260312580921.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: なお、当社グループは、化粧品事業の単一セグメントであるため、セグメント別の記載は省略しております。
- 推奨: `change_yes_to_no`

### 140120260312580948.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: 当社グループの事業は、「金属製品加工事業」の単一セグメントであるため省略しております。
- 推奨: `change_yes_to_no`

### 140120260313581230.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: なお、当社はフィットネスクラブ運営事業の単一セグメントであるため、セグメント別の記載は省略しておりま
- 推奨: `change_yes_to_no`

### 140120260313581307.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: あるため、セグメント別の記載を省略しております。
- 推奨: `change_yes_to_no`

### 140120260313581490.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: 当社の事業セグメントは単一セグメントであるため、記載を省略しております。
- 推奨: `change_yes_to_no`

### 140120260313581606.pdf
- 現在: `yes` → 監査後: `yes`
- 理由: `actual_segment_table_found`
- 根拠: 売上高 営業利益 経常利益 / 法人税、住民税及び事業税 540 58,043
- 推奨: `keep_yes`

### 140120260313581778.pdf
- 現在: `yes` → 監査後: `no`
- 理由: `explicit_single_segment_omission`
- 根拠: 当社グループは単一セグメントであるため、記載を省略しております。
- 推奨: `change_yes_to_no`

---

## 4. screening_sheet.csv 反映すべき修正一覧

| pdf | 変更内容 |
|---|---|
| 140120260304575669.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260312580469.pdf | has_segment_table: yes → **no** (no_segment_table_found) |
| 140120260312580921.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260312580948.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260313581230.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260313581307.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260313581490.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
| 140120260313581778.pdf | has_segment_table: yes → **no** (explicit_single_segment_omission) |
