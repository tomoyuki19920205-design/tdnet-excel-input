from tools.company_news_date_precision_migration_dry_run import run_dry_run


def test_in_memory_migration_dry_run_passes_all_compatibility_checks():
    result = run_dry_run()
    assert result["status"] == "PASS"
    assert result["legacy_exact_value_unchanged"] is True
    assert result["published_date_backfilled_for_legacy"] is False
    assert result["date_only_insert"] is True and result["unknown_insert"] is True
    assert result["invalid_cross_field_rows_rejected"] == 3
