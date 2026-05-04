#!/usr/bin/env python3
r"""tools/inspect_task_scheduler.py — Windows タスクスケジューラ棚卸し (読み取り専用)

Usage:
    cd "C:\Users\takuy\OneDrive\tdnet-excel-input"
    python -X utf8 .\tools\inspect_task_scheduler.py
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── 設定 ────────────────────────────────────────────────────
KEYWORDS = [
    "tdnet", "backfill", "ingest", "segment",
    "decision", "nightly", "realtime", "reconcile",
]
OUT_DIR = Path(__file__).resolve().parent.parent / "out"
OUT_CSV = OUT_DIR / "task_scheduler_audit.csv"
OUT_TXT = OUT_DIR / "task_scheduler_audit.txt"

CSV_COLUMNS = [
    "TaskName", "TaskPath", "State", "LastRunTime", "LastTaskResult",
    "NextRunTime", "Triggers", "Actions", "WorkingDirectory", "Arguments", "error",
]

# ─── PowerShell スクリプト ────────────────────────────────────
# Get-ScheduledTask と Get-ScheduledTaskInfo を結合して JSON で返す
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$tasks = Get-ScheduledTask
$result = @()
foreach ($t in $tasks) {
    try {
        $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath
    } catch {
        $info = $null
    }
    $triggers = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName + ':' + $_.StartBoundary }) -join '; '
    $actions  = ($t.Actions  | ForEach-Object {
        $a = $_
        $cls = $a.CimClass.CimClassName
        if ($cls -eq 'MSFT_TaskExecAction') {
            "$($a.Execute) $($a.Arguments)"
        } elseif ($cls -eq 'MSFT_TaskComHandlerAction') {
            "COM:$($a.ClassId)"
        } else {
            $cls
        }
    }) -join '; '
    $workDir = ($t.Actions | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskExecAction' } | Select-Object -First 1 -ExpandProperty WorkingDirectory)
    $args_str = ($t.Actions | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskExecAction' } | Select-Object -First 1 -ExpandProperty Arguments)
    $obj = [PSCustomObject]@{
        TaskName        = $t.TaskName
        TaskPath        = $t.TaskPath
        State           = $t.State.ToString()
        LastRunTime     = if ($info) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
        LastTaskResult  = if ($info) { $info.LastTaskResult.ToString() } else { '' }
        NextRunTime     = if ($info) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }
        Triggers        = $triggers
        Actions         = $actions
        WorkingDirectory = $workDir
        Arguments       = $args_str
    }
    $result += $obj
}
$result | ConvertTo-Json -Depth 3 -Compress
"""


def _run_ps(script: str) -> str:
    """PowerShell スクリプトを実行して stdout を返す。"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    return result.stdout.strip()


def _to_str(v) -> str:
    """任意型を安全に str 変換する。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _matches_keywords(task: dict) -> bool:
    """任意フィールドにキーワードが含まれるか判定（大文字小文字無視）。"""
    targets = " ".join([
        _to_str(task.get("TaskName")),
        _to_str(task.get("TaskPath")),
        _to_str(task.get("Actions")),
        _to_str(task.get("Arguments")),
        _to_str(task.get("WorkingDirectory")),
    ]).lower()
    return any(kw.lower() in targets for kw in KEYWORDS)


