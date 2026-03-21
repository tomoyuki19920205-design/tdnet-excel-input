#!/usr/bin/env python3
"""run_earnings_sample_test.py — 決算短信 サンプル通知テスト CLI

Usage:
    python -m src.events.run_earnings_sample_test --sample 20 --send-discord
    python -m src.events.run_earnings_sample_test --sample 20 --sample-seed 42 --date 2026-03-13
    python -m src.events.run_earnings_sample_test --sample 5 --date 2026-03-13

Options:
    --sample N        : ランダムに N 社を抽出 (デフォルト: 20)
    --sample-seed N   : ランダムシード (同じseed+日付で同じ銘柄集合、未指定=ランダム)
    --date YYYY-MM-DD : 対象日付 (未指定=当日)
    --send-discord    : Discord へ実送信 (未指定=コンソール出力のみ)
    --model MODEL     : AI モデル名 (デフォルト: gpt-5.4-mini)

注意:
    - 本番DB (summary_jobs / ai_summaries) には一切書き込みません
    - fingerprint重複抑止は使用しません（毎回単発処理）
    - --send-discord なしではコンソール出力のみです
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.env_loader import load_project_env, get_project_root
from src.events.summary_earnings_pipeline import run_earnings_sample_test

logger = logging.getLogger("earnings_sample")


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    load_project_env()

    parser = argparse.ArgumentParser(
        description="決算短信 サンプル通知テスト CLI",
    )
    parser.add_argument("--sample", type=int, default=20,
                        help="ランダム抽出する社数 (デフォルト: 20)")
    parser.add_argument("--sample-seed", type=int, default=None,
                        help="ランダムシード (再現性用、未指定=ランダム)")
    parser.add_argument("--date", type=str, default=None,
                        help="対象日付 (YYYY-MM-DD)")
    parser.add_argument("--send-discord", action="store_true",
                        help="Discord へ実送信 (未指定=コンソール出力のみ)")
    parser.add_argument("--model", type=str, default="",
                        help="使用AIモデル (デフォルト: gpt-5.4-mini)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Webhook URL
    webhook_url = ""
    if args.send_discord:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not webhook_url:
            print("[ERROR] --send-discord 指定時は DISCORD_WEBHOOK_URL 環境変数が必要です")
            sys.exit(1)

    # OPENAI_API_KEY チェック（AI整形に必要）
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("[WARNING] OPENAI_API_KEY 未設定: AI整形はスキップされます")

    print()
    print("=" * 55)
    print("  決算短信 サンプル通知テスト")
    print("=" * 55)
    print(f"  sample     : {args.sample}")
    print(f"  seed       : {args.sample_seed or '(random)'}")
    print(f"  date       : {args.date or '(today)'}")
    print(f"  send       : {'Discord' if args.send_discord else 'console only'}")
    print(f"  model      : {args.model or '(default)'}")
    print("=" * 55)
    print()

    # 実行
    result = run_earnings_sample_test(
        target_date=args.date,
        sample_size=args.sample,
        sample_seed=args.sample_seed,
        send_discord=args.send_discord,
        webhook_url=webhook_url,
        model=args.model,
    )

    # 結果レポート
    print()
    print("=" * 55)
    print("  SAMPLE TEST RESULT")
    print("=" * 55)
    print(f"  total_disclosures    : {result.total_disclosures}")
    print(f"  validated_candidates : {result.validated_candidates}")
    print(f"  sampled              : {result.sampled}")
    print(f"  seed_used            : {result.seed_used}")
    print(f"  succeeded            : {result.succeeded}")
    print(f"  failed               : {result.failed}")
    print(f"  notifications_sent   : {result.notifications_sent}")
    if result.skip_reasons:
        print(f"  --- skip reasons ---")
        for reason, count in sorted(result.skip_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:30s}: {count}")
    print("=" * 55)

    if result.errors:
        print("\n  [ERRORS]")
        for err in result.errors[:10]:
            print(f"    - {err}")

    print()


if __name__ == "__main__":
    main()
