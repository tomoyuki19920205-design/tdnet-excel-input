from tools.fetch_jquants_financials import _to_local_code
from tools.sqlite_to_supabase import _build_financials_rows_from_tdnet


def test_319a_sec_code_normalization():
    assert _to_local_code("319A") == "319A0"
    assert _to_local_code("319A0") == "319A0"


def test_tdnet_million_yen_row_is_not_scaled_twice():
    rows = _build_financials_rows_from_tdnet([{
        "company_code": "319A",
        "fiscal_year_end": "2025-12-31",
        "quarter": "FY",
        "sales": 14_961,
        "gross_profit": 4_238,
        "operating_profit": 1_432,
        "cost_of_sales": None,
        "unit": "百万円",
        "field_sources": '{"sales":"summary_xbrl"}',
        "source_url": "https://www.release.tdnet.info/inbs/example.zip",
    }])
    assert len(rows) == 1
    assert rows[0]["ticker"] == "319A"
    assert rows[0]["sales"] == 14_961
    assert rows[0]["operating_profit"] == 1_432
