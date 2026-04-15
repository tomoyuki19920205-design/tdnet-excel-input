# FN best_page 診断レポート

## サマリー

| 指標 | 値 |
|-----|---|
| FN総件数 | 9 |
| bs_cf_guard 起因 | 8 |
| best_page 不一致 | 9 |
| 有効セグメントページ0件 | 1 |

## 原因カテゴリ別件数

| カテゴリ | 件数 | 意味 |
|---------|------|------|
| `header_boost_suppressed` | **7** | header_boost_suppressed_by_bs_cf でセグメントページが候補外れ |
| `guard_correct_page_rejected` | **1** | 正しいページが選ばれたが candidate_guard で落ちた |
| `bs_page_selected` | **1** | BSページが best_table として採用（抑制なし） |

## 代表3件の詳細

### 575669 （bs_cf_guard）

| 項目 | 値 |
|-----|---|
| best_page | pageNone |
| best_table_score | 0.49 |
| category | `guard_correct_page_rejected` |
| hbs_flagで抑制されたページ | 5 |
| mismatch | True |

**実セグメントKW含むページ：**  
page3(score=0.000, kw=['セグメント情報']) / page9(score=0.360, kw=['セグメント情報'])

**ページスコアTop4：**
```
  - page7: score=0.910  seg_hdr=N  sales=False  profit=True
  - page4: score=0.650  seg_hdr=N  sales=False  profit=True
  - page1: score=0.500  seg_hdr=N  sales=False  profit=True
  - page9: score=0.360  seg_hdr=Y  sales=False  profit=True
```

**なぜ正しいページが負けたか：**  
> header_boost_suppressed_by_bs_cf でpage5が抑制され、BSページpageNoneが採用

### 580469 （bs_cf_guard）

| 項目 | 値 |
|-----|---|
| best_page | page6 |
| best_table_score | 0.28 |
| category | `bs_page_selected` |
| hbs_flagで抑制されたページ | なし |
| mismatch | True |

**実セグメントKW含むページ：**  
（なし）

**ページスコアTop4：**
```
  - page1: score=0.590  seg_hdr=N  sales=False  profit=False
  - page3: score=0.340  seg_hdr=N  sales=True  profit=True
  - page5: score=0.340  seg_hdr=N  sales=True  profit=True
  - page4: score=0.180  seg_hdr=N  sales=False  profit=False
```

**なぜ正しいページが負けたか：**  
> セグメントKWを含む候補ページが存在しない

### 580921 （bs_cf_guard）

| 項目 | 値 |
|-----|---|
| best_page | page9 |
| best_table_score | 0.49 |
| category | `header_boost_suppressed` |
| hbs_flagで抑制されたページ | 6 |
| mismatch | True |

**実セグメントKW含むページ：**  
page3(score=0.000, kw=['セグメント情報']) / page4(score=1.000, kw=['セグメント別']) / page11(score=0.325, kw=['セグメント情報'])

**ページスコアTop4：**
```
  - page4: score=1.000  seg_hdr=Y  sales=True  profit=True
  - page1: score=0.520  seg_hdr=N  sales=True  profit=True
  - page8: score=0.480  seg_hdr=N  sales=True  profit=True
  - page9: score=0.480  seg_hdr=N  sales=False  profit=True
```

**なぜ正しいページが負けたか：**  
> header_boost_suppressed_by_bs_cf でpage6が抑制され、BSページpage9が採用

## 最終結論

- bs_cf_guard FN の主因は `Phase B header_boost_suppressed_by_bs_cf` にある。
- 実セグメント表ページが bscf_hits 高のため header_boost を抑制され、相対的にスコアが低くなりテーブル候補選択で負けている。
- 次に触るべき場所は `header_boost_suppressed_by_bs_cf` の発火条件（bscf 閾値）の精緻化。

## 次に触るべき箇所（1箇所のみ）

`Phase B header_boost_suppressed_by_bs_cf` の bscf 閾値。 現在 bscf>=1 で `header_boost` を全面抑制しているため、セグメント表の注記行（`セグメント資産`、`有形固定資産及び無形固定資産`）を含むページが 実セグメント表であっても boost されない。閾値を bscf>=3 以上に上げるか、 「セグメント情報/報告セグメント KW あり && bscf が注記由来のみ」なら boost を通す条件を追加する。
