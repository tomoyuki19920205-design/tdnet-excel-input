from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "install_company_news_worker_task.ps1"


def test_installer_defaults_to_five_minute_non_console_worker():
    script = INSTALLER.read_text(encoding="utf-8")

    assert "[int]$IntervalMinutes = 5" in script
    assert '".venv\\Scripts\\pythonw.exe"' in script
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in script
    assert '"--once"' in script
    assert '"--trigger", "task_scheduler"' in script
    assert "-WorkingDirectory $RepositoryRoot" in script
    assert "-MultipleInstances IgnoreNew" in script
