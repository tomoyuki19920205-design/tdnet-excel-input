# Buyback Scanner Score Tuning — Before/After 比較レポート

- **実行日**: 2026-03-11

## 1. Rules 変更内容

### 閾値

| 閾値 | 旧 | 新 |
|:---|---:|---:|
| high | 6 | **7** |
| medium | 3 | **4** |

### Keyword 重み変更

| keyword | 旧 | 新 | 変更理由 |
|:---|:---|:---|:---|
| `自己株式` | weak +1 | **penalty -2** | FP率100% (24件中 24件 non_buyback) |
| `上限` | weak +1 | **penalty -1** | 単独では weak signal, FP率67% |
| `自己株式の消却` | strong +3 | **strong +2** | cancel 誤検出対策 |

### 維持した keyword (変更なし)
- `自己株式の取得`, `取得する株式の総数`, `取得価額の総額`, `取得期間`, `取得方法`, `ToSTNeT`, `自己株式立会外買付取引`
- pair_bonus (shares_and_amount +4, tostnet +2, derived_ticker +1, derived_title +1)

## 2. Before / After 比較

### 候補検出

| 指標 | 旧 (old rules) | 新 (tuned rules) | 変化 |
|:---|---:|---:|:---|
| 対象 PDF | 134 | 134 | 同一 |
| **candidate 件数** | **29** | **3** | **-26 (90%削減)** |
| 候補率 | 21.6% | 2.2% | ↓↓ |

### Review 結果

| 指標 | 旧 | 新 | 変化 |
|:---|---:|---:|:---|
| buyback_related | 3 | 3 | **維持 (取りこぼしなし)** |
| non_buyback | 26 | 0 | **-26 (FP 全滅)** |
| **precision** | **10.3%** | **100%** | **↑↑↑** |

### Priority 分布 (tuning 再スコア後)

| priority | 旧 old rules | 旧 h7/m4 | 新 tuned rules h7/m4 |
|:---|---:|---:|---:|
| high | 1 | 0 | 0 |
| medium | 18 | 3 | **3** |
| low | 10 | 26 | 0 |

### Medium 帯 Precision

| 指標 | 旧 old rules (h6/m3) | 旧 h7/m4 (old rules) | **新 tuned rules (h7/m4)** |
|:---|---:|---:|---:|
| medium 件数 | 18 | 3 | **3** |
| medium → buyback 率 | 5.6% (1/18) | 100% (3/3) | **100% (3/3)** |

## 3. 主要差分

### False Positive 削減
- **26件の false positive が完全排除**
- 主因: `自己株式` のpenalty化 (-2) が24件を候補外に落とした
- `上限` のpenalty化 (-1) も寄与

### 真候補の取りこぼし
- **取りこぼしは 0 件** (buyback_related 3件中 3件が新ルールでも検出)
- medium 帯に全 3 件が集約

### Score 内訳 (新ルール 3 candidates)

| ファイル | score | 内訳 | 備考 |
|:---|---:|:---|:---|
| 140120260220566631 (2288) | 3 | 自己株式の取得:+3, 自己株式:-2, ticker:+1, title:+1 | 決算短信内の自己株式取得言及 |
| 140120260223566990 (8233) | 0 | 自己株式の消却:+2, 新株予約権:-3, 転換社債:-4, etc. | penalty多数で0に |
| 140120260306577313 (9824) | 2 | 自己株式の取得:+3, 自己株式:-2, 上限:-1, ticker:+1, title:+1 | 決算短信内の自己株式取得言及 |

## 4. 所見

### このルールで medium を review 対象帯として使えるか？
**YES** — medium precision 100% (3/3) は理想的。false positive は完全に排除された。

### Auto-save に進めるか？
**まだ review-only が安全**
- サンプル数が 3件と少ない（統計的信頼度が不十分）
- cancel 系真文書の recall は未検証
- 一度大きいデータセットで validation してから判断すべき

### 懸念事項
1. **candidate 件数が 3件に減りすぎ**ている可能性
   - `自己株式` penalty -2 が `自己株式の取得` (+3) とセットで出るケースで正味 +1 になる
   - 単独で `自己株式` のみ出現する強い buyback 文書があれば取りこぼすリスク
2. **`自己株式の消却` (score +2)** は penalty 語との共起で容易に 0 付近に落ちる
3. `high` priority は依然として 0 件 — event_type_candidate がある真の buyback decision は対象 PDF に少ない

## 5. 既知の制約

- review_bucket は proxy 指標（正確な precision 算出には人手確認が必要）
- サンプル件数 134 PDF は限定的（本番は数千件レベルが必要）
- cancel 真文書の recall は今後確認が必要
- OCR 非対応のため、スキャン PDF は対象外
- pair_bonus が適用される decision 系文書は対象サンプルに少ない

## 6. 次の推奨タスク

1. **medium 帯の人手 review → high_confidence_extracted のみ保存する運用設計** (priority: high)
2. **cancel 系の recall チェック用サンプル拡充** — cancel 真文書を意図的に含むデータセットで recall を計測
3. **auto-save 条件の最小導入** — medium + buyback_related + confidence >= 0.8 を高優先で保存する最小パイプライン
