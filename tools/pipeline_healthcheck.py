#!/usr/bin/env python3
# ============================================================
# pipeline_healthcheck.py — パイプライン生存監視
# ============================================================
"""
30分ごとにタスクスケジューラで実行。
以下をチェックし、異常があれば Discord で通知する:
  1. 当日 ingest ログの存在と最終更新
  2. Supabase pipeline_runs の最新 ingest/process 成功時刻
  3. stale running 行の存在
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.pipeline.db import load_env, get_supabase_read_config, supabase_select

logger = logging.getLogger("healthcheck")

# ── 定数 ──
JST = None
try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone
    JST = timezone(timedelta(hours=9))

INGEST_LOG_STALE_MINUTES = 30
INGEST_SUCCESS_STALE_MINUTES = 60
PROCESS_SUCCESS_STALE_MINUTES = 30
COOLDOWN_FILE = os.path.join(_PROJECT_ROOT, "logs", ".healthcheck_cooldown.json")
COOLDOWN_MINUTES = 60  # 同一アラートの再送間隔


def _now() -> datetime:
    return datetime.now(JST)


def _load_cooldown() -> dict:
    try:
        with open(COOLDOWN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cooldown(data: dict) -> None:
    os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(data, f)


def _should_alert(alert_key: str, cooldown: dict) -> bool:
    """cooldown期間内の同一alertは抑制"""
    last = cooldown.get(alert_key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (_now() - last_dt).total_seconds() > COOLDOWN_MINUTES * 60
    except ValueError:
        return True


def check_ingest_log() -> list[str]:
    """当日ingestログの存在・更新チェック"""
    alerts = []
    now = _now()
    log_name = f"ingest_{now.strftime('%Y%m%d')}.log"
    log_path = os.path.join(_PROJECT_ROOT, "logs", log_name)

    if not os.path.exists(log_path):
        alerts.append(f"⚠️ 当日ingestログなし: {log_name}")
        return alerts

    mtime = datetime.fromtimestamp(os.path.getmtime(log_path), tz=JST)
    minutes_ago = (now - mtime).total_seconds() / 60

    if minutes_ago > INGEST_LOG_STALE_MINUTES:
        alerts.append(
            f"⚠️ Ingest ログ未更新 ({int(minutes_ago)}分, 閾値={INGEST_LOG_STALE_MINUTES}分)"
        )

    # BAT_START あるが BAT_END なし → timeout kill の可能性
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            starts = content.count("===BAT_START===")
            ends = content.count("===BAT_END===")
            if starts > ends:
                alerts.append(
                    f"⚠️ BAT_START ({starts}回) > BAT_END ({ends}回) → timeout kill の可能性"
                )
    except Exception:
        pass

    return alerts


def check_pipeline_runs() -> list[str]:
    """Supabase pipeline_runsの最新成功時刻と stale running をチェック"""
    alerts = []
    now = _now()
    cfg = get_supabase_read_config()
    if not cfg:
        alerts.append("⚠️ Supabase設定なし — pipeline_runs チェックスキップ")
        return alerts

    # 最新 ingest 成功
    ingest_rows = supabase_select(
        "pipeline_runs",
        params={
            "job_type": "eq.ingest",
            "status": "eq.done",
            "order": "finished_at.desc",
            "limit": "1",
            "select": "id,finished_at",
        },
        config=cfg,
    )
    if ingest_rows:
        finished = ingest_rows[0].get("finished_at", "")
        try:
            finished_dt = datetime.fromisoformat(finished)
            minutes_ago = (now - finished_dt).total_seconds() / 60
            if minutes_ago > INGEST_SUCCESS_STALE_MINUTES:
                alerts.append(
                    f"⚠️ 最終 ingest 成功: {finished_dt.strftime('%H:%M')} "
                    f"({int(minutes_ago)}分前)"
                )
        except ValueError:
            pass
    else:
        alerts.append("⚠️ ingest 成功記録なし")

    # 最新 process 成功
    process_rows = supabase_select(
        "pipeline_runs",
        params={
            "job_type": "eq.process",
            "status": "eq.done",
            "order": "finished_at.desc",
            "limit": "1",
            "select": "id,finished_at",
        },
        config=cfg,
    )
    if process_rows:
        finished = process_rows[0].get("finished_at", "")
        try:
            finished_dt = datetime.fromisoformat(finished)
            minutes_ago = (now - finished_dt).total_seconds() / 60
            if minutes_ago > PROCESS_SUCCESS_STALE_MINUTES:
                alerts.append(
                    f"⚠️ 最終 process 成功: {finished_dt.strftime('%H:%M')} "
                    f"({int(minutes_ago)}分前)"
                )
        except ValueError:
            pass
    else:
        alerts.append("⚠️ process 成功記録なし")

    # stale running
    stale_rows = supabase_select(
        "pipeline_runs",
        params={
            "status": "eq.running",
            "select": "id,job_type,started_at",
        },
        config=cfg,
    )
    if stale_rows:
        alerts.append(f"ℹ️ stale running: {len(stale_rows)}件")

    return alerts


def send_discord_alert(alerts: list[str], *, dry_run: bool = False) -> bool:
    """アラートをDiscordに送信"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("[healthcheck] DISCORD_WEBHOOK_URL not set")
        return False

    now = _now()
    header = "🚨 Pipeline Health Alert"
    separator = "━" * 30
    body = "\n".join(alerts)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S JST")

    message = f"**{header}**\n{separator}\n{body}\n{separator}\n`{timestamp}`"

    if dry_run:
        print(f"[DRY-RUN] Discord message:\n{message}")
        return True

    import requests
    try:
        resp = requests.post(
            webhook_url,
            json={"content": message},
            timeout=10,
        )
        return resp.status_code < 300
    except Exception as e:
        logger.error(f"[healthcheck] Discord send failed: {e}")
        return False


def run(*, dry_run: bool = False) -> dict:
    """ヘルスチェックを実行"""
    load_env(_PROJECT_ROOT)

    all_alerts = []
    all_alerts.extend(check_ingest_log())
    all_alerts.extend(check_pipeline_runs())

    result = {
        "timestamp": _now().isoformat(),
        "alerts": all_alerts,
        "alert_count": len(all_alerts),
        "sent": False,
    }

    if not all_alerts:
        logger.info("[healthcheck] all OK — no alerts")
        return result

    # cooldown チェック
    cooldown = _load_cooldown()
    filtered_alerts = []
    for alert in all_alerts:
        key = alert[:50]  # alert冒頭50文字をキーに
        if _should_alert(key, cooldown):
            filtered_alerts.append(alert)
            cooldown[key] = _now().isoformat()

    if filtered_alerts:
        ok = send_discord_alert(filtered_alerts, dry_run=dry_run)
        result["sent"] = ok
        if not dry_run:
            _save_cooldown(cooldown)
    else:
        logger.info("[healthcheck] alerts suppressed by cooldown")

    result["filtered_count"] = len(filtered_alerts)

    for alert in all_alerts:
        logger.info(f"[healthcheck] {alert}")

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline Healthcheck")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
