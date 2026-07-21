"""Fail-closed repair for missing segment_financials TDNET provenance.

This tool never inserts, deletes, or performs a generic upsert.  Apply mode is
restricted to non-production database copies and can update only tdnet_doc_id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "row_id", "company_code", "fiscal_year_end", "quarter", "segment_name",
    "segment_sales", "segment_profit", "expected_tdnet_doc_id",
)


class RepairContractError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _load_manifest(path: Path, expected_count: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RepairContractError(f"expected_row_count_mismatch:{len(rows) if isinstance(rows, list) else 'invalid'}")
    ids: set[int] = set()
    for row in rows:
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            raise RepairContractError(f"manifest_fields_missing:{','.join(missing)}")
        row_id = int(row["row_id"])
        if row_id in ids:
            raise RepairContractError(f"duplicate_row_id:{row_id}")
        ids.add(row_id)
        doc_id = str(row["expected_tdnet_doc_id"] or "").strip()
        if not doc_id:
            raise RepairContractError(f"expected_document_id_missing:{row_id}")
    return rows


def _production_db_path(path: Path) -> bool:
    resolved = path.resolve()
    repo = Path(__file__).resolve().parents[1]
    return resolved == (repo / "decision_db.db").resolve() or resolved == (repo / "data" / "decision_db.db").resolve()


def _columns(con: sqlite3.Connection) -> list[str]:
    return [row[1] for row in con.execute("PRAGMA table_info(segment_financials)")]


def _fetch_row(con: sqlite3.Connection, columns: list[str], row_id: int) -> dict[str, Any] | None:
    row = con.execute(
        f"SELECT {','.join(chr(34) + c + chr(34) for c in columns)} FROM segment_financials WHERE id=?",
        (row_id,),
    ).fetchone()
    return dict(zip(columns, row)) if row else None


def _same(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return left == right


def inspect_repairs(con: sqlite3.Connection, plans: list[dict[str, Any]]) -> dict[str, Any]:
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RepairContractError("integrity_check_failed")
    if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RepairContractError("foreign_key_violation")
    columns = _columns(con)
    required_db = {"id", "company_code", "fiscal_year_end", "quarter", "segment_name", "segment_sales", "segment_profit", "tdnet_doc_id"}
    if not required_db.issubset(columns):
        raise RepairContractError("schema_mismatch")
    pending: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for plan in plans:
        row_id = int(plan["row_id"])
        current = _fetch_row(con, columns, row_id)
        if current is None:
            raise RepairContractError(f"target_row_missing:{row_id}")
        checks = {
            "company_code": plan["company_code"],
            "fiscal_year_end": plan["fiscal_year_end"],
            "quarter": plan["quarter"],
            "segment_name": plan["segment_name"],
            "segment_sales": plan["segment_sales"],
            "segment_profit": plan["segment_profit"],
        }
        for field, expected in checks.items():
            if not _same(current[field], expected):
                raise RepairContractError(f"row_contract_mismatch:{row_id}:{field}")
        duplicate_count = con.execute(
            "SELECT COUNT(*) FROM segment_financials WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND segment_name=?",
            (plan["company_code"], plan["fiscal_year_end"], plan["quarter"], plan["segment_name"]),
        ).fetchone()[0]
        if duplicate_count != 1:
            raise RepairContractError(f"natural_key_duplicate:{row_id}:{duplicate_count}")
        expected_doc = str(plan["expected_tdnet_doc_id"])
        if current["tdnet_doc_id"] is None:
            pending.append({**plan, "before": current})
        elif current["tdnet_doc_id"] == expected_doc:
            completed.append({"row_id": row_id, "tdnet_doc_id": expected_doc})
        else:
            raise RepairContractError(f"non_null_document_id_conflict:{row_id}")
    return {"pending": pending, "completed": completed, "conflicting": []}


def run_repair(
    db_path: Path, manifest_path: Path, expected_count: int, *, apply: bool,
    confirm_count: int | None = None, expected_db_sha256: str | None = None,
) -> dict[str, Any]:
    if apply and _production_db_path(db_path):
        raise RepairContractError("production_path_apply_forbidden")
    if apply and confirm_count != expected_count:
        raise RepairContractError("apply_confirmation_mismatch")
    before_sha = file_sha256(db_path)
    if expected_db_sha256 and before_sha.lower() != expected_db_sha256.lower():
        raise RepairContractError("database_sha256_mismatch")
    plans = _load_manifest(manifest_path, expected_count)
    con = sqlite3.connect(db_path)
    con.row_factory = None
    result: dict[str, Any] | None = None
    try:
        report = inspect_repairs(con, plans)
        before_count = con.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0]
        updated = 0
        if apply:
            con.execute("BEGIN IMMEDIATE")
            try:
                report = inspect_repairs(con, plans)
                if len(report["pending"]) != expected_count:
                    raise RepairContractError(f"pending_row_count_mismatch:{len(report['pending'])}")
                for item in report["pending"]:
                    plan = item
                    cur = con.execute(
                        "UPDATE segment_financials SET tdnet_doc_id=? WHERE id=? AND tdnet_doc_id IS NULL "
                        "AND company_code=? AND fiscal_year_end=? AND quarter=? AND segment_name=? "
                        "AND segment_sales IS ? AND segment_profit IS ?",
                        (plan["expected_tdnet_doc_id"], int(plan["row_id"]), plan["company_code"],
                         plan["fiscal_year_end"], plan["quarter"], plan["segment_name"],
                         plan["segment_sales"], plan["segment_profit"]),
                    )
                    if cur.rowcount != 1:
                        raise RepairContractError(f"update_predicate_mismatch:{plan['row_id']}:{cur.rowcount}")
                    updated += 1
                after_count = con.execute("SELECT COUNT(*) FROM segment_financials").fetchone()[0]
                if before_count != after_count:
                    raise RepairContractError("row_count_changed")
                con.commit()
            except Exception:
                con.rollback()
                raise
        final = inspect_repairs(con, plans)
        result = {
            "mode": "apply" if apply else "plan", "expected_rows": expected_count,
            "pending_updates": len(report["pending"]), "already_completed": len(report["completed"]),
            "updated_rows": updated, "inserted_rows": 0, "deleted_rows": 0,
            "final_pending": len(final["pending"]), "final_completed": len(final["completed"]),
            "database_sha256_before": before_sha,
        }
    finally:
        con.close()
    assert result is not None
    result["database_sha256_after"] = file_sha256(db_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repair-manifest", type=Path, required=True)
    parser.add_argument("--expected-row-count", type=int, required=True)
    parser.add_argument("--expected-db-sha256")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-null-doc-id-repair", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_repair(
        args.db, args.repair_manifest, args.expected_row_count, apply=args.apply,
        confirm_count=args.confirm_null_doc_id_repair,
        expected_db_sha256=args.expected_db_sha256,
    )
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(args.output)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
