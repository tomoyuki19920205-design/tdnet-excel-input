#!/usr/bin/env python3
"""Read-only post-migration gate for the NY morning automation."""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

EXPECTED_CWD = Path(__file__).resolve().parents[1]
EXPECTED_PROJECT = "02d23200-0fd1-4569-a639-b312e9591722"
EXPECTED_RRULE = "RRULE:FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0"


def audit(config_path: Path, *, expected_cwd: Path, expected_project: str) -> dict[str, object]:
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    checks = {
        "id": config.get("id") == "ny",
        "status": config.get("status") == "ACTIVE",
        "schedule": config.get("rrule") == EXPECTED_RRULE,
        "execution_environment": config.get("execution_environment") == "local",
        "project": config.get("target", {}).get("project_id") == expected_project,
        "cwd": config.get("cwds") == [str(expected_cwd.resolve())],
    }
    return {
        "automation": "ny",
        "config_path": str(config_path),
        "status": config.get("status"),
        "rrule": config.get("rrule"),
        "checks": checks,
        "ok": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path.home() / ".codex" / "automations" / "ny" / "automation.toml",
    )
    parser.add_argument("--expected-cwd", type=Path, default=EXPECTED_CWD)
    parser.add_argument("--expected-project", default=EXPECTED_PROJECT)
    args = parser.parse_args()
    result = audit(args.config, expected_cwd=args.expected_cwd, expected_project=args.expected_project)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
