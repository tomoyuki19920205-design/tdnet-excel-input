import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tools.run_tdnet_realtime_background import _creation_flags, run_realtime


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "register_tasks.ps1"
HIDDEN_LAUNCHER = ROOT / "tools" / "run_tdnet_realtime_hidden.vbs"


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
    assert command == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", f'""{batch.resolve()}""']
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
    assert records[0]["working_directory"] == str(tmp_path.resolve())
    assert records[-1]["event"] == "launcher_finished"
    assert records[-1]["return_code"] == 17


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
