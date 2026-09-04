"""Explicit code/state/settings launcher; no work occurs on import."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def job_steps(job, code_root):
    if job == 'realtime':
        return [('tools/scheduler_realtime.py',), ('tools/retry_material_urls.py','--runner','realtime')]
    if job == 'nightly':
        return [('tools/scheduler_nightly.py',), ('tools/retry_material_urls.py','--runner','nightly'), ('tools/backfill_earnings_tdnet_events.py','--since','60')]
    if job == 'reconcile':
        return [('tools/scheduler_reconcile.py',)]
    worker={'news':'company_news_inbox_worker.py','sector':'sector_weekly_inbox_worker.py'}[job]
    return [('tools/'+worker,'--once','--root',str(code_root),'--trigger','task_scheduler')]


def run_steps(job, code_root, environment, runner=subprocess.run):
    python = code_root/'.venv/Scripts/python.exe'
    for step in job_steps(job,code_root):
        result = runner([str(python),str(code_root/step[0]),*step[1:]],
                        cwd=str(code_root),env=environment,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            return result.returncode
    return 0


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--code-root',type=Path,required=True)
    parser.add_argument('--settings-root',type=Path,required=True)
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--steps-only', action='store_true')
    parser.add_argument('--job',choices=['realtime','nightly','reconcile','news','sector'],required=True)
    args=parser.parse_args()
    if not args.code_root.is_absolute() or not args.settings_root.is_absolute():
        parser.error('code-root and settings-root must be absolute')
    sys.path.insert(0,str(args.code_root))
    from lib.runtime_paths import runtime_state_root, STATE_ROOT_ENV
    from lib.production_environment import bootstrap_production_write_environment
    os.environ[STATE_ROOT_ENV]=args.state_root
    runtime_state_root()  # Fail before reading settings or starting any job.
    bootstrap_production_write_environment(args.settings_root)
    os.environ[STATE_ROOT_ENV] = args.state_root
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if args.job == 'realtime' and not args.steps_only:
        from tools.run_tdnet_realtime_background import run_realtime, _append_event
        from lib.runtime_paths import runtime_path
        command = [str(args.code_root/'.venv/Scripts/python.exe'), '-X', 'utf8', '-B',
                   str(Path(__file__).resolve()), *sys.argv[1:], '--steps-only']
        def logged_runner(command, **kwargs):
            kwargs.pop('check', None)
            with subprocess.Popen(command, **kwargs) as child:
                _append_event(runtime_path(args.code_root/'logs/realtime_launcher.jsonl', code_root=args.code_root),
                              'launcher_child_started', pid=os.getpid(),
                              parent_pid=os.getppid(), child_pid=child.pid)
                return SimpleNamespace(returncode=child.wait())
        return run_realtime(args.code_root, command=command, runner=logged_runner)
    return run_steps(args.job,args.code_root,os.environ.copy())


if __name__=='__main__':
    raise SystemExit(main())
