from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.backfill_campaign_manifest import run_dry_run


def _rows() -> list[dict]:
    base = {"ticker": "143A", "doc_url": "https://www.release.tdnet.info/inbs/140120260101000001.pdf", "xbrl_url": "https://www.release.tdnet.info/inbs/081220260101000001.zip", "disclosure_date": "2026-01-01", "doc_type": "financial_statement", "current_fiscal_year_end_date": "2026-12-31"}
    return [dict(base, ticker="143A"), dict(base, ticker="2000"), dict(base, ticker="3000", requested_disclosure_no="20260101000001", doc_url=None, xbrl_url=None), dict(base, ticker=None), dict(base, doc_url=None, xbrl_url="https://example.invalid/081220260101000001.zip")]


def _manifest(tmp_path: Path, rows: list[dict] | None = None) -> tuple[Path, str]:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(rows or _rows(), ensure_ascii=False), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_normal_manifest_and_row_ids(tmp_path):
    path, digest = _manifest(tmp_path)
    result = run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")
    rows = [json.loads(line) for line in (tmp_path / "out" / "registration-candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["manifest_row_id"] for r in rows] == ["0000000001", "0000000002", "0000000003", "0000000004", "0000000005"]
    assert result["summary"]["input_count"] == result["summary"]["output_count"] == 5


def test_requested_duplicate_rows_are_retained(tmp_path):
    rows = _rows()[:2]
    path, digest = _manifest(tmp_path, rows)
    result = run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")
    assert result["summary"]["duplicate_group_count"] == 1
    assert result["summary"]["duplicate_row_count"] == 2


def test_classification_missing_fields_and_invalid_url(tmp_path):
    path, digest = _manifest(tmp_path)
    run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")
    classes = [json.loads(line)["classification"] for line in (tmp_path / "out" / "registration-classification.jsonl").read_text(encoding="utf-8").splitlines()]
    assert classes == ["REQUESTED_ID_DUPLICATE", "REQUESTED_ID_DUPLICATE", "MISSING_URL", "MISSING_COMPANY_CODE", "INVALID_OFFICIAL_URL"]


def test_semantic_digest_is_deterministic(tmp_path):
    path, digest = _manifest(tmp_path, _rows()[:2])
    a = run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "a")
    b = run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "b")
    assert a["digests"]["semantic_digest"] == b["digests"]["semantic_digest"]


def test_missing_quarter_is_metadata_incomplete(tmp_path):
    rows = [_rows()[0]]
    path, digest = _manifest(tmp_path, rows)
    result = run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")
    assert result["summary"]["classification_counts"] == {"METADATA_INCOMPLETE": 1}


def test_manifest_sha_mismatch(tmp_path):
    path, _ = _manifest(tmp_path)
    with pytest.raises(ValueError, match="SHA"):
        run_dry_run(manifest=path, manifest_sha256="0" * 64, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")


def test_output_dir_must_be_absolute_and_outside_repo(tmp_path):
    path, digest = _manifest(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=Path("relative"))


def test_no_apply_or_database_side_effect(tmp_path):
    path, digest = _manifest(tmp_path, _rows()[:1])
    before = set(tmp_path.iterdir())
    run_dry_run(manifest=path, manifest_sha256=digest, campaign_id="c", campaign_name="C", code_sha="a" * 40, worker_version="v4", output_dir=tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.db"))
    assert set(tmp_path.iterdir()) == before | {tmp_path / "out"}
