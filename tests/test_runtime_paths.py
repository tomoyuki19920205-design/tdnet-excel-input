"""All files in these tests are synthetic state; no Production connection."""
import importlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.runtime_paths import CODE_ROOT, STATE_ROOT_ENV, runtime_path, runtime_state_root


def test_legacy_unset(monkeypatch):
    monkeypatch.delenv(STATE_ROOT_ENV, raising=False)
    assert runtime_path('data/state.db') == Path('data/state.db')
    assert runtime_state_root() == CODE_ROOT


@pytest.mark.parametrize('bad', ['', ' ', 'relative', '../escape', 'C:relative', '\\relative'])
def test_reject_invalid_root(monkeypatch, bad):
    monkeypatch.setenv(STATE_ROOT_ENV, bad)
    with pytest.raises(ValueError):
        runtime_path('decision_db.db')


def test_reject_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path))
    for bad in ['../outside.db', 'data/../../outside.db', r'data\..\outside.db']:
        with pytest.raises(ValueError):
            runtime_path(bad)


def test_import_and_resolve_do_not_create(monkeypatch, tmp_path):
    target = tmp_path / '不存在 state'
    monkeypatch.setenv(STATE_ROOT_ENV, str(target))
    import lib.runtime_paths as module
    importlib.reload(module)
    assert module.runtime_path('data/state.db') == target / 'data/state.db'
    assert not target.exists()


def test_runtime_resolution_after_import(monkeypatch, tmp_path):
    from src.config import Config
    from tools.file_lock import FileLock
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path))
    assert Config().decision_db_path == str(tmp_path / 'decision_db.db')
    assert Path(FileLock('test').lock_path) == tmp_path / 'state/locks/test.lock'
    assert not (tmp_path / 'state').exists()


def test_worker_paths_keep_code_and_share_state(monkeypatch, tmp_path):
    from tools.company_news_inbox_worker import WorkerPaths as News
    from tools.company_news_queue import QueuePaths
    from tools.sector_weekly_inbox_worker import WorkerPaths as Sector
    code = tmp_path / 'new-code'
    state = tmp_path / '旧 state'
    monkeypatch.setenv(STATE_ROOT_ENV, str(state))
    news = News.from_values(code)
    queue = QueuePaths.from_values(code)
    sector = Sector.from_values(code)
    assert news.root == code
    for path in (news.db, news.lock, news.state, news.inbox, queue.entries, sector.db, sector.lock):
        assert path.is_relative_to(state)
    assert not code.exists()
    assert not state.exists()


def test_sqlite_wal_and_old_new_old(monkeypatch, tmp_path):
    old = tmp_path / '旧 root'
    old.mkdir()
    db = old / 'decision_db.db'
    original = sqlite3.connect(db)
    try:
        original.execute('pragma journal_mode=wal')
        original.execute('create table fixture (id integer primary key, value text)')
        original.execute("insert into fixture values (1, 'old')")
        original.commit()
        monkeypatch.setenv(STATE_ROOT_ENV, str(old))
        new = sqlite3.connect(runtime_path('decision_db.db'))
        try:
            assert new.execute('select value from fixture').fetchone()[0] == 'old'
            new.execute("update fixture set value='new' where id=1")
            new.commit()
            assert Path(str(db)+'-wal').exists()
            assert Path(str(db)+'-shm').exists()
        finally:
            new.close()
        monkeypatch.delenv(STATE_ROOT_ENV)
        assert original.execute('select * from fixture').fetchall() == [(1, 'new')]
    finally:
        original.close()


def test_explicit_external_override(monkeypatch, tmp_path):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path/'state'))
    explicit = tmp_path / 'fixture.db'
    assert runtime_path(explicit) == explicit


def test_independent_fixture_roots(monkeypatch, tmp_path):
    monkeypatch.delenv(STATE_ROOT_ENV, raising=False)
    def run(index):
        root = tmp_path / str(index)
        path = runtime_path(root / 'state.db')
        root.mkdir()
        with sqlite3.connect(path) as conn:
            conn.execute('create table marker (value integer)')
            conn.execute('insert into marker values (?)', (index,))
            return conn.execute('select value from marker').fetchone()[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, range(4))) == list(range(4))


