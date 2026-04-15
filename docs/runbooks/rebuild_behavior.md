# Rebuild サブコマンドの挙動仕様

## 概要

`pipeline_run.py rebuild` は viewer 用 serving テーブルの整合性チェック・再構築を行う。
2つのモードがある。

## モード

### 1. Queue 消化モード (デフォルト)

```bash
python tools/pipeline_run.py rebuild
```

- `rebuild_queue` テーブルの `status=pending` を取得
- 各 ticker について `financials` / `segment_canonical` の重複チェックを実行
- 処理後、queue の status を `done` / `failed` に更新
- `pipeline_runs` に実行ログを記録

### 2. Ticker 直指定モード

```bash
python tools/pipeline_run.py rebuild --ticker 6750
```

- 指定 ticker のみ rebuild を実行
- **`rebuild_queue` は消化しない** — 既存 pending 行はそのまま残る
- `pipeline_runs` には記録される
- 主に手動デバッグや単体検証用

## pipeline_runs 記録

全サブコマンド (ingest/process/rebuild/notify/reconcile/retry-failed/backfill) および全体実行で `pipeline_runs` テーブルにログが記録される。

| フィールド | 内容 |
|:---|:---|
| job_type | サブコマンド名 ("rebuild", "ingest" 等) |
| trigger_type | "manual" / "scheduler" / "retry" / "reconcile" |
| status | "running" → "done" / "failed" |
| started_at | 実行開始時刻 (JST) |
| finished_at | 実行完了時刻 (JST) |
| duration_sec | 実行時間 (秒) |
| processed_count | 処理した ticker 数 |
| success_count | 成功した ticker 数 |
| failed_count | 失敗した ticker 数 |

## rebuild_queue の状態遷移

```
pending → running → done
                  → failed
```

- `take_pending_rebuilds()` が pending → running に更新
- `complete_rebuild()` が running → done/failed に更新
- 更新失敗時は warning ログを出力 (本体処理は停止しない)

## エラーハンドリング

- Supabase API 通信エラーは warning ログを出力
- pipeline_runs INSERT/UPDATE 失敗は本体処理を停止しない
- rebuild_queue UPDATE 失敗は warning ログを出力し、本体処理は継続
