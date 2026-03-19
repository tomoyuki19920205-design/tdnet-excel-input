#!/usr/bin/env python3
"""run_buyback_pipeline.py — buyback 一括実行ラッパー

scanner → review → export → save を1コマンドで順番に安全実行する。
デフォルトは dry-run。--live-save で実保存。

Usage:
  cd "C:\\Users\\takuy\\OneDrive\\tdnet-excel-input"
  .\\.venv\\Scripts\\python.exe tools/run_buyback_pipeline.py \\
    --input-dir data/docs --rules configs/buyback_scanner_rules.json \\
    --db data/decision_db.db --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows cp932 対策
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("buyback_pipeline")

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)

STEPS = ("candidates", "review", "operation", "save")

# ============================================================
# ステップ状態
# ============================================================
class StepResult:
    def __init__(self, name: str, order: int):
        self.name = name
        self.order = order
        self.status = "pending"
        self.started_at = ""
        self.finished_at = ""
        self.duration_sec = 0.0
        self.command = ""
        self.output_dir = ""
        self.key_output_file = ""
        self.row_count = 0
        self.error_message = ""

    def to_dict(self) -> dict:
        return {
            "step_name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": f"{self.duration_sec:.1f}",
            "command": self.command,
            "output_dir": self.output_dir,
            "key_output_file": self.key_output_file,
            "row_count": self.row_count,
            "error_message": self.error_message,
        }


def _now_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _csv_row_count(path: str) -> int:
    """CSV ファイルの行数 (ヘッダ除く)"""
    if not os.path.isfile(path):
        return 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


def _find_python() -> str:
    """現在の python.exe パスを返す"""
    return sys.executable


# ============================================================
# ステップ実行
# ============================================================
def _run_step(
    step: StepResult,
    cmd: list[str],
    step_dir: str,
) -> bool:
    """subprocess でステップを実行する。成功なら True。"""
    Path(step_dir).mkdir(parents=True, exist_ok=True)
    step.command = " ".join(cmd)
    step.output_dir = step_dir
    step.started_at = _now_str()

    logger.info(f"[{step.name}] 開始")
    logger.debug(f"  cmd: {step.command}")

    t0 = time.time()
    stdout_path = os.path.join(step_dir, "stdout.log")
    stderr_path = os.path.join(step_dir, "stderr.log")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            cwd=PROJECT_ROOT,
            timeout=600,
        )

        # stdout/stderr を保存
        with open(stdout_path, "wb") as f:
            f.write(proc.stdout or b"")
        with open(stderr_path, "wb") as f:
            f.write(proc.stderr or b"")

        step.duration_sec = time.time() - t0
        step.finished_at = _now_str()

        if proc.returncode != 0:
            step.status = "failed"
            err_text = (proc.stderr or b"").decode("utf-8", errors="replace")[-500:]
            step.error_message = f"exit={proc.returncode}: {err_text}"
            logger.error(f"[{step.name}] 失敗 (exit={proc.returncode})")
            return False

        step.status = "success"
        logger.info(f"[{step.name}] 完了 ({step.duration_sec:.1f}s)")
        return True

    except subprocess.TimeoutExpired:
        step.duration_sec = time.time() - t0
        step.finished_at = _now_str()
        step.status = "failed"
        step.error_message = "timeout (600s)"
        logger.error(f"[{step.name}] タイムアウト")
        return False
    except Exception as e:
        step.duration_sec = time.time() - t0
        step.finished_at = _now_str()
        step.status = "failed"
        step.error_message = str(e)
        logger.error(f"[{step.name}] 例外: {e}")
        return False


# ============================================================
# パイプライン
# ============================================================
def run_pipeline(opts) -> tuple[list[StepResult], str]:
    """パイプラインを実行し、ステップ結果リストと run_dir を返す。"""
    py = _find_python()
    run_id = opts.run_id or datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(opts.output_root, run_id)
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    stop_idx = STEPS.index(opts.stop_after) if opts.stop_after in STEPS else len(STEPS) - 1

    results: list[StepResult] = []

    # --- Step 1: candidates ---
    s1 = StepResult("candidates", 1)
    results.append(s1)
    if 0 <= stop_idx:
        s1_dir = os.path.join(run_dir, "01_candidates")
        cmd = [
            py,
            os.path.join(TOOLS_DIR, "find_buyback_candidate_docs.py"),
            "--input-dir", opts.input_dir,
            "--output-dir", s1_dir,
        ]
        if opts.rules:
            cmd += ["--rules", opts.rules]
        if opts.recursive:
            cmd += ["--recursive"]
        if opts.limit:
            cmd += ["--limit", str(opts.limit)]
        if opts.verbose:
            cmd += ["--verbose"]

        ok = _run_step(s1, cmd, s1_dir)
        manifest_path = os.path.join(s1_dir, "candidate_manifest.csv")
        s1.key_output_file = manifest_path
        s1.row_count = _csv_row_count(manifest_path)
        if not ok:
            return results, run_dir
    else:
        s1.status = "skipped"

    # --- Step 2: review ---
    s2 = StepResult("review", 2)
    results.append(s2)
    if 1 <= stop_idx:
        manifest_path = s1.key_output_file
        if not os.path.isfile(manifest_path):
            s2.status = "failed"
            s2.error_message = f"manifest not found: {manifest_path}"
            return results, run_dir

        s2_dir = os.path.join(run_dir, "02_review")
        cmd = [
            py,
            os.path.join(TOOLS_DIR, "review_buyback_extraction.py"),
            "--manifest", manifest_path,
            "--only-manifest-files",
            "--input-dir", opts.input_dir,
            "--output-dir", s2_dir,
            "--min-confidence", str(opts.min_confidence),
        ]
        if opts.limit:
            cmd += ["--limit", str(opts.limit)]
        if opts.verbose:
            cmd += ["--verbose"]

        ok = _run_step(s2, cmd, s2_dir)
        review_csv = os.path.join(s2_dir, "review_buyback_results.csv")
        s2.key_output_file = review_csv
        s2.row_count = _csv_row_count(review_csv)
        if not ok:
            return results, run_dir
    else:
        s2.status = "skipped"

    # --- Step 3: operation (export) ---
    s3 = StepResult("operation", 3)
    results.append(s3)
    if 2 <= stop_idx:
        review_csv = s2.key_output_file
        if not os.path.isfile(review_csv):
            s3.status = "failed"
            s3.error_message = f"review CSV not found: {review_csv}"
            return results, run_dir

        s3_dir = os.path.join(run_dir, "03_operation")
        cmd = [
            py,
            os.path.join(TOOLS_DIR, "export_buyback_save_candidates.py"),
            "--review", review_csv,
            "--output-dir", s3_dir,
            "--min-confidence", str(opts.min_confidence),
            "--min-core-fields", str(opts.min_core_fields),
            "--include-priority", "all",
        ]
        if opts.verbose:
            cmd += ["--verbose"]

        ok = _run_step(s3, cmd, s3_dir)
        save_csv = os.path.join(s3_dir, "review_save_candidates.csv")
        s3.key_output_file = save_csv
        s3.row_count = _csv_row_count(save_csv)
        if not ok:
            return results, run_dir
    else:
        s3.status = "skipped"

    # --- Step 4: save ---
    s4 = StepResult("save", 4)
    results.append(s4)
    if 3 <= stop_idx and not opts.skip_save:
        save_csv = s3.key_output_file
        if not os.path.isfile(save_csv) or _csv_row_count(save_csv) == 0:
            s4.status = "skipped"
            s4.error_message = "save_candidates なし (0行)"
            logger.info("[save] 保存候補なし — スキップ")
        else:
            s4_dir = os.path.join(run_dir, "04_save")
            cmd = [
                py,
                os.path.join(TOOLS_DIR, "save_buyback_candidates_to_db.py"),
                "--input", save_csv,
                "--db", opts.db,
                "--output-dir", s4_dir,
            ]
            if not opts.live_save:
                cmd += ["--dry-run"]
            if opts.verbose:
                cmd += ["--verbose"]

            ok = _run_step(s4, cmd, s4_dir)
            summary_md = os.path.join(s4_dir, "save_to_db_summary.md")
            s4.key_output_file = summary_md
            s4.row_count = _csv_row_count(save_csv)
    else:
        s4.status = "skipped"
        if opts.skip_save:
            s4.error_message = "--skip-save"

    return results, run_dir


# ============================================================
# 出力
# ============================================================
def write_step_status_csv(results: list[StepResult], run_dir: str) -> str:
    path = os.path.join(run_dir, "pipeline_step_status.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "step_name", "status", "started_at", "finished_at",
            "duration_sec", "command", "output_dir", "key_output_file",
            "row_count", "error_message",
        ])
        w.writeheader()
        for r in results:
            w.writerow(r.to_dict())
    return path


def write_pipeline_summary(
    results: list[StepResult],
    run_dir: str,
    opts,
    run_id: str,
) -> str:
    now = _now_str()
    mode = "**LIVE SAVE**" if opts.live_save else "**DRY-RUN**"

    # gather step info
    step_table = []
    for r in results:
        step_table.append(
            f"| {r.name} | {r.status} | {r.row_count} | {r.duration_sec:.1f}s |"
        )

    lines = [
        "# Buyback Pipeline — Summary",
        "",
        f"- **実行時刻**: {now}",
        f"- **run_id**: `{run_id}`",
        f"- **モード**: {mode}",
        f"- **入力**: `{opts.input_dir}`",
        f"- **rules**: `{opts.rules}`",
        f"- **DB**: `{opts.db}`",
        f"- **stop-after**: {opts.stop_after or 'save'}",
        "",
        "## ステップ結果",
        "",
        "| Step | Status | Rows | Duration |",
        "|:---|:---|---:|---:|",
    ]
    lines.extend(step_table)
    lines.append("")

    # 所見
    lines.append("## 所見")
    lines.append("")
    s1 = next((r for r in results if r.name == "candidates"), None)
    s2 = next((r for r in results if r.name == "review"), None)
    s3 = next((r for r in results if r.name == "operation"), None)
    s4 = next((r for r in results if r.name == "save"), None)

    if s1 and s1.status == "success":
        lines.append(f"- candidate scanner: **{s1.row_count}** 件")
    if s2 and s2.status == "success":
        lines.append(f"- review: **{s2.row_count}** 件")
    if s3 and s3.status == "success":
        lines.append(f"- save candidates: **{s3.row_count}** 件")
    if s4:
        if s4.status == "success":
            lines.append(f"- DB save: 完了 ({mode})")
        elif s4.status == "skipped":
            lines.append(f"- DB save: スキップ ({s4.error_message})")

    failed = [r for r in results if r.status == "failed"]
    if failed:
        lines.append("")
        lines.append("> [!WARNING]")
        for f in failed:
            lines.append(f"> {f.name} が失敗: {f.error_message[:200]}")

    if not opts.live_save:
        lines.append("")
        lines.append("> [!NOTE]")
        lines.append("> dry-run のため DB 書き込みは未実施。`--live-save` で実保存。")

    lines.append("")
    md = "\n".join(lines)
    path = os.path.join(run_dir, "pipeline_summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


# ============================================================
# メイン
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="buyback pipeline — scanner / review / export / save 一括実行",
    )
    parser.add_argument("--input-dir", default="data/docs",
                        help="PDF 入力ディレクトリ")
    parser.add_argument("--rules", default="configs/buyback_scanner_rules.json",
                        help="scanner rules JSON")
    parser.add_argument("--db", default="data/decision_db.db",
                        help="buyback_events DB パス")
    parser.add_argument("--output-root",
                        default="artifacts/buyback_pipeline_runs",
                        help="run 出力ルート")
    parser.add_argument("--min-confidence", type=float, default=0.60,
                        help="confidence 閾値")
    parser.add_argument("--min-core-fields", type=int, default=1,
                        help="core fields 最小数")
    parser.add_argument("--recursive", action="store_true", default=True,
                        help="再帰走査 (default)")
    parser.add_argument("--no-recursive", action="store_false", dest="recursive",
                        help="再帰走査しない")
    parser.add_argument("--limit", type=int, default=0, help="処理上限")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="DB 保存を dry-run (default)")
    parser.add_argument("--live-save", action="store_true", default=False,
                        help="DB 実保存 (明示必須)")
    parser.add_argument("--skip-save", action="store_true",
                        help="Step4 をスキップ")
    parser.add_argument("--stop-after",
                        choices=["candidates", "review", "operation", "save"],
                        default="save", help="指定ステップで停止")
    parser.add_argument("--run-id", default="", help="カスタム run_id")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    level = logging.DEBUG if opts.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # live-save 時は dry-run を off
    if opts.live_save:
        opts.dry_run = False

    run_id = opts.run_id or datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    opts.run_id = run_id
    mode = "LIVE SAVE" if opts.live_save else "DRY-RUN"
    logger.info(f"=== Buyback Pipeline ({mode}) ===")
    logger.info(f"run_id: {run_id}")

    results, run_dir = run_pipeline(opts)

    # 出力
    write_step_status_csv(results, run_dir)
    write_pipeline_summary(results, run_dir, opts, run_id)

    # コンソール出力
    print()
    print(f"  run_id: {run_id}")
    print(f"  mode: {mode}")
    for r in results:
        marker = "OK" if r.status == "success" else r.status.upper()
        print(f"  [{marker:7s}] {r.name:12s}  rows={r.row_count}  {r.duration_sec:.1f}s")
    print(f"  output: {run_dir}")
    print()

    failed = any(r.status == "failed" for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
