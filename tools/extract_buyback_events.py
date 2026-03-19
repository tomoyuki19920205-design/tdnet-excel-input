#!/usr/bin/env python3
"""extract_buyback_events.py — 自社株買いイベント抽出 CLI

Usage:
  python tools/extract_buyback_events.py --input sample.html --ticker 6750 \\
      --title "自己株式取得に係る事項の決定" --disclosure-date 2025-04-01 \\
      --print-json --no-save

  python tools/extract_buyback_events.py --input sample.pdf \\
      --ticker 6750 --source-type pdf --db data/decision_db.db
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.events.buyback_classifier import classify_buyback
from src.events.buyback_extractor import extract_buyback_event
from src.events.buyback_storage import ensure_buyback_table, upsert_buyback_event

logger = logging.getLogger("buyback_cli")


# ============================================================
# テキスト読み込み
# ============================================================
def _load_text_from_html(path: str) -> str:
    """HTML ファイルからプレーンテキストを抽出"""
    from bs4 import BeautifulSoup
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n")


def _load_text_from_pdf(path: str) -> str:
    """PDF ファイルからテキストを抽出"""
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber が未インストールです。pip install pdfplumber を実行してください。")
        sys.exit(1)

    pages_text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
    return "\n".join(pages_text)


def _load_text(path: str, source_type: str) -> str:
    """ファイルからテキストを取得"""
    if not os.path.exists(path):
        logger.error(f"ファイルが見つかりません: {path}")
        sys.exit(1)

    if source_type == "pdf":
        return _load_text_from_pdf(path)
    elif source_type == "html":
        return _load_text_from_html(path)
    else:
        # テキストファイル
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _guess_source_type(path: str) -> str:
    """ファイル拡張子から source_type を推定"""
    ext = Path(path).suffix.lower()
    if ext in (".html", ".htm"):
        return "html"
    elif ext == ".pdf":
        return "pdf"
    else:
        return "text"


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="TDnet 自社株買いイベント抽出 CLI"
    )
    parser.add_argument("--input", help="入力ファイルパス (HTML/PDF/テキスト)")
    parser.add_argument("--text-file", help="プレーンテキストファイルパス")
    parser.add_argument("--ticker", default="", help="銘柄コード (4桁)")
    parser.add_argument("--title", default="", help="開示タイトル")
    parser.add_argument("--disclosure-date", default="", help="開示日 (YYYY-MM-DD)")
    parser.add_argument("--source-type", choices=["html", "pdf", "text"],
                        help="入力形式 (未指定時は拡張子から推定)")
    parser.add_argument("--doc-id", default=None, help="文書ID")
    parser.add_argument("--source-url", default=None, help="元URL")
    parser.add_argument("--db", default="decision_db.db", help="SQLite DBパス")
    parser.add_argument("--print-json", action="store_true", help="JSON出力")
    parser.add_argument("--no-save", action="store_true", help="DB保存しない")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1. テキスト取得
    if args.input:
        source_type = args.source_type or _guess_source_type(args.input)
        text = _load_text(args.input, source_type)
        source_path = os.path.abspath(args.input)
    elif args.text_file:
        source_type = "text"
        text = _load_text(args.text_file, "text")
        source_path = os.path.abspath(args.text_file)
    else:
        logger.error("--input または --text-file を指定してください")
        sys.exit(1)

    if not text.strip():
        logger.error("テキストが空です")
        sys.exit(1)

    logger.info(f"テキスト読み込み完了: {len(text):,} 文字")

    # 2. 分類
    title = args.title or ""
    classification = classify_buyback(title, text[:1000])

    logger.info(
        f"分類結果: is_buyback={classification.is_buyback_related} "
        f"event_type={classification.event_type_candidate} "
        f"confidence={classification.confidence}"
    )

    if not classification.is_buyback_related:
        result = {
            "is_buyback_related": False,
            "classification": classification.to_dict(),
            "message": "自社株買い関連文書ではありません",
        }
        if args.print_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[SKIP] 自社株買い関連文書ではありません (confidence={classification.confidence})")
        sys.exit(0)

    event_type = classification.event_type_candidate
    if not event_type:
        logger.warning("event_type を推定できませんでした。buyback_decision として処理します。")
        event_type = "buyback_decision"

    # 3. 抽出
    event = extract_buyback_event(
        text=text,
        event_type=event_type,
        ticker=args.ticker,
        disclosure_date=args.disclosure_date,
        title=title,
        source_type=source_type,
        source_path=source_path,
        source_doc_id=args.doc_id,
        source_url=args.source_url,
    )

    logger.info(
        f"抽出完了: event_type={event.event_type} "
        f"confidence={event.extraction_confidence}"
    )

    # 4. 出力
    if args.print_json:
        output = {
            "is_buyback_related": True,
            "classification": classification.to_dict(),
            "event": event.to_dict(),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    # 5. 保存
    if not args.no_save:
        import sqlite3
        db_path = args.db
        if not os.path.isabs(db_path):
            db_path = os.path.join(_PROJECT_ROOT, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        ensure_buyback_table(conn)

        record_id = upsert_buyback_event(conn, event)
        conn.close()

        print(f"\n保存完了: buyback_events id={record_id}")
    elif not args.print_json:
        # no-save + no-print でもサマリは出す
        print(f"\n[DRY-RUN] event_type={event.event_type} "
              f"ticker={event.ticker} "
              f"confidence={event.extraction_confidence}")
        if event.shares_limit:
            print(f"  shares_limit: {event.shares_limit:,}")
        if event.shares_acquired:
            print(f"  shares_acquired: {event.shares_acquired:,}")
        if event.shares_cancelled:
            print(f"  shares_cancelled: {event.shares_cancelled:,}")
        if event.amount_limit_million_yen:
            print(f"  amount_limit: {event.amount_limit_million_yen:,.0f} 百万円")
        if event.amount_acquired_million_yen:
            print(f"  amount_acquired: {event.amount_acquired_million_yen:,.0f} 百万円")
        if event.start_date:
            print(f"  period: {event.start_date} ~ {event.end_date}")
        if event.acquisition_method:
            print(f"  method: {event.acquisition_method}")


if __name__ == "__main__":
    main()
