from pathlib import Path

import pytest

from tools import company_news_phase19b_offline_replay as phase19b


ROOT = Path(__file__).resolve().parents[1]


def test_saved_6444_offline_replay_is_bounded_and_preserves_evidence_gap(tmp_path):
    source = (ROOT / "artifacts" / "token_efficiency_phase18b_7760"
              / "companies" / "02-6444" / "workers" / "final-synthesis" / "last-message.json")
    if not source.exists():
        pytest.skip("saved Phase1.9 6444 artifact is unavailable")
    result = phase19b.replay(ROOT, tmp_path / "phase19b")
    assert result["expected_result"] == "UNKNOWN"
    assert result["levo_materiality"] == "FAIL_GENERAL_CONTEXT"
    assert result["identity_provenance"]["classification"] == "B"
    assert result["identity_provenance"]["actual_official_page_verified"] is False
    assert result["incremental_ir"] == {
        "applicable": True, "executed_records": 1, "contract_status": "PASS"}
    assert result["materiality_status"] == "PASS"
    assert result["token_total"] == 221_404
    assert result["llm_calls"] == result["web_calls"] == result["research_companies"] == 0
    assert result["other_company_runs"] == {"4883": 0, "5122": 0}
    assert result["overall_status"] == "WARN_TOKEN_EFFICIENCY_6444_PHASE19_EVIDENCE_REVIEW"
