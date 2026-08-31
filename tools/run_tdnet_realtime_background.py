#!/usr/bin/env python3
"""Launch the existing TDNET Realtime batch without creating a console window."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_event(path: Path, event: str, **details: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "event": event, **details}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_realtime(
    root: Path = ROOT,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    root = root.resolve()
    batch = root / "run_realtime.bat"
    audit_log = root / "logs" / "realtime_launcher.jsonl"
    output_log = root / "logs" / f"realtime_console_{datetime.now().astimezone():%Y%m%d}.log"
    if not batch.is_file():
        _append_event(audit_log, "launcher_error", pid=os.getpid(), error=f"batch not found: {batch}")
        return 1

    default_comspec = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    comspec = os.environ.get("COMSPEC", str(default_comspec))
    command = [comspec, "/d", "/c", "call", str(batch)]
    started = time.monotonic()
    _append_event(
        audit_log,
        "launcher_started",
        pid=os.getpid(),
        command=command,
        bat_path=str(batch),
        cwd=str(root),
        working_directory=str(root),
        creationflags=_creation_flags(),
    )
    try:
        output_log.parent.mkdir(parents=True, exist_ok=True)
        with output_log.open("a", encoding="utf-8") as output:
            result = runner(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                check=False,
                creationflags=_creation_flags(),
            )
        return_code = int(result.returncode)
    except Exception as exc:
        _append_event(
            audit_log,
            "launcher_error",
            pid=os.getpid(),
            error=str(exc),
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        return 1

    _append_event(
        audit_log,
        "launcher_finished",
        pid=os.getpid(),
        return_code=return_code,
        duration_seconds=round(time.monotonic() - started, 3),
        output_log=str(output_log),
    )
    return return_code


def main() -> int:
    return run_realtime()


if __name__ == "__main__":
    raise SystemExit(main())
