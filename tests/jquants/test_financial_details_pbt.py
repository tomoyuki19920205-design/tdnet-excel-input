from src.jquants.financial_details import (
    extract_pbt_from_fs,
    has_plausible_actual_period_metadata,
    normalize_actual_consolidated_pbt,
    select_latest_effective_pbt,
)


PBT_FIELD = "Profit (loss) before tax from continuing operations (IFRS)"


def _summary(*, disc_no="1", doc="2QFinancialStatements_Consolidated_IFRS", value=None):
    item = {
        "Code": "57130",
        "DiscNo": disc_no,
        "DocType": doc,
        "CurFYEn": "2026-03-31",
        "CurPerType": "2Q",
    }
    if value is not None:
        item[PBT_FIELD] = value
    return item


def _detail(*, disc_no="1", date="2025-11-10", doc="2QFinancialStatements_Consolidated_IFRS", field=PBT_FIELD, value="77815000000.0"):
    return {
        "Code": "57130",
        "DiscDate": date,
        "DiscNo": disc_no,
        "DiscTime": "14:30:00",
        "DocType": doc,
        "FS": {field: value},
    }


def test_extracts_exact_actual_pbt_field_and_raw_yen():
    assert extract_pbt_from_fs({PBT_FIELD: "37901000000.0"}) == (
        PBT_FIELD,
        37_901_000_000,
    )


def test_normalizes_actual_consolidated_scope():
    record = normalize_actual_consolidated_pbt(
        _detail(), _summary(), expected_code="57130"
    )
    assert record is not None
    assert record.raw_value_jpy == 77_815_000_000
    assert record.actual_scope == "actual_consolidated"
    assert record.accounting_standard == "IFRS"


def test_rejects_non_consolidated_and_forecast_documents():
    non_consolidated = "2QFinancialStatements_NonConsolidated_IFRS"
    assert normalize_actual_consolidated_pbt(
        _detail(doc=non_consolidated),
        _summary(doc=non_consolidated),
        expected_code="57130",
    ) is None
    forecast = "ForecastRevision"
    assert normalize_actual_consolidated_pbt(
        _detail(doc=forecast),
        _summary(doc=forecast),
        expected_code="57130",
    ) is None


def test_rejects_nc_prefixed_or_forecast_pbt_fallback():
    assert extract_pbt_from_fs({"NCProfitBeforeTax": "77815000000"}) is None
    assert extract_pbt_from_fs({"ForecastProfitBeforeTax": "77815000000"}) is None


def test_latest_effective_disclosure_is_selected_before_extraction():
    old = _detail(disc_no="1", date="2024-05-09", value="95795000000")
    new = _detail(disc_no="2", date="2024-07-31", value="95795000000")
    old_summary = _summary(disc_no="1")
    new_summary = _summary(disc_no="2")
    selected, _ = select_latest_effective_pbt(
        [old, new], {"1": old_summary, "2": new_summary}, expected_code="57130"
    )
    assert len(selected) == 1
    assert selected[0].disclosure_number == "2"


def test_latest_correction_without_pbt_does_not_fall_back():
    old = _detail(disc_no="1", date="2024-05-09", value="95795000000")
    new = _detail(disc_no="2", date="2024-07-31", field="Revenue", value="1")
    selected, audit = select_latest_effective_pbt(
        [old, new], {"1": _summary(disc_no="1"), "2": _summary(disc_no="2")},
        expected_code="57130",
    )
    assert selected == []
    assert any(item.get("reason") == "latest_effective_has_no_exact_pbt" for item in audit)


def test_interim_period_metadata_rejects_prior_fiscal_year_assignment():
    assert not has_plausible_actual_period_metadata({
        "DocType": "2QFinancialStatements_Consolidated_JP",
        "CurPerType": "2Q",
        "DiscDate": "2026-05-15",
        "CurFYEn": "2025-09-30",
    })
    assert has_plausible_actual_period_metadata({
        "DocType": "2QFinancialStatements_Consolidated_JP",
        "CurPerType": "2Q",
        "DiscDate": "2026-05-15",
        "CurFYEn": "2026-09-30",
    })
