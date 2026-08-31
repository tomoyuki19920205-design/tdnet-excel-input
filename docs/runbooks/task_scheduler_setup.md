# Task Scheduler 設定手順（v2 — 3タスク統合構成）

> 更新日: 2026-03-23

## 概要

TDNET パイプラインは以下の **3系統** に統合されている。

| タスク名 | バッチ | スケジュール | 目的 |
|:---|:---|:---|:---|
| `TDNET_Realtime` | `wscript.exe` → hidden VBS → `pythonw.exe` → background launcher → `run_realtime.bat` | 平日 08:32-18:02, 10分間隔 | 日中軽量処理 |
| `TDNET_Nightly` | `run_nightly.bat` | 毎日 19:00 | 夜間重処理 |
| `TDNET_Reconcile` | `run_reconcile_scheduled.bat` | 毎日 18:35 | 取りこぼし修復 |

**禁止**: 以下は定期タスクとして登録しないこと。
- Ingest / Process / Notify / Rebuild 単体タスク
- MainPipeline
- Update_PM

`TDNET_Update_All` は手動専用（ロジック変更後の全再処理用）。

---

## 排他制御

ファイルロックによる二段制御:

1. **グローバルロック** (`state/locks/tdnet_pipeline.lock`)
2. **ジョブロック** (`state/locks/{realtime,nightly,reconcile}.lock`)

- 起動時に両方取得。取れなければ即終了（待機しない）
- Nightly 実行中は Realtime / Reconcile は走らない
- stale ロック（PID不在）は自動解除

ロック状態確認:
```powershell
.venv\Scripts\python.exe tools\file_lock.py status
```

---

## 処理内容

### Realtime（日中軽量）
1. 15秒遅延（TDNET反映待ち）
2. Ingest（増分のみ）
3. Process（realtime モード: PDF重解析スキップ）
4. Notify
5. 軽量 Reconcile（当日分）
- **8分上限**（deadline 超過で次ステップをスキップ）

### Nightly（夜間重処理）
1. Ingest 補完
2. Process（nightly モード: フル解析）
3. Reconcile 完全
4. Rebuild
- 時間制限なし

### Reconcile（補完専用）
1. 未処理検出 + 再投入
2. 失敗リトライ
- 18:35 実行（Realtime 非衝突）

---

## Task Scheduler 登録

### 自動登録
```powershell
# 新3タスク登録
powershell -ExecutionPolicy Bypass -File .\tools\register_tasks.ps1 -Mode Install

# 旧タスク無効化
powershell -ExecutionPolicy Bypass -File .\tools\register_tasks.ps1 -Mode DisableLegacy

# 新3タスク削除（ロールバック用）
powershell -ExecutionPolicy Bypass -File .\tools\register_tasks.ps1 -Mode Uninstall
```

### 確認
```powershell
schtasks /Query /TN TDNET_Realtime
schtasks /Query /TN TDNET_Nightly
schtasks /Query /TN TDNET_Reconcile
```

---

## ログ

Python 側で `logs/` に自動出力:

| ログファイル | 対応タスク |
|:---|:---|
| `logs/realtime_YYYYMMDD.log` | Realtime |
| `logs/realtime_launcher.jsonl` | Realtime background launcher start/end/error/return code、絶対bat pathとsanitized command argv |
| `logs/realtime_console_YYYYMMDD.log` | Realtime batch stdout/stderr capture |
| `logs/nightly_YYYYMMDD.log` | Nightly |
| `logs/reconcile_scheduled_YYYYMMDD.log` | Reconcile |

ログ形式: `key=value` 形式（grep 可能）

---

## 手動実行

```powershell
# Realtime (dry-run)
.venv\Scripts\python.exe tools\scheduler_realtime.py --dry-run

# Nightly (dry-run)
.venv\Scripts\python.exe tools\scheduler_nightly.py --dry-run

# Reconcile (dry-run)
.venv\Scripts\python.exe tools\scheduler_reconcile.py --dry-run

# 手動フル再処理（Update_All 相当）
.venv\Scripts\python.exe tools\pipeline_run.py --trigger manual
```

---

## 前提

- Windows Task Scheduler
- Python venv: `.venv\Scripts\python.exe`
- Realtime Task Action: `wscript.exe //B //NoLogo tools\run_tdnet_realtime_hidden.vbs .venv\Scripts\pythonw.exe <project-root> tools\run_tdnet_realtime_background.py`
- Background launcher: `cmd.exe`, `/d`, `/c`, `call`, `<absolute-run_realtime.bat>`を独立argvとして起動（`shell=False`、`CREATE_NO_WINDOW`、HTML/XML entity不使用）
- プロジェクト: `C:\Users\takuy\OneDrive\tdnet-excel-input`
