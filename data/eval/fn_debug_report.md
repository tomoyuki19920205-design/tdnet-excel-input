# FN 詳細デバッグレポート
対象件数: 11 件
---

## 140120260304575669.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `single_candidate_bs_table`
- **推定原因詳細**: all_table_candidates=1件のみ、bs_cf_like=25 valid_seg=1 → swap 対象なくそのまま bs_cf_guard

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 6 | 0.86 | ✓ |
| 3 | 0.65 | ✓ |
| 8 | 0.36 | ✓ |
| 0 | 0.35 | ✓ |
| 5 | 0.23 | ✓ |
| 7 | 0.13 | ✓ |
| 1 | 0.0 |  |
| 2 | 0.0 |  |
| 4 | 0.0 |  |

### Table Candidates (1 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 5 | 0.49 | 0.21 | 数値列2列; 数値行38行(5+); 数値密度38/48; 行数48; セグメント名行27行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 1
- bs_cf_like: 25
- segment_name_like_rows: 27
- reject_reason: `bs_cf_guard`

---

## 140120260312580469.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(2件)だが swap 条件不成立: bs_cf_like=8 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 0 | 0.49 | ✓ |
| 2 | 0.29 | ✓ |
| 4 | 0.29 | ✓ |
| 1 | 0.13 | ✓ |
| 3 | 0.13 | ✓ |
| 5 | 0.13 | ✓ |

### Table Candidates (2 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 3 | 0.28 | 0.15 | 数値列2列; 数値行12行(5+); 数値密度12/17; 行数17; セグメント名行9行; キャッシュ・フロー(減点) |
| 5 | 0.28 | 0.15 | 数値列2列; 数値行12行(5+); 数値密度12/17; 行数17; セグメント名行9行; キャッシュ・フロー(減点) |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 8
- segment_name_like_rows: 9
- reject_reason: `bs_cf_guard`

---

## 140120260312580847.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=1 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 0.97 | ✓ |
| 0 | 0.55 | ✓ |
| 5 | 0.54 | ✓ |
| 8 | 0.41 | ✓ |
| 1 | 0.365 | ✓ |
| 14 | 0.365 | ✓ |
| 10 | 0.33 | ✓ |
| 7 | 0.31 | ✓ |
| 9 | 0.31 |  |
| 4 | 0.25 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 10 | 0.5 | 0.21 | 数値列5列; 数値行6行(5+); 数値密度6/9; セグメント名行5行; 補助語2個 |
| 7 | 0.33 | 0.21 | 数値列1列(減点); 数値行24行(5+); 数値密度24/30; 行数30; セグメント名行16行; 補助語2個 |
| 9 | 0.32 | 0.21 | 数値列1列(減点); 数値行5行(5+); 数値密度5/8; セグメント名行4行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 1
- segment_name_like_rows: 5
- reject_reason: `bs_cf_guard`

---

## 140120260312580921.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=12 valid_seg=2

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 1.0 | ✓ |
| 7 | 0.43 | ✓ |
| 8 | 0.43 | ✓ |
| 0 | 0.37 | ✓ |
| 6 | 0.33 | ✓ |
| 10 | 0.325 | ✓ |
| 11 | 0.25 | ✓ |
| 4 | 0.24 | ✓ |
| 9 | 0.19 |  |
| 5 | 0.18 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 3 | 0.52 | 0.1 | セグメント別; 売上系:売上高; 利益系:営業利益; 数値列3列; 数値行3行(3+); 数値密度3/8; セグメント名 |
| 6 | 0.49 | 0.21 | 数値列2列; 数値行25行(5+); 数値密度25/31; 行数31; セグメント名行16行; 補助語2個 |
| 8 | 0.44 | 0.21 | 数値列2列; 数値行6行(5+); 数値密度6/8; セグメント名行4行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 2
- bs_cf_like: 12
- segment_name_like_rows: 16
- reject_reason: `bs_cf_guard`

---