def test_static_config_not_read_from_state(monkeypatch, tmp_path):
    from src.config import load_config
    code, state = tmp_path/'code', tmp_path/'state'
    code.mkdir(); state.mkdir()
    (code/'config.yaml').write_text('sheet_name: code\n', encoding='utf8')
    (state/'config.yaml').write_text('sheet_name: WRONG\n', encoding='utf8')
    monkeypatch.setenv(STATE_ROOT_ENV, str(state))
    config = load_config(str(code/'config.yaml'))
    assert config.sheet_name == 'code'
    assert Path(config.state_db_path) == state/'data/state.db'
    from tools.sector_weekly_scheduler import PROMPT_PATH
    assert PROMPT_PATH.is_relative_to(CODE_ROOT)
    from tools.fetch_jquants_prices import _MIGRATION_SQL
    assert _MIGRATION_SQL.is_relative_to(CODE_ROOT)
    assert _MIGRATION_SQL.read_text(encoding='utf8')


def test_queue_contract_old_new_old(monkeypatch, tmp_path):
    from tests import test_company_news_batched_tasks as fixture
    from tools.company_news_queue import QueuePaths, queue_status
    from tools.company_news_work_bridge import BridgePaths
    from tools.company_news_task_batch import snapshot_task, release_task
    from tools.company_news_inbox_worker import WorkerPaths, run_once
    from datetime import timedelta
    old, code = tmp_path/'old', tmp_path/'new'
    old.mkdir()
    monkeypatch.delenv(STATE_ROOT_ENV, raising=False)
    fixture._setup(old, count=200, batch_size=4)
    snapshot = snapshot_task(old, 'task01', now=fixture.NOW)
    monkeypatch.setenv(STATE_ROOT_ENV, str(old))
    assert snapshot_task(code, 'task01', now=fixture.NOW+timedelta(minutes=1))['status'] == 'busy'
    fixture._save_success(old, 'slot01')
    result = run_once(WorkerPaths.from_values(code, db=old/'news.db'), sync_func=fixture._sync)
    assert result['completed'] == 1
    release_task(code, 'task01', snapshot['run_token'], success_count=1, now=fixture.NOW+timedelta(minutes=2))
    monkeypatch.delenv(STATE_ROOT_ENV)
    status = queue_status(QueuePaths.from_values(old), BridgePaths.from_root(old))
    assert status['completed'] == 1
    assert not code.exists()


def test_checkpoint_default_and_explicit_roundtrip(monkeypatch, tmp_path):
    from tools.fetch_jquants_prices import save_progress, load_progress
    path=tmp_path/'data/jquants_prices_progress.json'
    monkeypatch.delenv(STATE_ROOT_ENV, raising=False)
    save_progress({'cursor':'old'},path)
    monkeypatch.setenv(STATE_ROOT_ENV,str(tmp_path))
    assert load_progress()=={'cursor':'old'}
    save_progress({'cursor':'new'})
    monkeypatch.delenv(STATE_ROOT_ENV)
    assert load_progress(path)=={'cursor':'new'}


def test_shared_lock_excludes_second_root(monkeypatch, tmp_path):
    from tools.file_lock import FileLock
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path))
    first, second = FileLock('fixture'), FileLock('fixture')
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_imports_do_not_open_db_or_create_state(tmp_path):
    modules = [
        'src.config', 'tools.file_lock', 'tools.company_news_queue',
        'tools.company_news_inbox_worker', 'tools.company_news_task_batch',
        'tools.sector_weekly_inbox_worker', 'tools.sector_weekly_scheduler',
        'tools.company_ir_nightly', 'tools.company_ir_source_discovery',
        'tools.scheduler_nightly', 'tools.scheduler_realtime', 'tools.scheduler_reconcile',
        'tools.fetch_jquants_prices', 'tools.fetch_jquants_financials',
        'tools.sync_financials', 'tools.retry_material_urls',
    ]
    script = '''
import importlib, os, sys
def guard(event, args):
    if event in ('sqlite3.connect', 'os.mkdir', 'os.remove', 'os.rename'):
        raise AssertionError('Import performed state I/O: '+event)
    if event == 'open' and not isinstance(args[0], int):
        mode, flags = args[1:]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT)):
            raise AssertionError('Import wrote a file')
sys.addaudithook(guard)
for name in sys.argv[1:]: importlib.import_module(name)
'''
    env = os.environ.copy()
    env[STATE_ROOT_ENV] = str(tmp_path/'not-created')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-c', script, *modules], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path/'not-created').exists()


