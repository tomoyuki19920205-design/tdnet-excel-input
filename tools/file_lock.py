#!/usr/bin/env python3
# ============================================================
# file_lock.py — ファイルベースの排他制御
# ============================================================
"""
Windows Task Scheduler 上で安全に使えるファイルロック機構。

使い方::

    from tools.file_lock import FileLock

    lock = FileLock("realtime", max_age_minutes=15)
    if not lock.acquire():
        print("別プロセスが実行中")
        sys.exit(0)
    try:
        # ... 処理 ...
    finally:
        lock.release()

    # またはコンテキストマネージャ:
    with FileLock("realtime", max_age_minutes=15) as lock:
        if not lock.acquired:
            sys.exit(0)
        # ... 処理 ...

グローバルロック + ジョブロック の二段構成::

    global_lock = FileLock("tdnet_pipeline", max_age_minutes=60)
    job_lock    = FileLock("realtime",       max_age_minutes=15)
"""
from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("pipeline.lock")

JST = timezone(timedelta(hours=9))

# デフォルトのロックディレクトリ
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_STATE_DIR = str(_PROJECT_ROOT / "state" / "locks")


# ============================================================
# プロセス存在チェック (Windows / POSIX 両対応)
# ============================================================

def process_exists(pid: int) -> bool:
    """指定 PID のプロセスが存在するか確認する。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: kernel32.OpenProcess で確認
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            # POSIX: signal 0 で確認
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False
    except Exception:
        # 不明なエラー → 安全側に倒して True 扱い
        return True


def process_matches(pid: int, expected_script: str) -> bool:
    """PID のコマンドラインが期待するスクリプト名を含むか確認する。

    確認できない場合は True を返す（安全側に倒す）。
    """
    if not process_exists(pid):
        return False
    try:
        if os.name == "nt":
            import subprocess
            result = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}",
                 "get", "CommandLine", "/format:list"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            cmdline = result.stdout.strip()
            if not cmdline or "No Instance" in cmdline:
                # WMIC で取得できない → プロセスは存在するが
                # コマンドラインを取得できない → 安全側に倒す
                return True
            return expected_script.lower() in cmdline.lower()
        else:
            # Linux/Mac: /proc/{pid}/cmdline
            cmdline_path = f"/proc/{pid}/cmdline"
            if os.path.exists(cmdline_path):
                with open(cmdline_path, "r") as f:
                    cmdline = f.read()
                return expected_script.lower() in cmdline.lower()
            return True  # 確認できない → 安全側
    except Exception:
        return True  # エラー → 安全側に倒す


def read_lock_metadata(lock_path: str) -> dict[str, Any]:
    """ロックファイルのメタデータを読み込む。"""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return {}


# ============================================================
# FileLock
# ============================================================

class FileLock:
    """ファイルベースの排他制御。

    Args:
        name: ロック名（拡張子なし）。``state/locks/{name}.lock`` になる。
        max_age_minutes: stale 判定の閾値（分）。この時間を超えた
            lock はプロセス存在確認の上で自動解除候補になる。
        state_dir: ロックファイルの保存ディレクトリ。
    """

    def __init__(
        self,
        name: str,
        max_age_minutes: int = 15,
        state_dir: str = _DEFAULT_STATE_DIR,
    ) -> None:
        self.name = name
        self.max_age_minutes = max_age_minutes
        self.state_dir = state_dir
        self.lock_path = os.path.join(state_dir, f"{name}.lock")
        self.acquired = False
        self._my_pid = os.getpid()

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    def acquire(self) -> bool:
        """ロックを取得する。

        Returns:
            True: ロック取得成功
            False: 他プロセスが保持中のため取得失敗
        """
        os.makedirs(self.state_dir, exist_ok=True)

        # 既存ロックの確認
        if os.path.exists(self.lock_path):
            if self._handle_existing_lock():
                # stale として解除済み → 再取得を試みる
                pass
            else:
                # 有効なロックが存在する
                return False

        # ロック取得: atomic に近い形で書き込み
        try:
            meta = self._build_metadata()
            # O_CREAT | O_EXCL で他プロセスとの競合を防ぐ
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            try:
                os.write(fd, json.dumps(meta, indent=2).encode("utf-8"))
            finally:
                os.close(fd)

            self.acquired = True
            logger.info(
                f"[lock] acquired: {self.name} "
                f"(pid={self._my_pid}, path={self.lock_path})"
            )
            return True

        except FileExistsError:
            # 別プロセスが先に作成した → 取得失敗
            logger.info(
                f"[lock] acquire FAILED (race): {self.name} "
                f"(pid={self._my_pid})"
            )
            return False
        except OSError as e:
            logger.error(
                f"[lock] acquire ERROR: {self.name} error={e}"
            )
            return False

    def release(self) -> None:
        """ロックを解放する。自分が保持しているロックのみ削除する。"""
        if not self.acquired:
            return

        try:
            # 自分が保持しているか確認
            meta = read_lock_metadata(self.lock_path)
            if meta.get("pid") == self._my_pid:
                os.remove(self.lock_path)
                logger.info(
                    f"[lock] released: {self.name} (pid={self._my_pid})"
                )
            else:
                logger.warning(
                    f"[lock] release SKIPPED: {self.name} "
                    f"lock owned by pid={meta.get('pid')}, "
                    f"self pid={self._my_pid}"
                )
        except FileNotFoundError:
            # 既に削除済み
            pass
        except OSError as e:
            logger.error(f"[lock] release ERROR: {self.name} error={e}")
        finally:
            self.acquired = False

    def is_stale(self) -> bool:
        """現在のロックが stale（陳腐化）しているか確認する。

        以下の条件を全て満たす場合に stale と判定:
        1. max_age_minutes を超過している
        2. PID が存在しない or PID が対象ジョブのプロセスではない
        """
        if not os.path.exists(self.lock_path):
            return False

        meta = read_lock_metadata(self.lock_path)
        if not meta:
            # メタデータ読み取り不可 → stale とみなす
            return True

        # 時刻チェック
        started_at = meta.get("started_at", "")
        if started_at:
            try:
                lock_time = datetime.fromisoformat(started_at)
                now = datetime.now(JST)
                age_minutes = (now - lock_time).total_seconds() / 60
                if age_minutes <= self.max_age_minutes:
                    # まだ有効期限内
                    return False
            except (ValueError, TypeError):
                pass  # パース失敗 → 時刻チェックをスキップ

        # 時刻超過 → PIDチェック
        lock_pid = meta.get("pid", 0)
        script_name = meta.get("script_name", "")

        if not process_exists(lock_pid):
            logger.info(
                f"[lock] stale detected: {self.name} "
                f"pid={lock_pid} (not running)"
            )
            return True

        if script_name and not process_matches(lock_pid, script_name):
            logger.info(
                f"[lock] stale detected: {self.name} "
                f"pid={lock_pid} (different process)"
            )
            return True

        # PID存在 & スクリプト名一致 → まだ動いている可能性（staleではない）
        logger.info(
            f"[lock] NOT stale: {self.name} "
            f"pid={lock_pid} still running (age exceeded but process alive)"
        )
        return False

    def force_release(self) -> bool:
        """強制ロック解除（stale判定済みの場合に使用）。

        Returns:
            True: 解除成功
            False: 解除失敗
        """
        try:
            if os.path.exists(self.lock_path):
                meta = read_lock_metadata(self.lock_path)
                os.remove(self.lock_path)
                logger.warning(
                    f"[lock] force_released: {self.name} "
                    f"(was pid={meta.get('pid')}, "
                    f"started={meta.get('started_at')})"
                )
                return True
        except OSError as e:
            logger.error(f"[lock] force_release ERROR: {self.name} error={e}")
        return False

    def get_info(self) -> dict[str, Any]:
        """現在のロック情報を返す。"""
        if not os.path.exists(self.lock_path):
            return {"exists": False, "name": self.name}
        meta = read_lock_metadata(self.lock_path)
        meta["exists"] = True
        meta["name"] = self.name
        meta["is_stale"] = self.is_stale()
        return meta

    # ----------------------------------------------------------
    # Context Manager
    # ----------------------------------------------------------

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.release()
        return False

    # ----------------------------------------------------------
    # Private
    # ----------------------------------------------------------

    def _build_metadata(self) -> dict[str, Any]:
        """ロックファイルに書き込むメタデータを構築する。"""
        return {
            "pid": self._my_pid,
            "hostname": platform.node(),
            "started_at": datetime.now(JST).isoformat(),
            "script_name": _get_script_name(),
            "command_line": " ".join(sys.argv),
            "lock_name": self.name,
        }

    def _handle_existing_lock(self) -> bool:
        """既存ロックファイルを処理する。

        Returns:
            True: stale として解除した（再取得可能）
            False: 有効なロックが存在（取得不可）
        """
        meta = read_lock_metadata(self.lock_path)
        lock_pid = meta.get("pid", 0)

        # 1. stale チェック
        if self.is_stale():
            self.force_release()
            return True

        # 2. 有効なロックが存在（同一PIDでも拒否 — 別インスタンスの可能性）
        logger.info(
            f"[lock] already held: {self.name} "
            f"(pid={lock_pid}, started={meta.get('started_at')}, "
            f"hostname={meta.get('hostname')})"
        )
        return False

    def __repr__(self) -> str:
        status = "acquired" if self.acquired else "released"
        return f"FileLock({self.name}, {status}, path={self.lock_path})"


# ============================================================
# ヘルパー
# ============================================================

def _get_script_name() -> str:
    """実行中スクリプトのファイル名を返す。"""
    if sys.argv and sys.argv[0]:
        return os.path.basename(sys.argv[0])
    return "unknown"


def acquire_dual_lock(
    job_name: str,
    *,
    global_max_age: int = 60,
    job_max_age: int = 15,
    state_dir: str = _DEFAULT_STATE_DIR,
) -> tuple[FileLock, FileLock] | None:
    """グローバルロック + ジョブロックの二段取得。

    両方取得できた場合のみタプルを返す。
    どちらか失敗した場合は取得済みロックを解放して None を返す。

    Args:
        job_name: ジョブ名 (realtime / nightly / reconcile)
        global_max_age: グローバルロックの stale 閾値（分）
        job_max_age: ジョブロックの stale 閾値（分）

    Returns:
        (global_lock, job_lock) or None
    """
    global_lock = FileLock(
        "tdnet_pipeline",
        max_age_minutes=global_max_age,
        state_dir=state_dir,
    )
    job_lock = FileLock(
        job_name,
        max_age_minutes=job_max_age,
        state_dir=state_dir,
    )

    # 1. グローバルロック取得
    if not global_lock.acquire():
        logger.info(
            f"[lock] dual acquire FAILED: global lock held "
            f"(job={job_name})"
        )
        return None

    # 2. ジョブロック取得
    if not job_lock.acquire():
        logger.info(
            f"[lock] dual acquire FAILED: job lock held "
            f"(job={job_name})"
        )
        global_lock.release()
        return None

    logger.info(
        f"[lock] dual acquire OK: global + {job_name} "
        f"(pid={os.getpid()})"
    )
    return global_lock, job_lock


def release_dual_lock(
    global_lock: FileLock,
    job_lock: FileLock,
) -> None:
    """二段ロックを安全に解放する。"""
    try:
        job_lock.release()
    except Exception as e:
        logger.error(f"[lock] job lock release error: {e}")
    try:
        global_lock.release()
    except Exception as e:
        logger.error(f"[lock] global lock release error: {e}")


# ============================================================
# CLI (診断用)
# ============================================================

def _cli_main() -> None:
    """ロック状態の確認・強制解除用CLI。"""
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="File lock management")
    parser.add_argument("action", choices=["status", "force-release", "test"],
                        help="アクション")
    parser.add_argument("--name", type=str, default=None,
                        help="ロック名 (未指定で全ロック)")
    args = parser.parse_args()

    lock_names = ["tdnet_pipeline", "realtime", "nightly", "reconcile"]
    if args.name:
        lock_names = [args.name]

    if args.action == "status":
        print()
        print("=" * 50)
        print("  LOCK STATUS")
        print("=" * 50)
        for name in lock_names:
            lock = FileLock(name)
            info = lock.get_info()
            exists = info.get("exists", False)
            stale = info.get("is_stale", False)
            icon = "[LOCKED]" if exists else "[FREE]"
            if exists and stale:
                icon = "[STALE]"
            pid = info.get("pid", "-")
            started = info.get("started_at", "-")
            print(f"  {name:25s}: {icon} pid={pid} started={started}")
        print("=" * 50)

    elif args.action == "force-release":
        for name in lock_names:
            lock = FileLock(name)
            if os.path.exists(lock.lock_path):
                lock.force_release()
                print(f"  {name}: force released")
            else:
                print(f"  {name}: no lock file")

    elif args.action == "test":
        print("  Testing lock acquire/release...")
        name = args.name or "test_lock"
        lock = FileLock(name, max_age_minutes=1)
        ok = lock.acquire()
        print(f"  acquire({name}): {ok}")
        if ok:
            info = lock.get_info()
            print(f"  info: pid={info.get('pid')} started={info.get('started_at')}")
            lock.release()
            print(f"  release({name}): done")
        print("  Test complete.")


if __name__ == "__main__":
    _cli_main()
