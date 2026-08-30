import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.run_tdnet_realtime_background import _creation_flags, run_realtime


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


def test_registration_uses_non_console_launcher_and_keeps_schedule():
    installer = Path(__file__).resolve().parents[1] / "tools" / "register_tasks.ps1"
    script = installer.read_text(encoding="utf-8")

    assert '".venv\\Scripts\\pythonw.exe"' in script
    assert '"tools\\run_tdnet_realtime_background.py"' in script
    assert '"/ST", "08:32"' in script
    assert '"/RI", "10"' in script
    assert '"/DU", "09:30"' in script
    assert "-MultipleInstances IgnoreNew" in script