def main() -> None:
    print("[audit] PowerShell からタスク一覧を取得中...")
    try:
        raw = _run_ps(_PS_SCRIPT)
    except subprocess.TimeoutExpired:
        print("[ERROR] PowerShell タイムアウト", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] PowerShell 実行失敗: {e}", file=sys.stderr)
        sys.exit(1)

    if not raw:
        print("[ERROR] PowerShell から出力なし", file=sys.stderr)
        sys.exit(1)

    # JSON パース
    try:
        all_tasks: list[dict] = json.loads(raw)
        if isinstance(all_tasks, dict):
            all_tasks = [all_tasks]  # タスク1件の場合はオブジェクトになる
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON パース失敗: {e}", file=sys.stderr)
        print(f"  raw[:500]: {raw[:500]}", file=sys.stderr)
        sys.exit(1)

    # str 変換（Actions/Arguments が dict/list で返る場合に対応）
    for t in all_tasks:
        for key in ("Actions", "Arguments", "WorkingDirectory", "Triggers",
                    "TaskName", "TaskPath", "State",
                    "LastRunTime", "LastTaskResult", "NextRunTime"):
            t[key] = _to_str(t.get(key))

    # キーワードフィルタ
    matched: list[dict] = [t for t in all_tasks if _matches_keywords(t)]

    # ─── 集計 ──
    enabled  = [t for t in matched if t.get("State", "").lower() == "ready"]
    disabled = [t for t in matched if t.get("State", "").lower() in ("disabled", "unknown")]
    failed   = [t for t in matched if t.get("LastTaskResult", "0") not in ("0", "267009", "")]
    backfill_callers = [
        t for t in matched
        if "backfill_segments_tdnet" in (t.get("Actions", "") + t.get("Arguments", "")).lower()
    ]

    # ─── CSV 出力 ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for t in matched:
            row = {k: t.get(k, "") for k in CSV_COLUMNS}
            row["error"] = ""
            writer.writerow(row)

    # ─── TXT 出力 ──
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"  TDNet タスクスケジューラ棚卸し  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    lines.append("=" * 70)
    lines.append(f"  全タスク数     : {len(all_tasks)}")
    lines.append(f"  抽出タスク数   : {len(matched)}")
    lines.append(f"  有効 (Ready)   : {len(enabled)}")
    lines.append(f"  無効/その他    : {len(disabled)}")
    lines.append("")

    lines.append("── 抽出タスク詳細 " + "-" * 50)
    for t in matched:
        lines.append("")
        lines.append(f"  TaskName       : {t.get('TaskName', '')}")
        lines.append(f"  TaskPath       : {t.get('TaskPath', '')}")
        lines.append(f"  State          : {t.get('State', '')}")
        lines.append(f"  LastRunTime    : {t.get('LastRunTime', '')}")
        lines.append(f"  LastTaskResult : {t.get('LastTaskResult', '')}")
        lines.append(f"  NextRunTime    : {t.get('NextRunTime', '')}")
        lines.append(f"  Triggers       : {t.get('Triggers', '')}")
        lines.append(f"  Actions        : {t.get('Actions', '')}")
        lines.append(f"  WorkingDir     : {t.get('WorkingDirectory', '')}")
        lines.append(f"  Arguments      : {t.get('Arguments', '')}")

    lines.append("")
    lines.append("── LastTaskResult が 0 以外 " + "-" * 40)
    if failed:
        for t in failed:
            lines.append(f"  [{t.get('LastTaskResult')}] {t.get('TaskName')}  ({t.get('LastRunTime')})")
    else:
        lines.append("  なし")

    lines.append("")
    lines.append("── backfill_segments_tdnet.py を呼んでいるタスク " + "-" * 20)
    if backfill_callers:
        for t in backfill_callers:
            lines.append(f"  {t.get('TaskName')}")
            lines.append(f"    Actions  : {t.get('Actions', '')}")
            lines.append(f"    Arguments: {t.get('Arguments', '')}")
            lines.append(f"    WorkDir  : {t.get('WorkingDirectory', '')}")
            lines.append(f"    State    : {t.get('State', '')}")
    else:
        lines.append("  なし（キーワード一致なし）")

    lines.append("")
    lines.append("=" * 70)

    OUT_TXT.write_text("\n".join(lines), encoding="utf-8")

    # ─── 標準出力 ──
    print(f"\n[audit] 抽出タスク件数   : {len(matched)}")
    print(f"[audit] 有効タスク件数   : {len(enabled)}")
    print(f"[audit] 無効タスク件数   : {len(disabled)}")
    print()

    print("[audit] LastTaskResult が 0 以外のタスク:")
    if failed:
        for t in failed:
            print(f"  [{t.get('LastTaskResult')}] {t.get('TaskName')}  (最終実行: {t.get('LastRunTime')})")
    else:
        print("  なし")
    print()

    print("[audit] backfill_segments_tdnet.py を呼んでいるタスク:")
    if backfill_callers:
        for t in backfill_callers:
            print(f"  {t.get('TaskName')}")
            print(f"    State    : {t.get('State')}")
            print(f"    Actions  : {t.get('Actions', '')}")
            print(f"    Arguments: {t.get('Arguments', '')}")
            print(f"    WorkDir  : {t.get('WorkingDirectory', '')}")
    else:
        print("  なし（キーワード一致なし）")
    print()

    print(f"[audit] CSV: {OUT_CSV}")
    print(f"[audit] TXT: {OUT_TXT}")


if __name__ == "__main__":
    main()
