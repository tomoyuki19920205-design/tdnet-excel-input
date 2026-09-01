from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "install_sector_weekly_inbox_worker_task.ps1"
LAUNCHER = ROOT / "tools" / "run_sector_weekly_inbox_worker_hidden.vbs"


def test_installer_registers_dedicated_disabled_hidden_five_minute_worker():
    script = INSTALLER.read_text(encoding="utf-8")
    assert '"SectorWeeklyInboxWorker"' in script
    assert "[int]$IntervalMinutes = 5" in script
    assert '"wscript.exe"' in script and "-Execute $wscriptPath" in script
    assert '"//B"' in script and '"//NoLogo"' in script
    assert "run_sector_weekly_inbox_worker_hidden.vbs" in script
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in script
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 5)" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-WorkingDirectory $RepositoryRoot" in script
    assert "Disable-ScheduledTask -TaskName $TaskName" in script
    assert 'Enabled = $false' in script


def test_hidden_launcher_waits_without_window_and_propagates_exit_code():
    script = LAUNCHER.read_text(encoding="utf-8")
    assert 'CreateObject("WScript.Shell")' in script
    assert "shell.CurrentDirectory = workingDirectory" in script
    assert "shell.Run(commandLine, 0, True)" in script
    assert "WScript.Quit exitCode" in script
