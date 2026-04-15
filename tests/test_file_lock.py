#!/usr/bin/env python3
# ============================================================
# test_file_lock.py — FileLock 単体テスト
# ============================================================
"""
Usage:
    python -m pytest tests/test_file_lock.py -v
"""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── path setup ──
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.file_lock import (
    FileLock,
    acquire_dual_lock,
    release_dual_lock,
    process_exists,
    read_lock_metadata,
)


@pytest.fixture
def lock_dir(tmp_path):
    """テスト用のロックディレクトリ。"""
    d = tmp_path / "locks"
    d.mkdir()
    return str(d)


# ============================================================
# 基本テスト
# ============================================================

class TestFileLockBasics:
    """基本的なロック取得・解放"""

    def test_acquire_and_release(self, lock_dir):
        lock = FileLock("test_basic", max_age_minutes=5, state_dir=lock_dir)
        assert lock.acquire() is True
        assert lock.acquired is True
        assert os.path.exists(lock.lock_path)

        # メタデータ確認
        meta = read_lock_metadata(lock.lock_path)
        assert meta["pid"] == os.getpid()
        assert meta["lock_name"] == "test_basic"
        assert "started_at" in meta
        assert "hostname" in meta

        lock.release()
        assert lock.acquired is False
        assert not os.path.exists(lock.lock_path)

    def test_double_release_is_safe(self, lock_dir):
        lock = FileLock("test_double", state_dir=lock_dir)
        lock.acquire()
        lock.release()
        lock.release()  # 二回目は何も起きない
        assert lock.acquired is False

    def test_release_without_acquire(self, lock_dir):
        lock = FileLock("test_no_acquire", state_dir=lock_dir)
        lock.release()  # エラーにならない
        assert lock.acquired is False

    def test_creates_state_dir(self, tmp_path):
        new_dir = str(tmp_path / "new" / "locks")
        assert not os.path.exists(new_dir)
        lock = FileLock("test_mkdir", state_dir=new_dir)
        lock.acquire()
        assert os.path.exists(new_dir)
        lock.release()


# ============================================================
# 排他制御テスト
# ============================================================

class TestFileLockExclusion:
    """同一ロック名での排他制御"""

    def test_second_acquire_fails(self, lock_dir):
        lock1 = FileLock("test_excl", state_dir=lock_dir)
        lock2 = FileLock("test_excl", state_dir=lock_dir)

        assert lock1.acquire() is True
        assert lock2.acquire() is False  # 競合
        assert lock2.acquired is False

        lock1.release()

    def test_acquire_after_release(self, lock_dir):
        lock1 = FileLock("test_reacquire", state_dir=lock_dir)
        lock2 = FileLock("test_reacquire", state_dir=lock_dir)

        lock1.acquire()
        lock1.release()
        assert lock2.acquire() is True
        lock2.release()

    def test_different_names_independent(self, lock_dir):
        lock_a = FileLock("test_a", state_dir=lock_dir)
        lock_b = FileLock("test_b", state_dir=lock_dir)

        assert lock_a.acquire() is True
        assert lock_b.acquire() is True  # 名前が違うので競合しない

        lock_a.release()
        lock_b.release()

    def test_same_pid_still_excluded(self, lock_dir):
        """同一 PID でも別インスタンスからは排他される"""
        lock1 = FileLock("test_same_pid", state_dir=lock_dir)
        lock2 = FileLock("test_same_pid", state_dir=lock_dir)

        assert lock1.acquire() is True
        # 同じ PID でも排他される
        assert lock2.acquire() is False

        lock1.release()


# ============================================================
# stale lock テスト
# ============================================================

class TestStaleLock:
    """stale ロックの検出と自動解除"""

    def _write_lock(self, path: str, pid: int, started_at: str,
                    script_name: str = "test.py"):
        """テスト用にロックファイルを直接作成。"""
        meta = {
            "pid": pid,
            "hostname": "testhost",
            "started_at": started_at,
            "script_name": script_name,
            "command_line": f"python {script_name}",
            "lock_name": "test",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

    def test_stale_by_dead_pid(self, lock_dir):
        """存在しない PID のロックは stale"""
        lock = FileLock("test_stale_pid", max_age_minutes=0, state_dir=lock_dir)
        # 存在しないPIDでロックファイルを作成
        self._write_lock(
            lock.lock_path,
            pid=99999999,  # 存在しないPID
            started_at="2020-01-01T00:00:00+09:00",
        )
        assert lock.is_stale() is True

    def test_not_stale_within_age(self, lock_dir):
        """max_age 内なら stale ではない"""
        lock = FileLock("test_fresh", max_age_minutes=60, state_dir=lock_dir)
        # 現在時刻でロックファイルを作成（自分のPID）
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=9))).isoformat()
        self._write_lock(lock.lock_path, pid=os.getpid(), started_at=now)
        assert lock.is_stale() is False

    def test_stale_auto_cleared_on_acquire(self, lock_dir):
        """stale ロックがある場合、acquire 時に自動クリアされる"""
        lock = FileLock("test_auto_clear", max_age_minutes=0, state_dir=lock_dir)
        # 存在しないPIDでロックファイルを作成
        self._write_lock(
            lock.lock_path,
            pid=99999999,
            started_at="2020-01-01T00:00:00+09:00",
        )
        assert os.path.exists(lock.lock_path)

        # acquire で stale が自動クリアされるはず
        assert lock.acquire() is True
        assert lock.acquired is True
        lock.release()

    def test_corrupt_lock_file(self, lock_dir):
        """壊れたロックファイルは stale 扱い"""
        lock = FileLock("test_corrupt", max_age_minutes=0, state_dir=lock_dir)
        with open(lock.lock_path, "w") as f:
            f.write("not json")
        assert lock.is_stale() is True
        assert lock.acquire() is True
        lock.release()

    def test_force_release(self, lock_dir):
        """force_release は無条件でロックを削除"""
        lock = FileLock("test_force", state_dir=lock_dir)
        lock.acquire()
        assert os.path.exists(lock.lock_path)

        other = FileLock("test_force", state_dir=lock_dir)
        other.force_release()
        assert not os.path.exists(lock.lock_path)


