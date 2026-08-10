#!/usr/bin/env python3
"""Exact-manifest repair for non-consolidated OP canonical contamination."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.pipeline.db import get_supabase_write_config, load_env


APPLY_TOKEN = "I_UNDERSTAND_NONCONSOLIDATED_OP_DELETE"
ALLOWED_METRIC = "operating_profit"
ALLOWED_SOURCE = "summary_xbrl"
LOCKED_FIELDS = (
    "id",
    "ticker",
    "period",
    "quarter",
    "metric",
    "source",
    "value",
    "source_row_key",
)


def manifest_sha256(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _headers(config: dict[str, str], *, return_rows: bool = False) -> dict[str, str]:
    headers = {
        "apikey": config["key"],
        "Authorization": f"Bearer {config['key']}",
    }
    if return_rows:
        headers["Prefer"] = "return=representation"
    return headers


def _params(row: dict[str, Any], *, select: bool = False) -> dict[str, str]:
    params = {field: f"eq.{row[field]}" for field in LOCKED_FIELDS}
    if select:
        params["select"] = ",".join(LOCKED_FIELDS)
        params["limit"] = "2"
    return params


def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = manifest.get("rows")
    expected = manifest.get("expected_delete_count")
    if not isinstance(rows, list) or expected != len(rows) or expected != 3:
        raise RuntimeError("manifest must lock exactly three approved deletes")
    ids: set[int] = set()
    for row in rows:
        if any(field not in row for field in LOCKED_FIELDS):
            raise RuntimeError(f"manifest row lacks a locked field: {row}")
        if row["metric"] != ALLOWED_METRIC or row["source"] != ALLOWED_SOURCE:
            raise RuntimeError(f"out-of-scope metric/source: {row}")
        if "NonConsolidatedMember" not in str(row.get("official_context") or ""):
            raise RuntimeError(f"missing official non-consolidated evidence: {row}")
        row_id = int(row["id"])
        if row_id in ids:
            raise RuntimeError(f"duplicate canonical id: {row_id}")
        ids.add(row_id)
    return rows


def _preflight(
    session: requests.Session,
    config: dict[str, str],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    endpoint = f"{config['rest_url']}/canonical_financials"
    for row in rows:
        response = session.get(
            endpoint,
            headers=_headers(config),
            params=_params(row, select=True),
            timeout=5,
        )
        response.raise_for_status()
        found = response.json()
        if len(found) != 1:
            raise RuntimeError(
                f"exact precondition mismatch for id={row['id']}: count={len(found)}"
            )
        matched.append(found[0])
    return matched


def repair(
    manifest: dict[str, Any],
    *,
    expected_hash: str,
    apply: bool,
    apply_token: str,
) -> dict[str, Any]:
    actual_hash = manifest_sha256(manifest)
    if manifest.get("manifest_sha256") != actual_hash or expected_hash != actual_hash:
        raise RuntimeError(
            f"manifest hash mismatch: expected={expected_hash} actual={actual_hash}"
        )
    rows = _validate_manifest(manifest)
    config = get_supabase_write_config()
    if not config.get("rest_url") or not config.get("key"):
        raise RuntimeError("Supabase write credentials unavailable")

    with requests.Session() as session:
        matched = _preflight(session, config, rows)
        if not apply:
            return {
                "status": "dry_run",
                "matched": len(matched),
                "would_delete": len(rows),
                "canonical_ids": [row["id"] for row in rows],
            }
        if apply_token != APPLY_TOKEN:
            raise RuntimeError("invalid apply token")

        endpoint = f"{config['rest_url']}/canonical_financials"
        deleted: list[dict[str, Any]] = []
        for row in rows:
            response = session.delete(
                endpoint,
                headers=_headers(config, return_rows=True),
                params=_params(row),
                timeout=5,
            )
            response.raise_for_status()
            returned = response.json()
            if len(returned) != 1 or int(returned[0]["id"]) != int(row["id"]):
                raise RuntimeError(
                    f"delete count/identity mismatch for id={row['id']}: {returned}"
                )
            deleted.extend(returned)

        remaining = _preflight_remaining_ids(session, config, rows)
        if remaining:
            raise RuntimeError(f"post-delete verification failed: {remaining}")
        return {
            "status": "applied",
            "deleted": len(deleted),
            "verified_absent": len(rows),
            "canonical_ids": [row["id"] for row in deleted],
        }


def _preflight_remaining_ids(
    session: requests.Session,
    config: dict[str, str],
    rows: list[dict[str, Any]],
) -> list[int]:
    endpoint = f"{config['rest_url']}/canonical_financials"
    remaining: list[int] = []
    for row in rows:
        response = session.get(
            endpoint,
            headers=_headers(config),
            params={"id": f"eq.{row['id']}", "select": "id", "limit": "1"},
            timeout=5,
        )
        response.raise_for_status()
        if response.json():
            remaining.append(int(row["id"]))
    return remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-token", default="")
    parser.add_argument("--env-root", default=str(PROJECT_ROOT))
    args = parser.parse_args()

    load_env(args.env_root)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    result = repair(
        manifest,
        expected_hash=args.manifest_sha256,
        apply=args.apply,
        apply_token=args.apply_token,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
