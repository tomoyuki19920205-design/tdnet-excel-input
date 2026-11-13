#!/usr/bin/env python3
"""Zero-generation-cost NY delivery watchdog, independent of the Codex app."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_ny_automation_state import audit
from tools.preflight_ny_market_runtime import preflight

JST = timezone(timedelta(hours=9))


def assess(snapshot: dict, automation: dict, *, now: datetime, deadline: time) -> dict:
    today = now.astimezone(JST).date().isoformat()
    overdue = now.astimezone(JST).time().replace(tzinfo=None) >= deadline
    reasons = []
    if not automation.get("ok"):
        reasons.append("automation_configuration_drift")
    if snapshot.get("status") != "ready":
        reasons.append("production_readback_unavailable")
    elif not snapshot.get("complete_readback_verified"):
        reasons.append("complete_report_missing" if overdue else "awaiting_complete_report")
    status = "healthy" if not reasons else ("pending" if reasons == ["awaiting_complete_report"] else "failed")
    return {
        "checked_at_jst": now.astimezone(JST).isoformat(),
        "report_date_jst": today,
        "stable_key": f"ny_market_daily:{today}",
        "deadline_jst": deadline.isoformat(timespec="minutes"),
        "status": status, "reasons": reasons,
        "automation": automation,
        "delivery": snapshot,
        "limits": {"paid_generation_calls": 0, "generation_cost_usd": 0,
                   "max_http_read_attempts_per_endpoint": 4, "max_http_read_attempts_per_check": 12},
    }


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def render_html(record: dict) -> str:
    safe = html.escape
    remote = record["delivery"].get("supabase", {})
    labels = {"healthy": "完全版・保存一致", "pending": "完全版生成待ち", "failed": "異常を検出"}
    reasons = {"automation_configuration_drift": "定期タスクが無効、または登録設定に変更があります。",
               "production_readback_unavailable": "本番APIまたはDBを読み戻せませんでした。",
               "complete_report_missing": "所定時刻を過ぎても完全版と保存先の一致を確認できません。",
               "awaiting_complete_report": "判定時刻まで完全版を待っています。"}
    body = "".join(f"<li>{safe(reasons.get(r, r))}</li>" for r in record["reasons"])
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>NYレポート配信監視</title><style>body{{font:17px system-ui;margin:3rem auto;padding:1rem;max-width:850px;background:#f6f8fc;color:#182238}}main{{background:white;border-radius:14px;padding:2rem}}h1{{font-size:1.5rem}}strong{{color:{'#18703b' if record['status']=='healthy' else '#a53b20'}}}dt{{margin-top:1rem;font-weight:600}}dd{{margin-left:0}}code{{overflow-wrap:anywhere}}</style><main>
<h1>NYモーニングレポート 配信監視</h1><p><strong>{labels[record['status']]}</strong></p><ul>{body}</ul>
<dl><dt>レポート日</dt><dd>{safe(record['report_date_jst'])}</dd><dt>最終確認</dt><dd>{safe(record['checked_at_jst'])}</dd>
<dt>判定時刻</dt><dd>{safe(record['deadline_jst'])} JST</dd><dt>本番レポートの状態</dt><dd>{safe(str(remote.get('quality_status','読取不能')))}</dd>
<dt>定期タスク</dt><dd>{safe(str(record['automation'].get('status','読取不能')))}</dd></dl>
<p>この監視はレポートを生成しません。追加の生成API料金は0です。本文・表示仕様・他のジョブを変更しません。</p>
<p><a href="https://company-memo-app.vercel.app/news">Company Viewer NEWSを開く</a></p></main></html>'''


def run(args, *, now=None, fetch=preflight) -> dict:
    now = now or datetime.now(JST)
    report_date = now.astimezone(JST).date().isoformat()
    try:
        automation = audit(args.automation_config, expected_cwd=args.runtime,
                           expected_project="acce2aa5-a39b-48e1-8487-ac0e68e8f29c")
        config = tomllib.loads(args.automation_config.read_text(encoding="utf-8"))
        expected_prompt = (args.runtime / "config/ny_market_daily_scheduled_prompt.txt").read_text(encoding="utf-8").strip()
        automation["checks"]["prompt_matches_deployed_source"] = config.get("prompt", "").strip() == expected_prompt
        automation["ok"] = all(automation["checks"].values())
    except Exception as exc:
        automation = {"ok": False, "error_type": type(exc).__name__}
    try:
        snapshot = fetch(args.production_root, args.db, f"ny_market_daily:{report_date}")
    except Exception as exc:
        # Exceptions may contain credential-bearing URLs/headers. Never persist them.
        snapshot = {"status": "blocked", "error_type": type(exc).__name__}
        response = getattr(exc, "response", None)
        if response is not None:
            snapshot["http_status"] = response.status_code
    record = assess(snapshot, automation, now=now, deadline=time.fromisoformat(args.deadline))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Task Scheduler IgnoreNew prevents overlap; unique audit names retain every invocation.
    import uuid
    name = now.astimezone(JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    raw = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    atomic_text(args.output_dir / "history" / f"{name}.json", raw)
    atomic_text(args.output_dir / "latest.json", raw)
    atomic_text(args.output_dir / "index.html", render_html(record))
    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime", type=Path, default=ROOT)
    p.add_argument("--production-root", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--automation-config", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--deadline", default="07:45")
    a = p.parse_args()
    r = run(a)
    if sys.stdout is not None:
        print(json.dumps({k:r[k] for k in ("status", "report_date_jst", "reasons")}, ensure_ascii=True))
    return 2 if r["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
