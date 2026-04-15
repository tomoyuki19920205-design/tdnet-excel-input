#!/usr/bin/env python3
"""send_4q_samples.py — 4Q決算サンプル5件をDiscordに送信

Usage:
    python tools/send_4q_samples.py              # 実際にDiscordへ送信
    python tools/send_4q_samples.py --dry-run    # ログ出力のみ
    python tools/send_4q_samples.py --date 2026-03-13
"""
from __future__ import annotations

import io
import logging
import os
import sys
import time
import unicodedata
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.env_loader import load_project_env, get_project_root
from src.downloader import download_document
from src.fetcher import fetch_new_disclosures
from src.events.summary_financials import extract_earnings_data
from src.events.summary_notify import format_earnings_message, send_earnings_discord
from src.events.earnings_guidance_extractor import (
    extract_guidance_from_zip,
    format_guidance_section,
)
from src.events.earnings_production_pipeline import _is_fy_or_4q

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("4q_samples")

MAX_SAMPLES = 3


def main():
    if sys.stdout and hasattr(sys.stdout, "encoding"):
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace",
            )

    load_project_env()

    import argparse
    parser = argparse.ArgumentParser(description="4Q決算サンプル Discord送信")
    parser.add_argument("--date", type=str, default=None, help="対象日付 (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="送信しない")
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not args.dry_run and not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL が未設定です。--dry-run で試すか環境変数を設定してください。")
        sys.exit(1)

    xbrl_dir = str(get_project_root() / "data" / "xbrl_archive")

    # ---- 開示取得 ----
    target_date = args.date
    logger.info(f"Fetching disclosures for date={target_date or 'today'}")
    items = fetch_new_disclosures(target_date=target_date)
    logger.info(f"Total disclosures: {len(items)}")

    # ---- 4Q銘柄を5件まで処理 ----
    sent_count = 0
    for item in items:
        if sent_count >= MAX_SAMPLES:
            break

        # 決算短信フィルタ
        title_norm = unicodedata.normalize("NFKC", item.title)
        if "決算短信" not in title_norm:
            continue
        if any(kw in title_norm for kw in ["説明会", "補足", "訂正", "参考"]):
            continue

        ticker = item.ticker
        company_name = item.company_name

        # ZIPダウンロード
        xbrl_path = None
        url = getattr(item, 'xbrl_url', None) or getattr(item, 'doc_url', None)
        if url:
            try:
                xbrl_path = download_document(url, xbrl_dir)
            except Exception as e:
                logger.warning(f"{ticker} ZIP DL失敗: {e}")

        if not xbrl_path:
            continue

        # 財務データ抽出
        try:
            earnings = extract_earnings_data(
                xbrl_path=xbrl_path, title=item.title, ticker=ticker,
            )
        except Exception as e:
            logger.warning(f"{ticker} 抽出失敗: {e}")
            continue

        if earnings is None:
            continue

        # 4Q判定（引数は earnings オブジェクト + title）
        is_4q, fy_reason = _is_fy_or_4q(earnings, title_norm)
        if not is_4q:
            continue

        logger.info(f"✅ 4Q [{sent_count + 1}/{MAX_SAMPLES}]: {ticker} {company_name}")

        # サマリー生成（EarningsSummaryData のメソッド）
        summary_line = earnings.format_summary_line(clip=2.0)
        segment_lines = earnings.format_segment_lines() if earnings.segments else ""

        # 通知メッセージ生成（ナラティブはAI不要なので空）
        full_message = format_earnings_message(
            ticker=ticker,
            company_name=company_name,
            summary_line=summary_line,
            segment_lines=segment_lines,
            company_reasons=[],
            segment_reasons=[],
            title=item.title,
        )

        # ガイダンス抽出
        try:
            guidance = extract_guidance_from_zip(
                xbrl_path=xbrl_path,
                actual_sales=earnings.sales_current,
                actual_op=earnings.op_current,
            )
            if guidance:
                guidance_section = format_guidance_section(guidance)
                if guidance_section:
                    full_message += "\n\n" + guidance_section
                    logger.info(
                        f"  ガイダンス: sales={guidance.sales_forecast} "
                        f"op={guidance.op_forecast} eps={guidance.eps_forecast}"
                    )
        except Exception as e:
            logger.warning(f"  ガイダンスエラー: {e}")

        # 出力
        print(f"\n{'='*55}")
        print(full_message)
        print(f"{'='*55}")

        if not args.dry_run:
            ok = send_earnings_discord(webhook_url, full_message)
            if ok:
                logger.info(f"✅ Discord送信成功: {ticker}")
            else:
                logger.error(f"❌ Discord送信失敗: {ticker}")
            time.sleep(2)
        else:
            logger.info(f"[DRY-RUN] {ticker} — 送信スキップ")

        sent_count += 1

    logger.info(f"\n完了: {sent_count}/{MAX_SAMPLES} 件")


if __name__ == "__main__":
    main()
