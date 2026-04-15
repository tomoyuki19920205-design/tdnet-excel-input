# Webアプリ自動更新パイプライン アーキテクチャ

> 更新日: 2026-03-11

## 概要

TDnet / J-Quants 等から取得した財務データを Supabase canonical tables に正規化し、
viewer 用 serving tables を再構築して COMPANYVIEW に反映する自動更新パイプライン。

## 4層アーキテクチャ

```
A. ingest層    B. process層      C. serving層        D. app層
TDnet → raw    raw → canonical   canonical → viewer  viewer → 表示
EDINET → raw   正規化/抽出       financials 更新     メモ保存
J-Quants       quarantine 判定   segment 更新        手動修正
```

## データフロー

```
[TDnet API] → filings_ingest → SQLite (raw)
                                   ↓
                              filings_process → Supabase financials / segment_canonical
                                   ↓
                              rebuild_serving_views → viewer 整合性チェック
                                   ↓
                              notify_updates → Discord 通知
```

## 正本定義

| 層 | テーブル | 正本 |
|:---|:---|:---|
| 数値正本 (Phase 1) | `financials` | 暫定 serving (将来 canonical_financials に移行) |
| 数値正本 (Phase 1) | `segment_canonical` | canonical + serving 兼用 |
| 表示正本 | viewer が参照するテーブル | Phase 2 で viewer_* に分離 |
| **Excel は正本にしない** | | export/確認用途に限定 |

## 運用系テーブル

| テーブル | 目的 |
|:---|:---|
| `pipeline_runs` | バッチ実行ログ (trigger_type 付き) |
| `job_queue` | 疑似ジョブキュー (重複防止付き) |
| `rebuild_queue` | ticker 再構築キュー (重複防止付き) |
| `quarantine_items` | 抽出失敗隔離 |
| `data_quality_issues` | 品質警告 |

## CLI

```
python tools/pipeline_run.py              # 全ステップ
python tools/pipeline_run.py ingest
python tools/pipeline_run.py process
python tools/pipeline_run.py rebuild
python tools/pipeline_run.py notify
python tools/pipeline_run.py reconcile
python tools/pipeline_run.py retry-failed
python tools/pipeline_run.py backfill --from YYYY-MM-DD --to YYYY-MM-DD
```

## 終了コード

| コード | 意味 |
|---:|:---|
| 0 | 正常 |
| 1 | ingest/process 失敗 |
| 2 | rebuild 失敗 |
| 3 | reconcile 重大異常 |

## 同時実行防止

`pipeline_runs` で同一 `job_type` の `running` 行 (1時間以内) があれば自動 skip。

## Phase 2 予定

- `canonical_financials` (long 形式) 導入
- `viewer_financials_latest` 新設
- segment canonical / serving 分離
- manual_overrides 反映
- VPS / cron 移行
