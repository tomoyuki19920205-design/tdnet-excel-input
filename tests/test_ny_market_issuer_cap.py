from datetime import date

import pytest

from lib.ny_market_issuer_cap import IssuerCapError, issuer_cap_from_evidence
from lib.ny_market_research import (
    NYMarketResearchError, attach_market_data_packet_metadata, validate_research_packet,
)
from tests.ny_market_quality_fixture import payload as quality_payload


SESSION = date(2026, 10, 23)
SOURCE = "https://www.sec.gov/Archives/edgar/data/1/filing.htm"


def evidence(classes, ratio=1):
    return {
        "status": "verified",
        "class_coverage": {"source_url": SOURCE, "as_of": "2026-10-20", "classes": [n for n, _ in classes],
                           "basis": "all issued common economic classes", "economic_equivalence_source_url": SOURCE},
        "components": [
            {"class": n, "shares_outstanding": count, "share_count_as_of": "2026-10-20",
             "share_count_source_url": SOURCE, "quoted_security_ratio": ratio,
             "quoted_security_ratio_source_url": SOURCE}
            for n, count in classes
        ],
    }


def test_all_economic_classes_are_priced_and_summed():
    cap, method, rows, _ = issuer_cap_from_evidence(
        "JYD", 3.0, SESSION, evidence([("Class A", 100), ("Class B", 20)]),
    )
    assert cap == 360
    assert method == "issuer_total_dual_class"
    assert [row["price"] for row in rows] == [3, 3]


def test_ads_ratio_prices_underlying_share_once():
    cap, method, rows, _ = issuer_cap_from_evidence("MYND", 0.24, SESSION, evidence([("ordinary", 100)], 10))
    assert cap == 2.4
    assert method == "issuer_total_ads"
    assert rows[0]["pricing_basis"] == "quoted_regular_close_divided_by_security_ratio"


def test_vendor_implied_count_and_missing_class_are_rejected():
    with pytest.raises(IssuerCapError, match="every economic class"):
        bad = evidence([("Class A", 100), ("Class B", 20)])
        bad["components"].pop()
        issuer_cap_from_evidence("JYD", 3.0, SESSION, bad)
    with pytest.raises(IssuerCapError, match="needs an outcome"):
        issuer_cap_from_evidence("JYD", 3.0, SESSION, {
            "status": "unavailable", "missing_items": ["current Class B count"],
            "attempted_sources": [{"url": SOURCE}, {"url": SOURCE, "outcome": "not found"}]
        })


def test_future_share_count_is_rejected():
    bad = evidence([("common", 100)])
    bad["components"][0]["share_count_as_of"] = "2026-10-24"
    with pytest.raises(IssuerCapError, match="after market session"):
        issuer_cap_from_evidence("AIOK", 12.02, SESSION, bad)


def test_split_requires_post_action_class_and_share_counts():
    action = {"symbol": "AIOK", "effective_session_date": "2026-10-22", "new_shares": 1, "old_shares": 9}
    old = evidence([("common", 900)])
    old["class_coverage"]["as_of"] = "2026-10-21"
    old["components"][0]["share_count_as_of"] = "2026-10-21"
    with pytest.raises(IssuerCapError, match="class coverage predates split"):
        issuer_cap_from_evidence("AIOK", 9, SESSION, old, action)
    old["class_coverage"]["as_of"] = "2026-10-22"
    with pytest.raises(IssuerCapError, match="share count predates split"):
        issuer_cap_from_evidence("AIOK", 9, SESSION, old, action)
    old["components"][0]["share_count_as_of"] = "2026-10-22"
    old["components"][0]["shares_outstanding"] = 100
    cap, _, _, _ = issuer_cap_from_evidence("AIOK", 9, SESSION, old, action)
    assert cap == 900


def test_v2_research_validator_rejects_vendor_cap_and_accepts_documented_unavailable():
    report = quality_payload()
    packet = report["canonical_market_data"]
    packet["market_data_contract_version"] = "ny_market_data_v2"
    attach_market_data_packet_metadata(report)
    with pytest.raises(NYMarketResearchError, match="issuer-total evidence invalid"):
        validate_research_packet(report)
    attempts = [{"url": SOURCE, "outcome": "No current class counts"},
                {"url": "https://stockanalysis.com/stocks/example/statistics/", "outcome": "Vendor cap only"}]
    for canonical, research in zip(packet["top_gainers_20"], report["ticker_research"]):
        evidence_row = {"status": "unavailable", "missing_items": ["current outstanding shares"],
                        "attempted_sources": attempts}
        for row in (canonical, research):
            row.update(market_cap=None, market_cap_method="unavailable",
                       share_class_components=[], market_cap_evidence=evidence_row)
    attach_market_data_packet_metadata(report)
    validate_research_packet(report)
    report["ticker_research"][0]["market_cap_evidence"] = {"status": "unavailable"}
    with pytest.raises(NYMarketResearchError, match="market_cap_evidence differs"):
        validate_research_packet(report)


def test_new_report_cannot_publish_provisional_vendor_cap_packet():
    report = quality_payload()
    report["report_date_jst"] = "2026-10-24"
    with pytest.raises(NYMarketResearchError, match="require issuer-total"):
        validate_research_packet(report)
