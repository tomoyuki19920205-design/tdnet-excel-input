from src.tdnet_summary_actuals import SummaryActualFact
from tools.repair_9249_2q_official_xbrl import build_manifest


def _fact(metric, value):
    return SummaryActualFact(
        metric=metric, value_jpy=value, qname=f"tse-ed-t:{metric}",
        local_name=metric, namespace="urn:tdnet", context="CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember",
        period_start="2025-10-01", period_end="2026-03-31",
        members=("ConsolidatedMember", "ResultMember"),
        dimensions=("ConsolidatedNonconsolidatedAxis", "ResultForecastAxis"),
        unit_ref="JPY", scale=6, source_file="Summary/sample.htm",
    )


def test_manifest_inserts_official_2q_and_deletes_only_misperiodized_sales_op():
    facts = {
        "sales": _fact("sales", 7_882_000_000),
        "operating_profit": _fact("operating_profit", 1_019_000_000),
        "ordinary_profit": _fact("ordinary_profit", 1_019_000_000),
        "net_income": _fact("net_income", 687_000_000),
    }
    canonical = [
        {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "metric": "sales", "value": 7882, "source": "jquants", "source_row_key": "cf|9249|2025-09-30|2Q|sales|jquants|"},
        {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "metric": "operating_profit", "value": 1019, "source": "jquants", "source_row_key": "cf|9249|2025-09-30|2Q|operating_profit|jquants|"},
        {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "metric": "gross_profit", "value": 1583, "source": "jquants", "source_row_key": "cf|9249|2025-09-30|2Q|gross_profit|jquants|"},
        {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "metric": "sales", "value": 5634, "source": "tdnet", "source_row_key": "cf|9249|2025-09-30|2Q|sales|tdnet|"},
        {"ticker": "9249", "period": "2025-09-30", "quarter": "2Q", "metric": "operating_profit", "value": 423, "source": "tdnet", "source_row_key": "cf|9249|2025-09-30|2Q|operating_profit|tdnet|"},
    ]

    manifest = build_manifest(facts, canonical, package_sha256="a" * 64)

    assert manifest["expected_insert_count"] == 4
    assert manifest["expected_update_count"] == 0
    assert manifest["expected_delete_count"] == 2
    deletes = [row for row in manifest["rows"] if row["intended_action"] == "DELETE_MISPERIODIZED_JQUANTS"]
    assert {row["metric"] for row in deletes} == {"sales", "operating_profit"}
    assert all(row["metric"] != "gross_profit" for row in deletes)
