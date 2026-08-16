from tools.fetch_jquants_financials import _details_row_to_db


def test_3925_correction_only_details_uses_exact_economic_period():
    row = _details_row_to_db({
        "Code": "39250",
        "DiscDate": "2025-08-19",
        "DiscNo": "20250819544106",
        "DocType": "1QFinancialStatements_Consolidated_JP",
        "FS": {
            "Current fiscal year end date, DEI": "2026-03-31",
            "Type of current period, DEI": "Q1",
            "Net sales": "1408904000",
            "Gross profit (loss)": "626241000",
            "Operating profit (loss)": "327224000",
            "Profit (loss) before income taxes": "307466000",
        },
    })
    assert row is not None
    assert row["current_fiscal_year_end_date"] == "2026-03-31"
    assert row["type_of_current_period"] == "1Q"
    assert row["net_sales"] == 1_408_904_000
    assert row["gross_profit"] == 626_241_000
    assert row["operating_profit"] == 327_224_000
