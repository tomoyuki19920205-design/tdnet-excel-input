from tools import backfill_financial_ifrs_jquants_profit_before_tax as backfill


FIELD = "Profit (loss) before tax from continuing operations (IFRS)"


def _statement(ticker, period, quarter, standard, value, suffix=""):
    code = backfill.COMPANY_CONFIG[ticker]["jquants_code"]
    disc_no = f"{ticker}-{period}-{quarter}-{standard}{suffix}"
    document_type = f"{quarter}FinancialStatements_Consolidated_{standard}"
    detail = {
        "Code": code,
        "DiscDate": "2026-05-15",
        "DiscTime": "15:00:00",
        "DiscNo": disc_no,
        "DocType": document_type,
        "FS": {FIELD: str(value)},
    }
    summary = {
        "Code": code,
        "DiscNo": disc_no,
        "DocType": document_type,
        "CurFYEn": period,
        "CurPerType": quarter,
    }
    return detail, summary


def _source(ticker, statements):
    details = []
    summaries = {}
    for period, quarter, standard, value in statements:
        detail, summary = _statement(ticker, period, quarter, standard, value)
        details.append(detail)
        summaries[detail["DiscNo"]] = summary
    return {
        "details": details,
        "details_sha256": f"details-{ticker}",
        "summaries": summaries,
        "summary_sha256": f"summary-{ticker}",
    }


def _all_sources():
    sources = {
        ticker: _source(ticker, [("2026-03-31", "FY", "IFRS", (index + 1) * 1_000_000_000)])
        for index, ticker in enumerate(backfill.TICKERS)
    }
    sources["8630"] = _source("8630", [
        ("2025-03-31", "3Q", "JP", 111_000_000),
        ("2025-03-31", "FY", "IFRS", 222_000_000),
        ("2026-03-31", "FY", "IFRS", 333_000_000),
    ])
    return sources


def _all_canonical():
    canonical = {
        ticker: [{
            "ticker": ticker,
            "period": "2026-03-31",
            "quarter": "FY",
            "metric": "sales",
            "value": 1,
            "source": "jquants",
        }]
        for ticker in backfill.TICKERS
    }
    canonical["8630"] = [
        {"ticker": "8630", "period": "2025-03-31", "quarter": "3Q", "metric": "sales", "value": 1, "source": "jquants"},
        {"ticker": "8630", "period": "2025-03-31", "quarter": "FY", "metric": "sales", "value": 1, "source": "jquants"},
        {"ticker": "8630", "period": "2026-03-31", "quarter": "FY", "metric": "sales", "value": 1, "source": "jquants"},
    ]
    return canonical


def test_manifest_is_exact_six_ifrs_only_and_preserves_official():
    canonical = _all_canonical()
    canonical["8473"].append({
        "ticker": "8473",
        "period": "2026-03-31",
        "quarter": "FY",
        "metric": "profit_before_tax",
        "value": 100,
        "source": "tdnet_xbrl",
    })

    manifest = backfill.build_manifest(_all_sources(), canonical)

    assert manifest["scope"]["tickers"] == ["7198", "8473", "8698", "8253", "7157", "8630"]
    assert manifest["scope"]["accounting_standard"] == "IFRS"
    assert manifest["expected_insert_count"] == 6
    assert manifest["official_rows_preserved_count"] == 1
    assert manifest["operating_profit_write_count"] == 0
    rows_8630 = {
        (row["period_end"], row["quarter"]): row for row in manifest["rows"] if row["ticker"] == "8630"
    }
    assert rows_8630[("2025-03-31", "3Q")]["intended_action"] == "NO_ACTION_NON_IFRS_PERIOD"
    assert rows_8630[("2025-03-31", "3Q")]["accounting_standard"] == "JP"
    assert rows_8630[("2025-03-31", "FY")]["intended_action"] == "INSERT_JQUANTS_PBT"
    assert rows_8630[("2025-03-31", "FY")]["accounting_standard"] == "IFRS"