@pytest.mark.parametrize('name,field', [
    ('tools.fetch_jquants_prices','db'),
    ('tools.fetch_jquants_financials','db'),
    ('tools.fetch_jquants_details','db'),
    ('tools.sync_financials','sqlite'),
    ('tools.sync_market_data','sqlite'),
    ('tools.sync_per_share_data','sqlite'),
    ('tools.extract_per_share_from_raw','db'),
    ('tools.sync_screener_snapshot','db'),
    ('tools.sync_jquants_tdnet_metadata','db'),
    ('tools.sync_ny_market','db'),
    ('tools.write_ny_market_payload','inbox'),
    ('tools.ingest_company_news','db'),
    ('tools.sector_weekly_scheduler','db'),
    ('tools.sector_weekly_work_bridge','work_root'),
])
def test_cli_defaults_use_state(monkeypatch, tmp_path, name, field):
    import argparse
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path))
    module = importlib.import_module(name)
    class Captured(Exception):
        pass
    def parse(parser, *args, **kwargs):
        path = Path(parser.get_default(field))
        assert path.is_relative_to(tmp_path)
        raise Captured()
    monkeypatch.setattr(argparse.ArgumentParser, 'parse_args', parse)
    with pytest.raises(Captured):
        module.main()


def test_cache_read_does_not_create_files(monkeypatch, tmp_path):
    from src.cache import cache_manager
    monkeypatch.setenv(STATE_ROOT_ENV,str(tmp_path/'state'))
    def no_connect(*args, **kwargs):
        raise AssertionError('Cache read must not open SQLite')
    monkeypatch.setattr(sqlite3,'connect',no_connect)
    assert cache_manager.load_binary('pdf','missing') is None
    assert cache_manager.load_json('missing') is None
    assert not (tmp_path/'state').exists()
    cache_manager.save_binary('pdf','fixture',b'%PDF fixture')
    assert cache_manager.get_path('pdf','fixture').is_relative_to(tmp_path/'state')


@pytest.mark.skipif(os.name != 'nt', reason='Windows path validation')
def test_runtime_root_validation_absolute_bad_components(monkeypatch, tmp_path):
    for bad in [str(tmp_path)+'/../escape', str(tmp_path)+'/a*', str(tmp_path)+'/NUL']:
        monkeypatch.setenv(STATE_ROOT_ENV,bad)
        with pytest.raises(ValueError):
            runtime_state_root()


def test_child_process_inherits_state_root(tmp_path):
    env=os.environ.copy()
    env[STATE_ROOT_ENV]=str(tmp_path/'共有 state')
    env['PYTHONIOENCODING']='utf8'
    result=subprocess.run([sys.executable,'-c',
        'from lib.runtime_paths import runtime_path; print(runtime_path("decision_db.db"))'],
        cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf8',timeout=30)
    assert result.returncode==0,result.stderr
    assert Path(result.stdout.strip())==tmp_path/'共有 state/decision_db.db'


def test_historical_cache_default_is_rebased(monkeypatch, tmp_path):
    from lib.runtime_paths import runtime_default
    legacy = tmp_path/'historical-cache'
    monkeypatch.delenv(STATE_ROOT_ENV,raising=False)
    assert runtime_default('data/edinet_cache',legacy)==legacy
    monkeypatch.setenv(STATE_ROOT_ENV,str(tmp_path/'state'))
    assert runtime_default('data/edinet_cache',legacy)==tmp_path/'state/data/edinet_cache'


def test_notification_dedup_state_roundtrip(monkeypatch, tmp_path):
    import json
    from tools.discord_alerts import _load_sent_log,_save_sent_log
    state=tmp_path/'shared'
    path=state/'logs/alert_sent_log.json'
    path.parent.mkdir(parents=True)
    marker=('1234','2024-10-31','3Q','earnings')
    path.write_text(json.dumps([marker]),encoding='utf8')
    monkeypatch.setenv(STATE_ROOT_ENV,str(state))
    assert _load_sent_log()=={marker}
    newer=('1234','2024-10-31','3Q','forecast')
    _save_sent_log({marker,newer})
    monkeypatch.delenv(STATE_ROOT_ENV)
    assert {tuple(x) for x in json.loads(path.read_text(encoding='utf8'))}=={marker,newer}
