import hashlib
import json
from pathlib import Path

import pytest

from lib.backfill.fresh_seed_cache_builder import (
    SIDECAR_FIELDS, build_record, canonical_records_bytes, canonical_sidecar_bytes,
    semantic_sha256, sha256,
)
from tools.build_v4_fresh_seed_cache import _parser, _safe_output, _validate_selection_hashes


def test_sha256_is_stable(tmp_path):
    path = tmp_path / "x"
    path.write_bytes(b"abc")
    assert sha256(path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_semantic_digest_ignores_json_formatting():
    rows = [{"b": 2, "a": "日本"}]
    assert semantic_sha256(rows) == hashlib.sha256(canonical_records_bytes(rows)).hexdigest()
    assert json.loads(canonical_records_bytes(rows)) == rows[0]


def test_row_order_changes_semantic_digest():
    assert semantic_sha256([{"a": 1}, {"a": 2}]) != semantic_sha256([{"a": 2}, {"a": 1}])


def test_canonical_sidecar_is_utf8_lf_and_stable():
    value = {"source": "jquants", "ticker": "7203"}
    assert canonical_sidecar_bytes(value) == b'{"source":"jquants","ticker":"7203"}\n'


def test_output_guard_accepts_tmp_and_rejects_repository():
    assert _safe_output(Path("C:/tmp/seed-test"))
    assert _safe_output(Path("C:/tmp/seed-test/summary.json"))
    assert not _safe_output(Path.cwd() / "data" / "seed-test")
    assert not _safe_output(Path.cwd() / "summary.json")


def test_cli_help_defines_both_selection_hashes():
    help_text = _parser().format_help()
    assert "Raw selection manifest file SHA-256" in help_text
    assert "Canonical semantic digest" in help_text


def test_selection_alias_is_explicitly_deprecated():
    assert "Deprecated alias" in _parser().format_help()


def _selection_fixture():
    rows = [{"filing_id": "f1", "requested_disclosure_no": "r1"}]
    raw = canonical_records_bytes(rows)
    return rows, raw, hashlib.sha256(raw).hexdigest(), semantic_sha256(rows)


def test_selection_byte_and_semantic_hashes_are_independently_accepted():
    rows, raw, byte_digest, semantic_digest = _selection_fixture()
    _validate_selection_hashes(
        raw, rows, alias_sha256=None, byte_sha256=byte_digest,
        semantic_digest=semantic_digest,
    )


def test_selection_byte_hash_mismatch_stops_before_output(tmp_path):
    rows, raw, _, semantic_digest = _selection_fixture()
    with pytest.raises(SystemExit, match="selection byte sha mismatch"):
        _validate_selection_hashes(
            raw, rows, alias_sha256=None, byte_sha256="0" * 64,
            semantic_digest=semantic_digest,
        )
    assert list(tmp_path.iterdir()) == []


def test_selection_semantic_hash_mismatch_stops_before_output(tmp_path):
    rows, raw, byte_digest, _ = _selection_fixture()
    with pytest.raises(SystemExit, match="selection semantic sha mismatch"):
        _validate_selection_hashes(
            raw, rows, alias_sha256=None, byte_sha256=byte_digest,
            semantic_digest="0" * 64,
        )
    assert list(tmp_path.iterdir()) == []


def test_selection_alias_and_explicit_byte_hash_conflict_stops_before_output(tmp_path):
    rows, raw, byte_digest, semantic_digest = _selection_fixture()
    with pytest.raises(SystemExit, match="selection byte hash arguments conflict"):
        _validate_selection_hashes(
            raw, rows, alias_sha256="0" * 64, byte_sha256=byte_digest,
            semantic_digest=semantic_digest,
        )
    assert list(tmp_path.iterdir()) == []


def _fresh_source(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_dir = source_root / "0000000001"
    source_dir.mkdir(parents=True)
    source_zip = source_dir / "xbrl.zip"
    source_zip.write_bytes(b"trusted-xbrl")
    source_dir.joinpath("provenance.json").write_text(json.dumps({
        "source_route": "JQUANTS_TD_FILES",
        "requested_disclosure_no": "20260101000001",
        "zip_sha256": sha256(source_zip),
        "downloaded_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    monkeypatch.setattr(
        "lib.backfill.fresh_seed_cache_builder.extract_actual_metadata_from_zip",
        lambda *args, **kwargs: {
            "ticker": "7203", "period": "2026-03-31", "quarter": "FY",
            "internal_document_id": "20260101372030",
            "document_type": "financial_statement",
        },
    )
    row = {
        "filing_id": "20260101000001", "requested_disclosure_no": "20260101000001",
        "ticker": "7203", "expected_period": "2026-03-31", "expected_quarter": "FY",
        "zip_path": str(source_zip),
    }
    return source_root, row


def test_builder_emits_formal_sidecar_schema_and_requested_file_type(tmp_path, monkeypatch):
    source_root, row = _fresh_source(tmp_path, monkeypatch)
    record = build_record(row, source_root, tmp_path / "output", materialize=True)
    sidecar = record["sidecar"]
    assert tuple(sidecar) == SIDECAR_FIELDS
    assert sidecar["requested_file_type"] == "x"
    assert sidecar["internal_document_id"] == "20260101372030"
    assert record["output_relative_path"] == "20260101000001/xbrl.zip"


def test_builder_reuses_exact_output_and_rejects_conflict_without_overwrite(tmp_path, monkeypatch):
    source_root, row = _fresh_source(tmp_path, monkeypatch)
    output_root = tmp_path / "output"
    assert build_record(row, source_root, output_root, materialize=True)["materialization"] == "created"
    assert build_record(row, source_root, output_root, materialize=True)["materialization"] == "reused"
    sidecar_path = output_root / "20260101000001" / "xbrl.zip.provenance.json"
    sidecar_path.write_bytes(b"conflict\n")
    before = sidecar_path.read_bytes()
    with pytest.raises(FileExistsError, match="conflicts"):
        build_record(row, source_root, output_root, materialize=True)
    assert sidecar_path.read_bytes() == before


def test_builder_rejects_path_traversal_before_reading_source(tmp_path):
    row = {"requested_disclosure_no": "../escape"}
    with pytest.raises(ValueError, match="unsafe requested disclosure ID"):
        build_record(row, tmp_path / "source", tmp_path / "output")
    assert not (tmp_path / "escape").exists()
