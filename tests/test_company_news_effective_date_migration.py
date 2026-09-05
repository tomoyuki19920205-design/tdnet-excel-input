from tools.company_news_effective_date_migration_dry_run import run_dry_run


def test_effective_date_migration_dry_run_is_reader_first_and_strict():
    result = run_dry_run()
    assert result["status"] == "PASS"
    assert result["legacy_exact_value_unchanged"] is True
    assert result["legacy_null_classified_unknown"] is True
    assert result["legacy_writer_compatible_during_additive_phase"] is True
    assert result["old_reader_compatible"] is True
    assert result["new_reader_compatible"] is True
    assert result["date_only_insert"] is True
    assert result["unknown_insert"] is True
    assert result["invalid_cross_field_rows_rejected"] == 3
    assert result["midnight_reclassification_attempted"] is False
    assert result["production_connection"] is False
