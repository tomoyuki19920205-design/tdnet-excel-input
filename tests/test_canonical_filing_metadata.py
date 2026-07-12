import json
import sqlite3

import lib.backfill.canonical_filing_metadata as metadata


def _db(tmp_path, rows):
    path = tmp_path / "jquants.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jquants_financials_normalized (local_code TEXT, disclosed_date TEXT, current_fiscal_year_end_date TEXT, type_of_current_period TEXT, raw_json TEXT)")
    conn.executemany("INSERT INTO jquants_financials_normalized VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit(); conn.close()
    return path


def _row(disc, code="76010", fy="2027-02-28", q="1Q", raw=None):
    return (code, "2026-07-10", fy, q, json.dumps(raw if raw is not None else {"DiscNo": disc, "CurPerEn": "2026-05-31", "CurFYEn": fy, "CurPerType": q}))


def test_exact_canaries_not_found_and_fuzzy_rejection(tmp_path):
    path = _db(tmp_path, [_row("20260709590505"), _row("20260709590450", "99820")])
    index = metadata.load_canonical_filing_metadata_index(str(path))
    assert index["20260709590505"].match_status == "exact_requested_disclosure_match"
    assert (index["20260709590505"].normalized_ticker, index["20260709590505"].expected_period, index["20260709590505"].expected_quarter) == ("7601", "2027-02-28", "1Q")
    assert index["20260709590450"].normalized_ticker == "9982"
    assert index.get("20260709590504") is None


def test_duplicate_invalid_and_malformed_are_not_selected(tmp_path):
    path = _db(tmp_path, [_row("20260709590505"), _row("20260709590505", "99820"), _row("20260709590450", fy=""), _row("20260709590451", q="bogus"), ("76010", "2026-07-10", "2027-02-28", "1Q", "{")])
    index = metadata.load_canonical_filing_metadata_index(str(path))
    assert index["20260709590505"].match_status == "duplicate"
    assert index["20260709590505"].expected_period == ""
    assert index["20260709590450"].match_status == "invalid_period"
    assert index["20260709590451"].match_status == "invalid_quarter"


def test_read_only_uri_and_query_only_are_executed(tmp_path, monkeypatch):
    path = _db(tmp_path, [_row("20260709590505")])
    real_connect = metadata.sqlite3.connect
    calls, sql = [], []
    class Conn:
        def __init__(self, conn): self.conn = conn
        def execute(self, statement): sql.append(statement); return self.conn.execute(statement)
        def close(self): return self.conn.close()
    def connect(*args, **kwargs):
        calls.append((args, kwargs)); return Conn(real_connect(*args, **kwargs))
    monkeypatch.setattr(metadata.sqlite3, "connect", connect)
    metadata.load_canonical_filing_metadata_index(str(path))
    assert "mode=ro" in calls[0][0][0] and calls[0][1]["uri"] is True
    assert "data/jquants.db" not in calls[0][0][0]
    assert "PRAGMA query_only=ON" in sql


def test_ticker_conflict_and_fuzzy_disc_no_are_not_rescued(tmp_path):
    path = _db(tmp_path, [_row("20260709590505", "99820"), _row("20260709590506", "76010")])
    index = metadata.load_canonical_filing_metadata_index(str(path))
    conflict = index["20260709590505"]
    assert conflict.normalized_ticker == "9982"
    assert conflict.expected_period == "2027-02-28"
    assert index.get("20260709590504") is None


def test_curperen_is_required_and_curperend_is_not_a_fallback(tmp_path):
    path = _db(tmp_path, [
        _row("20260709590505", "76010"),
        _row("20260709590450", "99820"),
        _row("20260709590506", raw={"DiscNo": "20260709590506", "CurPerEnd": "2026-05-31", "CurFYEn": "2027-02-28", "CurPerType": "1Q"}),
        _row("20260709590507", raw={"DiscNo": "20260709590507", "CurFYEn": "2027-02-28", "CurPerType": "1Q"}),
        _row("20260709590508", raw={"DiscNo": "20260709590508", "CurPerEn": "UNKNOWN", "CurFYEn": "2027-02-28", "CurPerType": "1Q"}),
    ])
    index = metadata.load_canonical_filing_metadata_index(str(path))

    assert (index["20260709590505"].match_status, index["20260709590505"].normalized_ticker, index["20260709590505"].expected_period, index["20260709590505"].expected_quarter) == ("exact_requested_disclosure_match", "7601", "2027-02-28", "1Q")
    assert (index["20260709590450"].match_status, index["20260709590450"].normalized_ticker, index["20260709590450"].expected_period, index["20260709590450"].expected_quarter) == ("exact_requested_disclosure_match", "9982", "2027-02-28", "1Q")
    assert index["20260709590506"].match_status == "invalid_period"
    assert index["20260709590506"].expected_period == ""
    assert index["20260709590507"].match_status == "invalid_period"
    assert index["20260709590508"].match_status == "invalid_period"


def test_ticker_conflict_does_not_mutate_or_rescue_other_disclosure():
    from lib.backfill.listing_sources.base import FilingInfo
    import tools.backfill_segments_tdnet as tool

    def filing(filing_id, disclosure_no, ticker, period, quarter):
        return FilingInfo(filing_id, ticker, "title", "2026-07-10", "pdf", None, "financial_statement", "company", "", "tdnet_html", False, requested_disclosure_no=disclosure_no, expected_period=period, expected_quarter=quarter)

    conflict = filing("conflict", "20260709590505", "7601", "existing-period", "existing-quarter")
    untouched = filing("untouched", "20260709590507", "7601", "other-period", "other-quarter")
    canonical_index = {
        "20260709590505": metadata.CanonicalFilingMetadata(requested_disclosure_no="20260709590505", normalized_ticker="9982", expected_period="2027-02-28", expected_quarter="1Q", match_status="exact_requested_disclosure_match"),
        "20260709590506": metadata.CanonicalFilingMetadata(requested_disclosure_no="20260709590506", normalized_ticker="7601", expected_period="2027-02-28", expected_quarter="1Q", match_status="exact_requested_disclosure_match"),
        "20260709590507": metadata.CanonicalFilingMetadata(requested_disclosure_no="20260709590507", match_status="duplicate"),
    }

    counts = tool._apply_canonical_metadata_to_filings([conflict, untouched], canonical_index)

    assert (conflict.expected_period, conflict.expected_quarter) == ("existing-period", "existing-quarter")
    assert (untouched.expected_period, untouched.expected_quarter) == ("other-period", "other-quarter")
    assert counts["canonical_metadata_matched"] == 0
    assert counts["canonical_metadata_ticker_conflict"] == 1
    assert counts["canonical_metadata_not_found"] == 0
    assert counts["canonical_metadata_invalid"] == 0
    assert counts["canonical_metadata_conflict"] == 0
    assert counts["canonical_metadata_duplicate"] == 1
