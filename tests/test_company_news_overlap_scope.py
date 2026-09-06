import pytest

from tools.company_news_overlap_scope import (
    IrNightlyCoverageRecord,
    OverlapScopeError,
    apply_ir_nightly_overlap,
    assert_incremental_ir_candidate_allowed,
    classify_news_monitor_scope,
    classify_xj_inventory_entry,
    remove_properly_excluded_document_gaps,
)


PDF = "https://example.test/material.pdf"


def test_earnings_presentation_is_out_of_scope_ir_nightly():
    result = classify_news_monitor_scope("2026年12月期第2四半期決算説明資料", PDF)
    assert result["scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"
    assert not result["body_fetch_required"] and not result["candidate_allowed"]


def test_nightly_inventory_authority_can_cover_medium_term_plan():
    record = IrNightlyCoverageRecord(True, "earnings_material", "IR_NIGHTLY_INVENTORY")
    assert classify_news_monitor_scope("中期経営計画説明資料", PDF, nightly_record=record)[
        "scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"


def test_earnings_supplement_follows_nightly_classifier():
    assert classify_news_monitor_scope("2026年12月期決算補足資料", PDF)[
        "scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"


@pytest.mark.parametrize("title", ["製品価格改定のお知らせ", "大手顧客から新規受注"])
def test_ir_page_does_not_blanket_exclude_company_news(title):
    result = classify_news_monitor_scope(title, "https://example.test/ir/news/release.pdf")
    assert result["scope_label"] == "IN_SCOPE_COMPANY_WEB"
    assert result["candidate_allowed"]


def test_tdnet_exclusion_is_distinct_and_precedes_nightly():
    result = classify_news_monitor_scope("第三者割当による新株式発行", PDF, tdnet_covered=True)
    assert result["scope_label"] == "OUT_OF_SCOPE_TDNET"


def test_nightly_document_cannot_be_incremental_ir_candidate():
    with pytest.raises(OverlapScopeError, match="IR_NIGHTLY_COVERED_DOCUMENT"):
        assert_incremental_ir_candidate_allowed({
            "title": "決算説明資料", "url": PDF,
            "scope_label": "IN_SCOPE_INCREMENTAL_IR",
        })


def test_excluded_document_is_not_a_document_access_gap():
    documents = [{"url": PDF, "scope_label": "OUT_OF_SCOPE_IR_NIGHTLY"}]
    gaps = [{"gap_type": "document_access", "url": PDF},
            {"gap_type": "query_execution", "url": None}]
    assert remove_properly_excluded_document_gaps(documents, gaps) == [gaps[1]]


def test_xj_inventory_retains_document_without_body_or_candidate():
    result = classify_xj_inventory_entry({
        "title": "決算説明資料", "canonical_document_candidates": [{"url": PDF}],
    })
    assert result["inventory_retained"]
    assert result["scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"
    assert not result["body_fetch_required"] and not result["llm_materiality_required"]


def test_6444_replay_needs_no_worker_b_and_leaves_autotimes_only():
    response = {
        "research_result": {"items": [{"headline": "2026年12月期第2四半期決算説明資料",
                                          "source_url": PDF}]},
        "strategy_telemetry": {
            "documents": [{
                "title": "2026年12月期第2四半期決算説明資料", "url": PDF,
                "scope_label": "IN_SCOPE_INCREMENTAL_IR", "scope_reason": "synthesis",
                "material": True, "body_status": "INACCESSIBLE",
                "attempt_evidence": "not fetched", "incremental_information": True,
                "incremental_information_evidence": "claimed delta",
            }],
            "candidate_provenance": [{
                "document_url": PDF, "selected_for_output": True,
                "selection_reason": "synthesis", "source_url": PDF,
                "headline": "2026年12月期第2四半期決算説明資料",
                "economic_transmission": "earnings", "event_key": "briefing",
            }],
            "final_event_count": 1,
        },
    }
    # Contradictory incremental scope fails closed before replay correction.
    with pytest.raises(OverlapScopeError, match="IR_NIGHTLY_COVERED_DOCUMENT"):
        apply_ir_nightly_overlap(response)
    response["strategy_telemetry"]["documents"][0]["scope_label"] = "IN_SCOPE_COMPANY_WEB"
    repaired = apply_ir_nightly_overlap(response)
    document = repaired["strategy_telemetry"]["documents"][0]
    assert document["scope_label"] == "OUT_OF_SCOPE_IR_NIGHTLY"
    assert document["body_status"] == "SKIPPED"
    assert repaired["research_result"]["items"] == []
    assert repaired["strategy_telemetry"]["final_event_count"] == 0
    gaps = [
        {"gap_type": "document_access", "url": PDF, "reason": "briefing body"},
        {"gap_type": "document_access", "url": "https://autotimes.example/article",
         "reason": "Autotimes candidate body"},
    ]
    remaining = remove_properly_excluded_document_gaps(
        repaired["strategy_telemetry"]["documents"], gaps)
    assert [row["reason"] for row in remaining] == ["Autotimes candidate body"]
