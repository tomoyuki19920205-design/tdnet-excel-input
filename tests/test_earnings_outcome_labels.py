import sqlite3
from collections import Counter

import pytest

from tools.label_earnings_outcomes import (
    EXPECTED,
    LABEL_BASIS,
    LABEL_VERSION,
    classify_outcome,
    ensure_outcome_columns,
    save_outcomes,
    validate_expected_counts,
    validate_ratio_scale,
)


@pytest.mark.parametrize(
    ("ratio", "label", "band"),
    [
        (0.05, "success", "strong_success"),
        (0.02, "success", "success"),
        (0.0199, "neutral", "neutral"),
        (-0.0199, "neutral", "neutral"),
        (-0.02, "failure", "failure"),
        (-0.05, "failure", "large_failure"),
    ],
)
def test_outcome_boundaries(ratio: float, label: str, band: str) -> None:
    assert classify_outcome(True, ratio) == (True, label, band)


def test_invalid_reaction_window_is_excluded() -> None:
    assert classify_outcome(False, 0.10) == (False, "excluded", "excluded")


def test_missing_price_is_excluded() -> None:
    assert classify_outcome(True, None) == (False, "excluded", "excluded")


def test_db_value_is_verified_as_decimal_ratio_not_percent_value() -> None:
    valid = [{
        "code": "7203",
        "close_2026_07_15_adjusted": 100.0,
        "open_2026_07_16_adjusted": 102.0,
        "open_gap_return_pct": 0.02,
    }]
    validate_ratio_scale(valid)

    invalid = [{**valid[0], "open_gap_return_pct": 2.0}]
    with pytest.raises(RuntimeError, match="scale mismatch"):
        validate_ratio_scale(invalid)


def test_expected_counts_gate() -> None:
    validate_expected_counts(EXPECTED)
    mismatched = {
        **EXPECTED,
        "eligible": EXPECTED["eligible"] - 1,
        "excluded": EXPECTED["excluded"] + 1,
    }
    with pytest.raises(RuntimeError, match="save aborted"):
        validate_expected_counts(mismatched)


def _outcome(event_id: str, label: str = "success", band: str = "success") -> dict:
    return {
        "source_event_id": event_id,
        "outcome_eligible": True,
        "primary_outcome_label": label,
        "primary_outcome_band": band,
        "outcome_label_basis": LABEL_BASIS,
        "outcome_label_version": LABEL_VERSION,
        "outcome_labeled_at": "2026-07-16T00:00:00+00:00",
        "outcome_exclusion_reason": "",
    }


def test_rerun_updates_without_duplicate_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE earnings_reactions (
          source_event_id TEXT PRIMARY KEY,
          earnings_date TEXT NOT NULL,
          updated_at TEXT
        );
        INSERT INTO earnings_reactions VALUES
          ('event-1', '2026-07-15', NULL),
          ('event-2', '2026-07-15', NULL);
    """)
    added_first = ensure_outcome_columns(conn)
    added_second = ensure_outcome_columns(conn)
    assert set(added_first) >= {
        "outcome_eligible", "primary_outcome_label", "primary_outcome_band"
    }
    assert added_second == []

    outcomes = [_outcome("event-1"), _outcome("event-2", "failure", "large_failure")]
    assert save_outcomes(conn, outcomes) == 2
    assert save_outcomes(conn, outcomes) == 2
    assert conn.execute("SELECT count(*) FROM earnings_reactions").fetchone()[0] == 2
    assert Counter(
        row[0] for row in conn.execute(
            "SELECT primary_outcome_label FROM earnings_reactions"
        )
    ) == {"success": 1, "failure": 1}
