# 決算間市場・評価倍率分析

`src.analysis.earnings_market_intervals` は、決算持ち越し候補や既存スコアから独立した読み取り専用の分析レイヤーです。日次結果はオンデマンドで作り、既存の市場データ・財務データを更新しません。

```text
market_data (source=jquants) ─┐
jquants_financials_normalized ├─> analyze_earnings_market_intervals ─> JSON / CSV / Markdown
per_share_data ───────────────┘
```

`jquants_financials_normalized` から `*FinancialStatements` のみを正式決算イベントとして選び、訂正と予想修正だけの資料を境界から除外します。同一会計期間では訂正でない連結資料を優先し、次に最初の開示を使います。E2 の翌営業日から E1 の前営業日を期間A、E1 の翌営業日から as-of の最新取引日を期間Bとします。

株価リターン・ボラティリティ・ドローダウンは `adj_close`、評価倍率は `close` と、その時点の未調整株数基準へそろえた一株指標を使います。EPS、年間予想DPS、実績BPSは `per_share_data` の開示履歴から翌営業日を有効日としてas-of適用します。予想EPSが負またはゼロならPERは `NULL` とし、未開示をゼロとして扱いません。

分割時には、J-Quants の `adj_factor` を用いて過去の一株指標を `source_adj_factor / current_adj_factor` 倍して当日の `close` の株数基準にそろえます。出力には開示ID、有効日、coverage、不足理由、入力ハッシュ、固定閾値による説明タグを保存します。

実行例:

```powershell
python -m tools.analyze_earnings_market_intervals --tickers 1418,2337 --as-of 2026-07-14
python -m tools.benchmark_earnings_market_intervals --as-of 2026-07-14
```