def test_latest_effective_statement_metadata_rejects_nonconsolidated():
    ticker = "8253"
    source = _source(ticker, [("2026-03-31", "FY", "IFRS", 55_536_000_000)])
    detail = source["details"][0]
    summary = source["summaries"][detail["DiscNo"]]
    detail["DocType"] = "FYFinancialStatements_NonConsolidated_IFRS"
    summary["DocType"] = detail["DocType"]

    metadata = backfill.select_latest_actual_statement_metadata(
        source["details"], source["summaries"], expected_code="82530"
    )

    assert metadata == {}


def test_apply_rejects_non_ifrs_insert_even_with_valid_hash():
    manifest = {
        "scope": backfill._expected_scope(),
        "operating_profit_write_count": 0,
        "expected_insert_count": 1,
        "rows": [{
            "ticker": "8630",
            "period_end": "2025-03-31",
            "quarter": "3Q",
            "accounting_standard": "JP",
            "source": "jquants",
            "intended_action": "INSERT_JQUANTS_PBT",
        }],
    }
    manifest["manifest_sha256"] = backfill._manifest_hash(manifest)

    try:
        backfill.apply_manifest(
            manifest,
            expected_count=1,
            expected_hash=manifest["manifest_sha256"],
            apply_token=backfill.APPLY_TOKEN,
        )
    except RuntimeError as exc:
        assert "non-IFRS" in str(exc)
    else:
        raise AssertionError("non-IFRS insert was accepted")


def test_manifest_requires_all_and_only_six_tickers():
    sources = _all_sources()
    sources.pop("8630")
    try:
        backfill.build_manifest(sources, _all_canonical())
    except RuntimeError as exc:
        assert "exact approved ticker order" in str(exc)
    else:
        raise AssertionError("partial scope was accepted")


def test_forecast_canonical_period_never_enters_actual_manifest():
    canonical = _all_canonical()
    canonical["7198"].append({
        "ticker": "7198",
        "period": "2028-03-31",
        "quarter": "FY",
        "metric": "sales",
        "value": 999,
        "source": "jquants_nxf",
    })

    manifest = backfill.build_manifest(_all_sources(), canonical)

    assert not any(
        row["ticker"] == "7198" and row["period_end"] == "2028-03-31"
        for row in manifest["rows"]
    )


def test_unprovenanced_legacy_tdnet_row_never_enters_actual_manifest():
    canonical = _all_canonical()
    canonical["7198"].append({
        "ticker": "7198",
        "period": "2028-03-31",
        "quarter": "FY",
        "metric": "sales",
        "value": 999,
        "source": "tdnet",
        "filing_id": None,
        "disclosure_datetime": None,
    })

    manifest = backfill.build_manifest(_all_sources(), canonical)

    assert not any(
        row["ticker"] == "7198" and row["period_end"] == "2028-03-31"
        for row in manifest["rows"]
    )


def test_actual_period_with_only_non_sales_canonical_metric_is_in_scope():
    sources = _all_sources()
    detail, summary = _statement("8630", "2027-03-31", "FY", "IFRS", 444_000_000)
    sources["8630"]["details"].append(detail)
    sources["8630"]["summaries"][detail["DiscNo"]] = summary
    canonical = _all_canonical()
    canonical["8630"].append({
        "ticker": "8630",
        "period": "2027-03-31",
        "quarter": "FY",
        "metric": "net_income",
        "value": 300,
        "source": "jquants",
    })

    manifest = backfill.build_manifest(sources, canonical)
    row = next(
        row for row in manifest["rows"]
        if row["ticker"] == "8630" and row["period_end"] == "2027-03-31"
    )

    assert row["intended_action"] == "INSERT_JQUANTS_PBT"
    assert row["canonical_normalized_value_millions_jpy"] == 444
