# Buyback Pipeline — Summary

- **実行時刻**: 2026-03-11 16:36:41
- **run_id**: `test_dryrun`
- **モード**: **DRY-RUN**
- **入力**: `c:\Users\takuy\OneDrive\tdnet-excel-input\data\docs`
- **rules**: `c:\Users\takuy\OneDrive\tdnet-excel-input\configs\buyback_scanner_rules.json`
- **DB**: `c:\Users\takuy\OneDrive\tdnet-excel-input\data\decision_db.db`
- **stop-after**: save

## ステップ結果

| Step | Status | Rows | Duration |
|:---|:---|---:|---:|
| candidates | success | 3 | 29.8s |
| review | success | 36 | 3.1s |
| operation | success | 1 | 0.1s |
| save | success | 1 | 0.1s |

## 所見

- candidate scanner: **3** 件
- review: **36** 件
- save candidates: **1** 件
- DB save: 完了 (**DRY-RUN**)

> [!NOTE]
> dry-run のため DB 書き込みは未実施。`--live-save` で実保存。
