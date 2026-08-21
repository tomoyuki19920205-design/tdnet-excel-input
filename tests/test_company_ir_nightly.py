import tools.company_ir_nightly as company_ir_nightly


def test_publisher_configuration_available_with_required_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "configured")

    assert company_ir_nightly.publisher_configuration_available()


def test_publisher_configuration_unavailable_without_url(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "configured")

    assert not company_ir_nightly.publisher_configuration_available()


def test_publisher_configuration_unavailable_without_service_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert not company_ir_nightly.publisher_configuration_available()


def test_publisher_preflight_modes():
    check = company_ir_nightly.publisher_preflight_failure

    assert not check(gate=False, dry_run=False, pending=1, configured=False)
    assert not check(gate=True, dry_run=False, pending=0, configured=False)
    assert not check(gate=True, dry_run=True, pending=1, configured=False)
    assert not check(gate=True, dry_run=False, pending=1, configured=True)
    assert check(gate=True, dry_run=False, pending=1, configured=False)
