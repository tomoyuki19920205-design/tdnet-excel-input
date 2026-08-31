import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.run_tdnet_realtime_background import _creation_flags, run_realtime


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "register_tasks.ps1"
HIDDEN_LAUNCHER = ROOT / "tools" / "run_tdnet_realtime_hidden.vbs"
VBS_RUN_CODE = (
    "import sys;from pathlib import Path;"
    "from tools.run_tdnet_realtime_background import run_realtime;"
    "raise SystemExit(run_realtime(Path(sys.argv[1])))"
)


def _wscript() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"


def _pythonw() -> Path:
    return Path(sys.executable).with_name("pythonw.exe")


def _vbs_command(root: Path) -> list[str]:
    return [
        str(_wscript()),
        "//B",
        "//NoLogo",
        str(HIDDEN_LAUNCHER),
        str(_pythonw()),
        str(ROOT),
        "-c",
        VBS_RUN_CODE,
        str(root),
    ]


def test_background_launcher_preserves_batch_semantics_and_exit_code(tmp_path, monkeypatch):
    batch = tmp_path / "run_realtime.bat"
    batch.write_text("@exit /b 0\n", encoding="utf-8")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=17)

    assert run_realtime(tmp_path, runner=runner) == 17
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/c",
        "call",
        str(batch.resolve()),
    ]
    assert all("&quot;" not in value for value in command)
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["creationflags"] == _creation_flags()
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "realtime_launcher.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["event"] == "launcher_started"
    assert records[0]["command"] == command
    assert records[0]["bat_path"] == str(batch.resolve())
    assert records[0]["cwd"] == str(tmp_path.resolve())
    assert records[0]["working_directory"] == str(tmp_path.resolve())
    assert "&quot;" not in json.dumps(records[0])
    assert records[-1]["event"] == "launcher_finished"
    assert records[-1]["return_code"] == 17


def test_real_cmd_runs_batch_in_space_and_japanese_path_with_logs(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows cmd.exe regression")

    root = tmp_path / "空白 日本語 path"
    root.mkdir()
    (root / "run_realtime.bat").write_text(
        "@echo off\r\n"
        "> sentinel.txt echo sentinel-ok\r\n"
        "echo TDNET_STDOUT_UNIQUE\r\n"
        "echo TDNET_STDERR_UNIQUE 1>&2\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )

    assert run_realtime(root) == 0
    assert (root / "sentinel.txt").read_text(encoding="ascii").strip() == "sentinel-ok"
    output_logs = list((root / "logs").glob("realtime_console_*.log"))
    assert len(output_logs) == 1
    output = output_logs[0].read_bytes().decode("ascii")
    assert "TDNET_STDOUT_UNIQUE" in output
    assert "TDNET_STDERR_UNIQUE" in output
    records = [
        json.loads(line)
        for line in (root / "logs" / "realtime_launcher.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["command"] == [
        os.environ["COMSPEC"],
        "/d",
        "/c",
        "call",
        str(root.resolve() / "run_realtime.bat"),
    ]
    assert records[0]["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert all("&quot;" not in value for value in records[0]["command"])
    assert records[-1]["return_code"] == 0


def test_real_cmd_exit_17_propagates_through_python_and_vbs(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows cmd.exe regression")

    root = tmp_path / "異常 終了 path"
    root.mkdir()
    (root / "run_realtime.bat").write_text("@echo off\r\nexit /b 17\r\n", encoding="ascii")

    assert run_realtime(root) == 17
    task_equivalent = subprocess.run(_vbs_command(root), check=False, timeout=15)
    assert task_equivalent.returncode == 17


def test_real_hidden_chain_has_no_visible_window_or_focus_change(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows window telemetry")

    psutil = pytest.importorskip("psutil")
    root = tmp_path / "非表示 実行 path"
    root.mkdir()
    (root / "run_realtime.bat").write_text(
        "@echo off\r\n"
        "> sentinel.txt echo started\r\n"
        "ping -n 4 127.0.0.1 >nul\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )

    user32 = ctypes.windll.user32
    foreground_before = user32.GetForegroundWindow()
    process = subprocess.Popen(_vbs_command(root))
    deadline = time.monotonic() + 8
    descendants = []
    while time.monotonic() < deadline:
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.Error:
            descendants = []
        if any(child.name().lower() == "cmd.exe" for child in descendants):
            break
        time.sleep(0.05)

    pids = {process.pid, *(child.pid for child in descendants)}
    visible_windows = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def inspect_window(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            visible_windows.append((pid.value, hwnd))
        return True

    user32.EnumWindows(inspect_window, 0)
    foreground_during = user32.GetForegroundWindow()

    assert any(child.name().lower() == "cmd.exe" for child in descendants)
    assert visible_windows == []
    assert foreground_during == foreground_before
    assert process.wait(timeout=15) == 0


def test_registration_uses_gui_parent_launcher_and_keeps_schedule():
    script = INSTALLER.read_text(encoding="utf-8")

    assert '"wscript.exe"' in script
    assert '"tools\\run_tdnet_realtime_hidden.vbs"' in script
    assert '".venv\\Scripts\\pythonw.exe"' in script
    assert '"tools\\run_tdnet_realtime_background.py"' in script
    assert '"//B"' in script
    assert '"//NoLogo"' in script
    assert '$realtimeLauncherArguments = @(' in script
    assert "$realtimeCommand = (ConvertTo-QuotedTaskArgument $WscriptPath)" in script
    assert '"/TR", $realtimeCommand' in script
    assert "$realtimeCommand = '\"' + $Pythonw" not in script
    assert "New-ScheduledTaskAction" in script
    assert "-Execute $WscriptPath" in script
    assert "-Argument $realtimeArgumentString" in script
    assert "-WorkingDirectory $ProjectRoot" in script
    assert '"/ST", "08:32"' in script
    assert '"/RI", "10"' in script
    assert '"/DU", "09:30"' in script
    assert "-MultipleInstances IgnoreNew" in script
    assert '"/TN", "TDNET_Nightly"' in script
    assert '"/TR", $NightlyBat' in script


def test_hidden_vbs_waits_without_a_window_and_propagates_exit_code():
    script = HIDDEN_LAUNCHER.read_text(encoding="utf-8")

    assert 'CreateObject("WScript.Shell")' in script
    assert "shell.CurrentDirectory = workingDirectory" in script
    assert "shell.Run(commandLine, 0, True)" in script
    assert "WScript.Quit exitCode" in script
    assert "For index = 2 To arguments.Count - 1" in script
    assert "QuoteArgument(arguments(index))" in script


def test_hidden_vbs_returns_python_launcher_exit_code(tmp_path):
    if os.name != "nt":
        return

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    fixture = tmp_path / "exit_fixture.py"
    fixture.write_text("raise SystemExit(17)\n", encoding="utf-8")
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"

    result = subprocess.run(
        [
            str(wscript),
            "//B",
            "//NoLogo",
            str(HIDDEN_LAUNCHER),
            str(pythonw),
            str(tmp_path),
            str(fixture),
        ],
        check=False,
        timeout=10,
    )

    assert result.returncode == 17
