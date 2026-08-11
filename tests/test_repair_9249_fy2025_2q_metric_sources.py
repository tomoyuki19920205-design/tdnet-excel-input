import pytest

from tools.repair_9249_fy2025_2q_metric_sources import (
    build_manifest,
    normalized_metrics,
    validate_source_payload,
)


def _raw():
    return {
        "Code": "92490", "DiscDate": "2025-05-15",
        "DiscNo": "20250515552859", "DocType": "2QFinancialStatements_Consolidated_JP",
        "CurPerType": "2Q", "CurPerSt": "2024-10-01", "CurPerEn": "2025-03-31",
        "CurFYEn": "2025-09-30", "Sales": "5634000000", "_gross_profit": 1583868000,
        "OP": "423000000", "OdP": "434000000", "NP": "523000000",
        "NCSales": "", "NCOP": "", "NCOdP": "", "NCNP": "",
    }


def _canonical():
    base = {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "filing_id": None, "disclosure_datetime": None}
    return [
        {**base, "metric": "sales", "value": 5634, "source": "tdnet", "source_row_key": "cf|9249|2025-09-30|2Q|sales|tdnet|"},
        {**base, "metric": "operating_profit", "value": 423, "source": "tdnet", "source_row_key": "cf|9249|2025-09-30|2Q|operating_profit|tdnet|"},
        {**base, "metric": "gross_profit", "value": 1583, "source": "jquants", "source_row_key": "cf|9249|2025-09-30|2Q|gross_profit|jquants|"},
    ]


def test_manifest_enriches_existing_metrics_and_inserts_only_missing_metrics():
    manifest = build_manifest(_raw(), _canonical())
    actions = {row["metric"]: row["intended_action"] for row in manifest["rows"]}

    assert actions == {
        "sales": "UPDATE_TDNET_PROVENANCE",
        "operating_profit": "UPDATE_TDNET_PROVENANCE",
        "gross_profit": "NO_ACTION_VALID_JQUANTS",
        "ordinary_profit": "INSERT_JQUANTS_ACTUAL",
        "net_income": "INSERT_JQUANTS_ACTUAL",
    }
    assert manifest["expected_insert_count"] == 2
    assert manifest["expected_update_count"] == 2
    assert manifest["expected_delete_count"] == 0


def test_metric_normalization_uses_consolidated_raw_payload_values():
    assert normalized_metrics(_raw()) == {
        "sales": 5634,
        "gross_profit": 1583,
        "operating_profit": 423,
        "ordinary_profit": 434,
        "net_income": 523,
    }


def test_manifest_rejects_nonconsolidated_metric_contamination():
    raw = _raw()
    raw["NCOP"] = "999000000"
    with pytest.raises(RuntimeError, match="nonconsolidated"):
        validate_source_payload(raw)
