import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from tools.runtime_launch import job_steps, run_steps
from tools.run_tdnet_realtime_background import run_realtime

@pytest.mark.parametrize('job', ['realtime', 'nightly', 'reconcile', 'news', 'sector'])
def test_steps_stop_and_inherit(tmp_path, job):
    environment = {'TDNET_RUNTIME_STATE_ROOT': str(tmp_path/'shared')}
    steps = job_steps(job, tmp_path)
    for failed_at in range(-1, len(steps)):
        calls = []
        def runner(command, **kwargs):
            assert kwargs['env'] is environment
            assert kwargs['cwd'] == str(tmp_path)
            assert Path(command[1]).is_relative_to(tmp_path)
            index = len(calls)
            calls.append(command)
            return SimpleNamespace(returncode=17 if index == failed_at else 0)
        assert run_steps(job, tmp_path, environment, runner) == (0 if failed_at == -1 else 17)
        assert len(calls) == (len(steps) if failed_at == -1 else failed_at+1)

def test_shared_realtime_utf8_append_and_rollback(tmp_path, monkeypatch):
    code = tmp_path/'new code'
    state = tmp_path/'shared state'
    code.mkdir()
    monkeypatch.setenv('TDNET_RUNTIME_STATE_ROOT', str(state))
    command = [sys.executable, '-X', 'utf8', '-c',
               "import sys; print('日本語 stdout'); print('stderr', file=sys.stderr); sys.exit(17)"]
    assert run_realtime(code, command=command) == 17
    monkeypatch.delenv('TDNET_RUNTIME_STATE_ROOT')
    assert run_realtime(state, command=command) == 17
    assert not (code/'logs').exists()
    outputs = list((state/'logs').glob('realtime_console_*.log'))
    assert len(outputs) == 1
    text = outputs[0].read_text(encoding='utf8')
    assert text.count('日本語 stdout') == 2
    assert text.count('stderr') == 2
    records = [json.loads(line) for line in (state/'logs/realtime_launcher.jsonl').read_text(encoding='utf8').splitlines()]
    assert [r['event'] for r in records] == ['launcher_started','launcher_finished']*2
    assert all(r['pid'] == os.getpid() and r['parent_pid'] == os.getppid() for r in records)
    assert all('at' in r for r in records)
    assert records[-1]['return_code'] == 17

def test_launcher_child_metadata_without_jobs(tmp_path, monkeypatch):
    import tools.runtime_launch as launch
    code = tmp_path/'code'; code.mkdir()
    state = tmp_path/'state'
    settings = tmp_path/'settings'; settings.mkdir()
    import lib.production_environment as production
    monkeypatch.setattr(production, 'bootstrap_production_write_environment', lambda root: None)
    monkeypatch.setattr(sys, 'argv', ['runtime_launch', '--code-root', str(code), '--settings-root', str(settings), '--state-root', str(state), '--job', 'realtime'])
    calls = []
    class Child:
        pid = 12345
        def __init__(self, command, **kwargs):
            calls.append(command)
            assert '--steps-only' in command
            assert kwargs['stderr'] == subprocess.STDOUT
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self): return 17
    monkeypatch.setattr(launch.subprocess, 'Popen', Child)
    # main modifies only this process environment; monkeypatch restores it.
    for name in ('TDNET_RUNTIME_STATE_ROOT','PYTHONUTF8','PYTHONIOENCODING'):
        monkeypatch.setenv(name, os.environ.get(name, ''))
    assert launch.main() == 17
    records = [json.loads(line) for line in (state/'logs/realtime_launcher.jsonl').read_text(encoding='utf8').splitlines()]
    assert records[1]['event'] == 'launcher_child_started'
    assert records[1]['child_pid'] == 12345
    assert len(calls) == 1

def test_log_daily_filename_and_error_event(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import tools.run_tdnet_realtime_background as background
    code = tmp_path/'code'; code.mkdir()
    shared = tmp_path/'state'
    monkeypatch.setenv('TDNET_RUNTIME_STATE_ROOT', str(shared))
    class Clock:
        day = 1
        @classmethod
        def now(cls):
            return datetime(2025, 1, cls.day, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(background, 'datetime', Clock)
    def runner(command, **kwargs):
        kwargs['stdout'].write('fixture log\n')
        return SimpleNamespace(returncode=0)
    assert run_realtime(code, command=['fixture'], runner=runner) == 0
    Clock.day = 2
    assert run_realtime(code, command=['fixture'], runner=runner) == 0
    assert sorted(p.name for p in (shared/'logs').glob('realtime_console_*.log')) == [
        'realtime_console_20250101.log', 'realtime_console_20250102.log']
    def fail(command, **kwargs):
        raise OSError('fixture launch failure')
    assert run_realtime(code, command=['fixture'], runner=fail) == 1
    rows = [json.loads(line) for line in (shared/'logs/realtime_launcher.jsonl').read_text(encoding='utf8').splitlines()]
    assert rows[-1]['event'] == 'launcher_error'
    assert rows[-1]['error_type'] == 'OSError'
