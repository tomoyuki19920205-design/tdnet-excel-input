from __future__ import annotations

import sqlite3

from tools.audit_local_alphanumeric_security_code_collisions import audit_local_codes


def test_local_audit_keeps_numeric_and_alpha_raw_codes_distinct(tmp_path) -> None:
    database = tmp_path / "jquants.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jquants_financials_normalized (local_code TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO jquants_financials_normalized(local_code) VALUES (?)",
            [("41700",), ("41700",), ("417A0",), ("41800",), ("418A0",)],
        )
    report = audit_local_codes(database)
    by_alpha = {row["alpha_ticker"]: row for row in report["candidate_pairs"]}
    assert by_alpha["417A"]["numeric_normalized"] == "4170"
    assert by_alpha["417A"]["alpha_normalized"] == "417A"
    assert by_alpha["417A"]["normalization_collision"] is False
    assert by_alpha["418A"]["numeric_normalized"] == "4180"
    assert report["confirmed_normalizer_collisions"] == 0