# ============================================================
# コンテキストマネージャテスト
# ============================================================

class TestContextManager:
    """with 文での使用"""

    def test_context_manager_success(self, lock_dir):
        with FileLock("test_ctx", state_dir=lock_dir) as lock:
            assert lock.acquired is True
            assert os.path.exists(lock.lock_path)
        # __exit__ で release
        assert not os.path.exists(lock.lock_path)

    def test_context_manager_on_exception(self, lock_dir):
        """例外発生時もロックが解放される"""
        try:
            with FileLock("test_ctx_exc", state_dir=lock_dir) as lock:
                assert lock.acquired is True
                raise ValueError("test error")
        except ValueError:
            pass
        # ロック解放済み
        assert not os.path.exists(
            os.path.join(lock_dir, "test_ctx_exc.lock")
        )


# ============================================================
# dual lock テスト
# ============================================================

class TestDualLock:
    """グローバル + ジョブの二段ロック"""

    def test_dual_lock_success(self, lock_dir):
        result = acquire_dual_lock(
            "realtime",
            global_max_age=60,
            job_max_age=15,
            state_dir=lock_dir,
        )
        assert result is not None
        global_lock, job_lock = result
        assert global_lock.acquired is True
        assert job_lock.acquired is True

        release_dual_lock(global_lock, job_lock)
        assert not os.path.exists(global_lock.lock_path)
        assert not os.path.exists(job_lock.lock_path)

    def test_dual_lock_global_held(self, lock_dir):
        """グローバルロックが保持されている場合"""
        # 先にグローバルロックを取得
        blocker = FileLock("tdnet_pipeline", state_dir=lock_dir)
        blocker.acquire()

        result = acquire_dual_lock("realtime", state_dir=lock_dir)
        assert result is None  # 取得失敗

        blocker.release()

    def test_dual_lock_job_held(self, lock_dir):
        """ジョブロックが保持されている場合"""
        blocker = FileLock("nightly", state_dir=lock_dir)
        blocker.acquire()

        result = acquire_dual_lock("nightly", state_dir=lock_dir)
        assert result is None

        # グローバルロックも解放されていることを確認
        assert not os.path.exists(
            os.path.join(lock_dir, "tdnet_pipeline.lock")
        )

        blocker.release()

    def test_nightly_blocks_realtime(self, lock_dir):
        """Nightly がグローバルロックを持っている間は Realtime が走れない"""
        nightly_locks = acquire_dual_lock(
            "nightly", state_dir=lock_dir,
        )
        assert nightly_locks is not None

        realtime_locks = acquire_dual_lock(
            "realtime", state_dir=lock_dir,
        )
        assert realtime_locks is None  # グローバルロックで止まる

        release_dual_lock(*nightly_locks)


# ============================================================
# process_exists テスト
# ============================================================

class TestProcessExists:

    def test_self_pid_exists(self):
        assert process_exists(os.getpid()) is True

    def test_invalid_pid(self):
        assert process_exists(0) is False
        assert process_exists(-1) is False

    def test_nonexistent_pid(self):
        # 非常に大きなPIDは存在しないはず
        assert process_exists(99999999) is False


# ============================================================
# get_info テスト
# ============================================================

class TestGetInfo:

    def test_info_no_lock(self, lock_dir):
        lock = FileLock("test_info_no", state_dir=lock_dir)
        info = lock.get_info()
        assert info["exists"] is False

    def test_info_with_lock(self, lock_dir):
        lock = FileLock("test_info_yes", state_dir=lock_dir)
        lock.acquire()
        info = lock.get_info()
        assert info["exists"] is True
        assert info["pid"] == os.getpid()
        assert info["is_stale"] is False
        lock.release()
