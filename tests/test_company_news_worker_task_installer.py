from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "install_company_news_worker_task.ps1"
HIDDEN_LAUNCHER = ROOT / "tools" / "run_company_news_worker_hidden.vbs"


def test_installer_defaults_to_five_minute_hidden_worker():
    script = INSTALLER.read_text(encoding="utf-8")

    assert "[int]$IntervalMinutes = 5" in script
    assert '".venv\\Scripts\\pythonw.exe"' in script
    assert '"tools\\run_company_news_worker_hidden.vbs"' in script
    assert '"wscript.exe"' in script
    assert "-Execute $wscriptPath" in script
    assert '"//B"' in script
    assert '"//NoLogo"' in script
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in script
    assert '"--once"' in script
    assert '"--trigger", "task_scheduler"' in script
    assert "-WorkingDirectory $RepositoryRoot" in script
    assert "-MultipleInstances IgnoreNew" in script


def test_hidden_launcher_preserves_exit_code_and_waits_without_a_window():
    script = HIDDEN_LAUNCHER.read_text(encoding="utf-8")

    assert 'CreateObject("WScript.Shell")' in script
    assert "shell.CurrentDirectory = workingDirectory" in script
    assert "shell.Run(commandLine, 0, True)" in script
    assert "WScript.Quit exitCode" in script
    assert "For index = 2 To arguments.Count - 1" in script