## 140120260312580948.pdf
- **quarantine_reason**: `candidate_guard:narrative_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(4件)だが swap 条件不成立: bs_cf_like=1 valid_seg=1

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 4 | 0.545 | ✓ |
| 9 | 0.48 | ✓ |
| 3 | 0.43 | ✓ |
| 10 | 0.41 | ✓ |
| 0 | 0.37 | ✓ |
| 6 | 0.36 | ✓ |
| 7 | 0.33 | ✓ |
| 5 | 0.16 | ✓ |
| 8 | 0.12 |  |
| 1 | 0.02 |  |

### Table Candidates (4 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 9 | 0.62 | 0.33 | セグメント情報; 数値列2列; 数値行12行(5+); 数値密度12/27; 行数27; セグメント名行6行; 補助語3 |
| 9 | 0.51 | 0.23 | 数値列2列; 数値行8行(5+); 数値密度8/10; 行数10; セグメント名行4行; 補助語3個 |
| 7 | 0.49 | 0.21 | 数値列2列; 数値行35行(5+); 数値密度35/41; 行数41; セグメント名行25行; 補助語2個 |
| 6 | 0.27 | 0.36 | 数値列2列; 数値行39行(5+); 数値密度39/45; 行数45; セグメント名行26行; 補助語2個; 減価償却( |

### Candidate Guard 詳細
- valid_segment_like: 1
- bs_cf_like: 1
- segment_name_like_rows: 6
- reject_reason: `narrative_guard`

---

## 140120260313581230.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(2件)だが swap 条件不成立: bs_cf_like=25 valid_seg=5

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 0.915 | ✓ |
| 6 | 0.83 | ✓ |
| 7 | 0.41 | ✓ |
| 0 | 0.37 | ✓ |
| 5 | 0.33 | ✓ |
| 4 | 0.11 | ✓ |
| 1 | 0.02 |  |
| 2 | 0.0 |  |

### Table Candidates (2 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 5 | 0.49 | 0.21 | 数値列2列; 数値行38行(5+); 数値密度38/48; 行数48; セグメント名行25行; 補助語2個 |
| 1 | 0.24 | 0.0 | 数値列7列; 数値行3行(3+); 数値密度3/3 |

### Candidate Guard 詳細
- valid_segment_like: 5
- bs_cf_like: 25
- segment_name_like_rows: 25
- reject_reason: `bs_cf_guard`

---

## 140120260313581307.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=4 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 0.83 | ✓ |
| 7 | 0.48 | ✓ |
| 8 | 0.43 | ✓ |
| 0 | 0.37 | ✓ |
| 6 | 0.33 | ✓ |
| 9 | 0.29 | ✓ |
| 4 | 0.21 | ✓ |
| 5 | 0.18 | ✓ |
| 10 | 0.16 |  |
| 11 | 0.1 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 6 | 0.49 | 0.21 | 数値列2列; 数値行11行(5+); 数値密度11/15; 行数15; セグメント名行6行; 補助語2個 |
| 5 | 0.49 | 0.21 | 数値列2列; 数値行27行(5+); 数値密度27/34; 行数34; セグメント名行19行; 補助語2個 |
| 8 | 0.44 | 0.21 | 数値列2列; 数値行7行(5+); 数値密度7/9; セグメント名行5行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 4
- segment_name_like_rows: 6
- reject_reason: `bs_cf_guard`

---

## 140120260313581490.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=4 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 1.0 | ✓ |
| 9 | 0.535 | ✓ |
| 8 | 0.43 | ✓ |
| 0 | 0.37 | ✓ |
| 4 | 0.36 | ✓ |
| 6 | 0.33 | ✓ |
| 7 | 0.28 | ✓ |
| 5 | 0.18 | ✓ |
| 1 | 0.02 |  |
| 2 | 0.0 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 6 | 0.49 | 0.21 | 数値列2列; 数値行9行(5+); 数値密度9/12; 行数12; セグメント名行5行; 補助語2個 |
| 5 | 0.49 | 0.21 | 数値列2列; 数値行28行(5+); 数値密度28/36; 行数36; セグメント名行19行; 補助語2個 |
| 8 | 0.44 | 0.21 | 数値列2列; 数値行5行(5+); 数値密度5/8; セグメント名行3行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 4
- segment_name_like_rows: 5
- reject_reason: `bs_cf_guard`

---

## 140120260313581606.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=3 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 7 | 0.63 | ✓ |
| 3 | 0.62 | ✓ |
| 10 | 0.535 | ✓ |
| 8 | 0.43 | ✓ |
| 0 | 0.37 | ✓ |
| 6 | 0.33 | ✓ |
| 4 | 0.215 | ✓ |
| 9 | 0.19 | ✓ |
| 5 | 0.18 |  |
| 1 | 0.13 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 6 | 0.49 | 0.21 | 数値列2列; 数値行10行(5+); 数値密度10/13; 行数13; セグメント名行5行; 補助語2個 |
| 5 | 0.49 | 0.21 | 数値列2列; 数値行28行(5+); 数値密度28/36; 行数36; セグメント名行19行; 補助語2個 |
| 8 | 0.39 | 0.16 | 数値列2列; 数値行5行(5+); 数値密度5/7; セグメント名行2行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 3
- segment_name_like_rows: 5
- reject_reason: `bs_cf_guard`

---

## 140120260313581677.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(3件)だが swap 条件不成立: bs_cf_like=1 valid_seg=0

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 1.0 | ✓ |
| 4 | 0.905 | ✓ |
| 8 | 0.53 | ✓ |
| 9 | 0.455 | ✓ |
| 0 | 0.43 | ✓ |
| 7 | 0.33 | ✓ |
| 6 | 0.31 | ✓ |
| 12 | 0.29 | ✓ |
| 10 | 0.23 |  |
| 11 | 0.23 |  |

### Table Candidates (3 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 9 | 0.55 | 0.23 | 数値列2列; 数値行8行(5+); 数値密度8/11; 行数11; セグメント名行5行; 補助語3個 |
| 7 | 0.49 | 0.21 | 数値列2列; 数値行25行(5+); 数値密度25/32; 行数32; セグメント名行15行; 補助語2個 |
| 6 | 0.47 | 0.31 | 数値列2列; 数値行34行(5+); 数値密度34/42; 行数42; セグメント名行23行; 補助語2個; 減価償却( |

### Candidate Guard 詳細
- valid_segment_like: 0
- bs_cf_like: 1
- segment_name_like_rows: 5
- reject_reason: `bs_cf_guard`

---

## 140120260313581778.pdf
- **quarantine_reason**: `candidate_guard:bs_cf_guard`
- **推定原因カテゴリ**: `multi_candidate_no_swap`
- **推定原因詳細**: 複数候補あり(2件)だが swap 条件不成立: bs_cf_like=13 valid_seg=2

### Page Scores (top 10)
| page | score | in_top_pages |
|---|---|---|
| 3 | 1.0 | ✓ |
| 4 | 0.53 | ✓ |
| 10 | 0.46 | ✓ |
| 9 | 0.43 | ✓ |
| 0 | 0.37 | ✓ |
| 5 | 0.36 | ✓ |
| 7 | 0.33 | ✓ |
| 8 | 0.22 | ✓ |
| 6 | 0.18 |  |
| 1 | 0.045 |  |

### Table Candidates (2 件)
| page | score | segment_like | reason(抜粋) |
|---|---|---|---|
| 7 | 0.49 | 0.21 | 数値列2列; 数値行24行(5+); 数値密度24/30; 行数30; セグメント名行16行; 補助語2個 |
| 9 | 0.44 | 0.21 | 数値列2列; 数値行6行(5+); 数値密度6/8; セグメント名行3行; 補助語2個 |

### Candidate Guard 詳細
- valid_segment_like: 2
- bs_cf_like: 13
- segment_name_like_rows: 16
- reject_reason: `bs_cf_guard`

---

## 原因内訳（全件）
| 原因カテゴリ | 件数 |
|---|---|
| multi_candidate_no_swap | 10 |
| single_candidate_bs_table | 1 |

## 改善優先順位
1. **swap 条件 `segment_like_rows >= 2` を 1 に緩和**  
   → OR条件（account_like_rows < segment_like_rows）の方を優先させる。

2. **Phase B: account_like_rows が多い場合のスコアペナルティを強化**  
   → BS/CF表が best_table に選ばれにくくする（exclusion_penalty 追加）。

3. **Phase B-1.9 swap: `segment_like_rows >= 1` に閾値を下げる**  
   → 条件が厳しすぎる場合の緩和（第2候補でも1行あれば救済）。
