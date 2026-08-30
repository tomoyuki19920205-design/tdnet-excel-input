import os
from pathlib import Path

import pytest

import tools.company_news_atomic as atomic_module
from tools.company_news_atomic import atomic_write_text, replace_with_retry


def _windows_permission_error(winerror: int = 5) -> PermissionError:
    error = PermissionError(winerror, "transient Windows file lock")
    error.winerror = winerror
    return error


def test_transient_windows_replace_error_retries_then_succeeds(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.json"
    source.write_text("new", encoding="utf-8")
    real_replace = os.replace
    attempts = []
    sleeps = []

    def flaky_replace(src, dst):
        attempts.append((Path(src), Path(dst)))
        if len(attempts) < 3:
            raise _windows_permission_error()
        real_replace(src, dst)

    monkeypatch.setattr(atomic_module.os, "replace", flaky_replace)
    retries = replace_with_retry(source, target, sleep=sleeps.append)

    assert retries == 2
    assert sleeps == [0.05, 0.1]
    assert target.read_text(encoding="utf-8") == "new"


def test_permanent_windows_replace_error_is_bounded_and_preserves_target(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    attempts = 0

    def locked_replace(_src, _dst):
        nonlocal attempts
        attempts += 1
        raise _windows_permission_error(32)

    monkeypatch.setattr(atomic_module.os, "replace", locked_replace)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new", sleep=lambda _delay: None)

    assert attempts == 6
    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_writes_use_unique_sibling_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "queue_state.json"
    real_replace = os.replace
    sources = []

    def record_replace(src, dst):
        sources.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(atomic_module.os, "replace", record_replace)
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")

    assert len({path.name for path in sources}) == 2
    assert all(path.parent == target.parent for path in sources)
    assert target.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.glob(".*.tmp")) == []
