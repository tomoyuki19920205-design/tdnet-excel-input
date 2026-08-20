from tools.cleanup_review_completion_notifications import build_cleanup_plan


def _row(event_id: str, title: str, ticker: str = "1234", archived_at=None):
    return {
        "id": event_id,
        "disclosed_at": "2026-08-20T15:00:00+09:00",
        "ticker": ticker,
        "event_type": "earnings",
        "event_subtype": "1Q",
        "headline": title,
        "source_title": None,
        "archived_at": archived_at,
    }


def test_cleanup_plan_targets_only_procedural_notification_artifacts():
    rows = [
        _row("procedural", "2027年3月期 第1四半期決算短信（公認会計士等による期中レビューの完了）"),
        _row("material", "2027年3月期 第1四半期決算短信（訂正）（期中レビュー完了）", "5678"),
        _row("normal", "2027年3月期 第1四半期決算短信", "9999"),
    ]
    plan = build_cleanup_plan(rows)
    assert plan["review_completion_disclosures"] == 2
    assert plan["false_positive_candidates"] == 1
    assert plan["candidate_ids"] == ["procedural"]
    assert [row["id"] for row in plan["retained_material"]] == ["material"]


def test_cleanup_plan_suppresses_ambiguous_change_only_with_unchanged_evidence():
    ambiguous = _row("ambiguous", "2027年3月期 第1四半期決算短信（期中レビュー完了及び開示事項の変更）")
    unchanged = build_cleanup_plan([ambiguous], {"ambiguous": "financials_unchanged"})
    changed = build_cleanup_plan([ambiguous], {"ambiguous": "financials_changed"})
    unavailable = build_cleanup_plan([ambiguous], {"ambiguous": "comparison_unavailable"})
    assert unchanged["candidate_ids"] == ["ambiguous"]
    assert changed["candidate_ids"] == []
    assert unavailable["candidate_ids"] == []


def test_cleanup_plan_is_idempotent_for_already_archived_cards():
    rows = [
        _row(
            "archived",
            "2027年3月期 第1四半期決算短信（期中レビューの完了）",
            archived_at="2026-08-21T00:00:00+09:00",
        )
    ]
    plan = build_cleanup_plan(rows)
    assert plan["review_completion_disclosures"] == 1
    assert plan["false_positive_candidates"] == 0
    assert plan["already_archived_false_positives"] == 1
    assert plan["retained_material_change_candidates"] == 0


def test_cleanup_plan_deduplicates_offset_pagination_overlap_by_event_id():
    row = _row("same-id", "2027年3月期 第1四半期決算短信（期中レビュー完了）")
    plan = build_cleanup_plan([row, dict(row)])
    assert plan["review_completion_disclosures"] == 1
    assert plan["candidate_ids"] == ["same-id"]
