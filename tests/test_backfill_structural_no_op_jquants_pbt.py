from tools import backfill_structural_no_op_jquants_profit_before_tax as backfill


FIELD = "Profit (loss) before tax from continuing operations (IFRS)"


def _source(ticker: str, value: int, *, period: str = "2026-03-31", quarter: str = "FY"):
    code = backfill.COMPANY_CONFIG[ticker]["jquants_code"]
    disc_no = f"{ticker}-{quarter}"
    document_type = f"{quarter}FinancialStatements_Consolidated_IFRS"
    return {
        "details": [{
            "Code": code,
            "DiscDate": "2026-05-11",
            "DiscNo": disc_no,
            "DiscTime": "15:00:00",
            "DocType": document_type,
            "FS": {FIELD: str(value)},
        }],
        "details_sha256": f"details-{ticker}",
        "summaries": {disc_no: {
            "Code": code,
            "DiscNo": disc_no,
            "DocType": document_type,
            "CurFYEn": period,
            "CurPerType": quarter,
        }},
        "summary_sha256": f"summary-{ticker}",
    }


def _all_sources():
    return {
        ticker: _source(ticker, (index + 1) * 1_000_000_000)
        for index, ticker in enumerate(backfill.TICKERS)
    }


def _all_canonical():
    return {
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


def test_manifest_scope_is_exact_four_and_preserves_official_pbt():
    canonical = _all_canonical()
    canonical["8031"].append({
        "ticker": "8031",
        "period": "2026-03-31",
        "quarter": "FY",
        "metric": "profit_before_tax",
        "value": 1_087_056,
        "source": "tdnet_xbrl",
    })

    manifest = backfill.build_manifest(_all_sources(), canonical)

    assert manifest["scope"]["tickers"] == ["2282", "8031", "8058", "4819"]
    assert manifest["expected_insert_count"] == 3
    assert manifest["expected_insert_count_by_ticker"] == {
        "2282": 1, "8031": 0, "8058": 1, "4819": 1,
    }
    assert manifest["official_rows_preserved_count"] == 1
    assert manifest["operating_profit_write_count"] == 0
    row_8031 = next(row for row in manifest["rows"] if row["ticker"] == "8031")
    assert row_8031["intended_action"] == "NO_ACTION_OFFICIAL_PBT_EXISTS"


def test_manifest_rejects_any_scope_other_than_exact_four():
    sources = _all_sources()
    sources.pop("4819")
    try:
        backfill.build_manifest(sources, _all_canonical())
    except RuntimeError as exc:
        assert "exact approved ticker order" in str(exc)
    else:
        raise AssertionError("out-of-scope manifest input was accepted")


def test_latest_effective_forecast_or_nonconsolidated_rows_are_not_inserted():
    sources = _all_sources()
    rejected = _source("2282", 54_545_000_000)
    rejected["details"][0]["DocType"] = "FYFinancialStatements_NonConsolidated_IFRS"
    rejected["summaries"]["2282-FY"]["DocType"] = "FYFinancialStatements_NonConsolidated_IFRS"
    sources["2282"] = rejected

    manifest = backfill.build_manifest(sources, _all_canonical())

    row = next(row for row in manifest["rows"] if row["ticker"] == "2282")
    assert row["intended_action"] == "NO_ACTION_NO_VALID_PBT"
    assert manifest["expected_insert_count_by_ticker"]["2282"] == 0


def test_apply_writes_only_pbt_for_exact_four_scope(monkeypatch):
    manifest = backfill.build_manifest(_all_sources(), _all_canonical())
    targets = [row for row in manifest["rows"] if row["intended_action"] == "INSERT_JQUANTS_PBT"]
    monkeypatch.setattr(backfill, "get_supabase_write_config", lambda: {"headers": {}})

    reads = {ticker: 0 for ticker in backfill.TICKERS}
    def fake_read(config, ticker):
        reads[ticker] += 1
        if reads[ticker] == 1:
            return [{
                "ticker": ticker, "period": "2026-03-31", "quarter": "FY",
                "metric": "sales", "value": 1,
            }]
        target = next(row for row in targets if row["ticker"] == ticker)
        return [{
            "ticker": ticker, "period": target["period_end"], "quarter": target["quarter"],
            "metric": "profit_before_tax", "source_row_key": target["source_row_key"],
            "value": target["canonical_normalized_value_millions_jpy"],
        }]
    monkeypatch.setattr(backfill, "_read_canonical", fake_read)

    def fake_expand(**kwargs):
        target = next(
            row for row in targets
            if row["ticker"] == kwargs["ticker"] and row["quarter"] == kwargs["quarter"]
        )
        return ([{
            "ticker": kwargs["ticker"],
            "metric": next(iter(kwargs["metrics_dict"])),
            "source_row_key": target["source_row_key"],
        }], [])
    monkeypatch.setattr(backfill, "expand_financials_rows", fake_expand)

    observed = {}
    def fake_upsert(table, payload, **kwargs):
        observed["payload"] = payload
        observed.update(kwargs)
        return {"ok": True, "count": len(payload)}
    monkeypatch.setattr(backfill, "supabase_upsert", fake_upsert)

    result = backfill.apply_manifest(
        manifest,
        expected_count=4,
        expected_hash=manifest["manifest_sha256"],
        apply_token=backfill.APPLY_TOKEN,
    )

    assert result["written"] == 4
    assert result["operating_profit_written"] == 0
    assert {row["ticker"] for row in observed["payload"]} == set(backfill.TICKERS)
    assert {row["metric"] for row in observed["payload"]} == {"profit_before_tax"}
    assert observed["max_retries"] == 1
