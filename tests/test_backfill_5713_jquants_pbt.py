from lib.pipeline.unit_convert import to_millions
from tools import backfill_5713_jquants_profit_before_tax as backfill
from tools.sync_financials import read_sqlite


FIELD = "Profit (loss) before tax from continuing operations (IFRS)"


def _detail(disc_no, date, quarter, raw):
    return {
        "Code": "57130",
        "DiscDate": date,
        "DiscNo": disc_no,
        "DiscTime": "15:00:00",
        "DocType": f"{quarter}FinancialStatements_Consolidated_IFRS",
        "FS": {FIELD: str(raw)},
    }


def _summary(disc_no, period, quarter):
    return {
        "Code": "57130",
        "DiscNo": disc_no,
        "DocType": f"{quarter}FinancialStatements_Consolidated_IFRS",
        "CurFYEn": period,
        "CurPerType": quarter,
    }


def test_unit_normalization_is_once_only():
    assert to_millions(37_901_000_000) == 37_901


def test_nightly_normalizer_carries_pbt_only_to_canonical_payload(tmp_path):
    import sqlite3
    db = tmp_path / "jquants.db"
    connection = sqlite3.connect(db)
    connection.execute("""
        CREATE TABLE jquants_financials_normalized (
            local_code TEXT, disclosed_date TEXT,
            current_fiscal_year_end_date TEXT, type_of_current_period TEXT,
            type_of_document TEXT, net_sales INTEGER, gross_profit INTEGER,
            operating_profit INTEGER, profit_before_tax INTEGER,
            raw_json TEXT, fetched_at TEXT
        )
    """)
    connection.execute(
        "INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("57130", "2025-11-10", "2026-03-31", "2Q",
         "2QFinancialStatements_Consolidated_IFRS", 1_000_000_000, None,
         None, 77_815_000_000, "{}", "2025-11-10T14:30:00+09:00"),
    )
    connection.commit()
    connection.close()
    rows, _ = read_sqlite(str(db), ticker="5713")
    assert rows[0]["ticker"] == "5713"
    assert rows[0]["profit_before_tax"] == 77_815
    assert rows[0]["operating_profit"] is None


def test_manifest_inserts_missing_and_preserves_official():
    details = [
        _detail("q2", "2025-11-10", "2Q", 77_815_000_000),
        _detail("fy", "2026-05-11", "FY", 255_680_000_000),
    ]
    summaries = {
        "q2": _summary("q2", "2026-03-31", "2Q"),
        "fy": _summary("fy", "2026-03-31", "FY"),
    }
    canonical = [
        {"ticker": "5713", "period": "2026-03-31", "quarter": "2Q", "metric": "sales", "value": 1, "source": "jquants"},
        {"ticker": "5713", "period": "2026-03-31", "quarter": "FY", "metric": "sales", "value": 1, "source": "jquants"},
        {"ticker": "5713", "period": "2026-03-31", "quarter": "FY", "metric": "profit_before_tax", "value": 255680, "source": "tdnet_xbrl"},
    ]
    manifest = backfill.build_manifest(details, "abc", summaries, canonical)
    by_quarter = {row["quarter"]: row for row in manifest["rows"]}
    assert by_quarter["2Q"]["intended_action"] == "INSERT_JQUANTS_PBT"
    assert by_quarter["2Q"]["canonical_normalized_value_millions_jpy"] == 77_815
    assert by_quarter["FY"]["intended_action"] == "NO_ACTION_OFFICIAL_PBT_EXISTS"
    assert manifest["expected_insert_count"] == 1
    assert manifest["official_rows_preserved_count"] == 1
    assert manifest["operating_profit_write_count"] == 0


def test_fy2024_fy2025_fy2026_fixtures_and_quarter_math():
    cumulative = {
        2024: [27_133, 53_779, 87_359, 95_795],
        2025: [30_688, 72_991, 48_139, 31_383],
        2026: [37_901, 77_815, 148_258, 255_680],
    }
    standalone = {
        year: [values[0], values[1] - values[0], values[2] - values[1], values[3] - values[2]]
        for year, values in cumulative.items()
    }
    assert standalone[2024] == [27_133, 26_646, 33_580, 8_436]
    assert standalone[2025] == [30_688, 42_303, -24_852, -16_756]
    assert standalone[2026] == [37_901, 39_914, 70_443, 107_422]


def test_apply_uses_single_attempt_and_exact_scope(monkeypatch):
    row = {
        "ticker": "5713", "period_end": "2026-03-31", "quarter": "2Q",
        "source": "jquants", "disclosure_number": "q2",
        "disclosure_datetime": "2025-11-10T14:30:00+09:00",
        "canonical_normalized_value_millions_jpy": 77_815,
        "source_row_key": "expected", "intended_action": "INSERT_JQUANTS_PBT",
    }
    manifest = {
        "scope": {"ticker": "5713", "jquants_code": "57130", "metric": "profit_before_tax", "actual_only": True, "consolidated_only": True},
        "operating_profit_write_count": 0,
        "expected_insert_count": 1,
        "rows": [row],
    }
    manifest["manifest_sha256"] = backfill._manifest_hash(manifest)
    monkeypatch.setattr(backfill, "get_supabase_write_config", lambda: {"headers": {}})
    reads = [
        [{"ticker": "5713", "period": "2026-03-31", "quarter": "2Q", "metric": "sales"}],
        [{"ticker": "5713", "period": "2026-03-31", "quarter": "2Q", "metric": "profit_before_tax", "source_row_key": "expected", "value": 77815}],
    ]
    monkeypatch.setattr(backfill, "_read_canonical", lambda config: reads.pop(0))
    monkeypatch.setattr(backfill, "expand_financials_rows", lambda **kwargs: ([{"source_row_key": "expected"}], []))
    observed = {}
    def fake_upsert(table, payload, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "count": 1}
    monkeypatch.setattr(backfill, "supabase_upsert", fake_upsert)
    result = backfill.apply_manifest(
        manifest, expected_count=1, expected_hash=manifest["manifest_sha256"],
        apply_token=backfill.APPLY_TOKEN,
    )
    assert result["written"] == 1
    assert observed["max_retries"] == 1
