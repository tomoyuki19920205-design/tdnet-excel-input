"""Build worker_v4-compatible manifests from verified Fresh campaign rows.

``expected_period`` is the fiscal year end, not the current cumulative-period
end date.  Fresh artifacts preserve the same fiscal-year-end value in the
completed-artifact record and provenance.  The campaign-filings ``zip_internal``
columns are historical identity-plan fields and may be unset; they are not an
artifact validation source.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from lib.backfill.listing_sources.base import make_filing_id

_ACCEPTED_VERDICTS = {
    "exact_document_id_match",
    "official_linked_xbrl_match",
    "official_linked_xbrl_match_without_internal_id",
}


class AdapterValidationError(ValueError):
    """Fail closed when a Fresh artifact does not prove its manifest identity."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _in_cache(path: Path, cache_root: Path) -> bool:
    try:
        path.resolve().relative_to(cache_root.resolve())
    except ValueError:
        return False
    return True


def _source_rows(source_manifest: Path) -> dict[str, dict]:
    payload = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_rows = payload.get("filings") if isinstance(payload, dict) else payload
    if not isinstance(source_rows, list):
        raise AdapterValidationError("source manifest structure invalid")
    mapping: dict[str, dict] = {}
    for row in source_rows:
        if not isinstance(row, dict):
            raise AdapterValidationError("source manifest row invalid")
        requested_id = str(row.get("requested_disclosure_no") or "")
        if not requested_id:
            filing_id = str(row.get("filing_id") or "")
            requested_id = filing_id.removeprefix("jquants_")
        if not requested_id or requested_id in mapping:
            raise AdapterValidationError("source manifest requested id invalid")
        mapping[requested_id] = row
    return mapping


def _state_filing_ids(state_db: Path) -> set[str]:
    conn = sqlite3.connect("file:" + state_db.as_posix() + "?mode=ro&immutable=1", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT filing_id FROM filing_state")}
    finally:
        conn.close()


def build(
    db: Path, campaign_id: str, cache_root: Path, *, source_manifest: Path,
    state_db: Path, expected_count: int = 5210,
) -> list[dict]:
    source_by_requested = _source_rows(source_manifest)
    existing_state_ids = _state_filing_ids(state_db)
    conn = sqlite3.connect("file:" + db.as_posix() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    query = """SELECT c.*, f.target_zip_path, f.target_provenance_path,
                      f.artifact_zip_sha256, f.artifact_internal_document_id,
                      f.artifact_ticker, f.artifact_period, f.artifact_quarter,
                      f.identity_verdict, f.source_route
               FROM campaign_filings c
               JOIN campaign_fresh_downloads f USING (campaign_id, manifest_row_id)
               WHERE c.campaign_id=? AND f.fresh_status='COMPLETE'
               ORDER BY c.manifest_row_id"""
    rows: list[dict] = []
    try:
        for row in conn.execute(query, (campaign_id,)):
            item = dict(row)
            zip_path = Path(item["target_zip_path"])
            provenance_path = Path(item["target_provenance_path"])
            row_id = item["manifest_row_id"]
            if not zip_path.is_file() or not provenance_path.is_file() or not _in_cache(zip_path, cache_root) or not _in_cache(provenance_path, cache_root):
                raise AdapterValidationError(f"{row_id}: artifact path invalid")
            if _sha256(zip_path) != item["artifact_zip_sha256"]:
                raise AdapterValidationError(f"{row_id}: zip sha mismatch")
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if str(provenance.get("zip_sha256") or "") != str(item["artifact_zip_sha256"]):
                raise AdapterValidationError(f"{row_id}: provenance sha mismatch")
            if item["identity_verdict"] not in _ACCEPTED_VERDICTS:
                raise AdapterValidationError(f"{row_id}: identity verdict rejected")
            fiscal_year_end = str(item["expected_period"] or "")
            canonical_quarter = str(item["expected_quarter"] or "")
            artifact_period = str(item["artifact_period"] or "")
            artifact_quarter = str(item["artifact_quarter"] or "")
            provenance_period = str(provenance.get("zip_internal_period") or "")
            provenance_quarter = str(provenance.get("zip_internal_quarter") or "")
            if not fiscal_year_end or fiscal_year_end != artifact_period or fiscal_year_end != provenance_period:
                raise AdapterValidationError(f"{row_id}: fiscal year end conflict")
            if canonical_quarter not in {"1Q", "2Q", "3Q", "FY"} or canonical_quarter != artifact_quarter or canonical_quarter != provenance_quarter:
                raise AdapterValidationError(f"{row_id}: canonical quarter conflict")
            if str(provenance.get("requested_disclosure_no") or "") != str(item["requested_disclosure_no"]):
                raise AdapterValidationError(f"{row_id}: requested disclosure mismatch")
            if str(provenance.get("zip_internal_ticker") or "") != str(item["company_code"]):
                raise AdapterValidationError(f"{row_id}: ticker mismatch")
            requested_id = str(item["requested_disclosure_no"] or "")
            source = source_by_requested.get(requested_id)
            if source is None:
                raise AdapterValidationError(f"{row_id}: source manifest row missing")
            if (
                str(source.get("ticker") or "") != str(item["company_code"])
                or str(source.get("disclosure_date") or "") != str(item["disclosure_date"])
                or str(source.get("doc_url") or "") != str(item["source_url"])
            ):
                raise AdapterValidationError(f"{row_id}: source manifest identity mismatch")
            title = str(source.get("title") or "").strip()
            if not title:
                raise AdapterValidationError(f"{row_id}: source title missing")
            state_filing_id = make_filing_id(
                str(source["disclosure_date"]), str(source["ticker"]), title,
                str(source["doc_url"]),
            )
            if state_filing_id in existing_state_ids:
                raise AdapterValidationError(f"{row_id}: existing state filing id collision")
            rows.append({
                "filing_id": state_filing_id,
                "requested_disclosure_no": item["requested_disclosure_no"],
                "expected_period": fiscal_year_end,
                "expected_quarter": canonical_quarter,
                "ticker": item["company_code"],
                "title": title,
                "disclosure_date": item["disclosure_date"],
                "doc_url": item["source_url"],
                "xbrl_url": item["normalized_xbrl_url"],
                "doc_type": item["document_type"],
                "company_name": "",
                "published_at": item["disclosure_date"],
                "listing_source": "fresh_campaign",
                "has_xbrl": True,
                "zip_path": str(zip_path),
                "internal_document_id": item["artifact_internal_document_id"],
            })
    finally:
        conn.close()
    if len(rows) != expected_count:
        raise AdapterValidationError(f"population mismatch: {len(rows)}")
    if len({row["requested_disclosure_no"] for row in rows}) != len(rows):
        raise AdapterValidationError("requested disclosure duplicate")
    if len({row["filing_id"] for row in rows}) != len(rows):
        raise AdapterValidationError("filing id duplicate")
    return rows

def write(rows: list[dict], path: Path) -> str:
    raw = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return hashlib.sha256(raw).hexdigest()
