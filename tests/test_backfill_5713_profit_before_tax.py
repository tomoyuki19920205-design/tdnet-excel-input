from tools import backfill_5713_profit_before_tax as backfill


def test_apply_uses_one_attempt_without_retry(monkeypatch):
    row = {
        "ticker": "5713",
        "metric": "profit_before_tax",
        "period": "2026-03-31",
        "quarter": "FY",
        "value": 255_680,
        "source_row_key": "5713:2026-03-31:FY:profit_before_tax:tdnet_xbrl:20260511521788",
        "disclosure_id": "20260511521788",
        "disclosure_datetime": "2026-05-11T14:30:00+09:00",
        "intended_action": "UPSERT",
    }
    manifest = {
        "scope": {"ticker": "5713", "metric": "profit_before_tax", "actual_only": True},
        "expected_upsert_count": 1,
        "rows": [row],
    }
    manifest["manifest_sha256"] = backfill._manifest_hash(manifest)

    monkeypatch.setattr(backfill, "get_supabase_write_config", lambda: {"headers": {}})
    monkeypatch.setattr(
        backfill,
        "expand_financials_rows",
        lambda **kwargs: ([{"source_row_key": row["source_row_key"]}], []),
    )

    observed = {}

    def fake_upsert(table, payload, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "count": 1}

    monkeypatch.setattr(backfill, "supabase_upsert", fake_upsert)
    monkeypatch.setattr(
        backfill,
        "_read_existing",
        lambda config: [{"source_row_key": row["source_row_key"], "value": row["value"]}],
    )

    result = backfill.apply_manifest(
        manifest,
        expected_count=1,
        expected_hash=manifest["manifest_sha256"],
        apply_token=backfill.APPLY_TOKEN,
    )

    assert observed["max_retries"] == 1
    assert result == {"status": "applied", "written": 1, "verified": 1}
