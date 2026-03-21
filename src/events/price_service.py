#!/usr/bin/env python3
"""price_service.py — 株価取得サービス

フォールバック構造:
1. SQLite DB (decision_db.db の daily_quotes / stock_prices テーブル)
2. 将来: J-Quants API 等のオンライン取得
3. 取得できなければ None を返す（処理継続）
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger("price_service")


def get_last_close(
    ticker: str,
    as_of: date | None = None,
    db_path: str | None = None,
) -> Optional[float]:
    """指定銘柄の前日終値を取得する。

    Parameters
    ----------
    ticker : str
        銘柄コード (4桁 or 5桁)
    as_of : date, optional
        基準日。None の場合は今日。
    db_path : str, optional
        SQLite DB のパス。None の場合は decision_db.db を探す。

    Returns
    -------
    float or None
        前日終値（円）。取得できない場合は None。
    """
    if as_of is None:
        as_of = date.today()

    # 最大7日前までさかのぼる（休日対応）
    dates_to_try = [(as_of - timedelta(days=i)).isoformat() for i in range(1, 8)]

    # Source 1: SQLite DB
    if db_path is None:
        # デフォルトパスを探索
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "..", "decision_db.db"),
            os.path.join(os.path.dirname(__file__), "..", "..", "data", "decision_db.db"),
        ]
        for c in candidates:
            p = os.path.normpath(c)
            if os.path.isfile(p):
                db_path = p
                break

    if db_path and os.path.isfile(db_path):
        price = _get_from_sqlite(db_path, ticker, dates_to_try)
        if price is not None:
            return price

    # Source 2: 将来の API 取得（未実装、フォールバック構造のみ）
    # price = _get_from_api(ticker, dates_to_try)
    # if price is not None:
    #     return price

    logger.debug(f"[PRICE] No price found for {ticker} as_of={as_of}")
    return None


def _get_from_sqlite(
    db_path: str,
    ticker: str,
    dates: list[str],
) -> Optional[float]:
    """SQLite DB から終値を取得。

    テーブル名は複数パターン対応（daily_quotes, stock_prices, prices）。
    """
    # ティッカー正規化: 4桁へ
    ticker_4 = ticker[:4] if len(ticker) >= 4 else ticker

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 対応テーブル候補
        for table, ticker_col, date_col, price_col in [
            ("daily_quotes", "code", "date", "close"),
            ("daily_quotes", "ticker", "date", "close"),
            ("stock_prices", "ticker_code", "date", "closing_price"),
            ("stock_prices", "code", "date", "close"),
            ("prices", "ticker", "date", "close"),
        ]:
            try:
                placeholders = ",".join(["?"] * len(dates))
                sql = (
                    f"SELECT {price_col} FROM {table} "
                    f"WHERE {ticker_col} IN (?, ?) "
                    f"AND {date_col} IN ({placeholders}) "
                    f"ORDER BY {date_col} DESC LIMIT 1"
                )
                params = [ticker_4, ticker] + dates
                row = conn.execute(sql, params).fetchone()
                if row and row[0] is not None:
                    price = float(row[0])
                    if price > 0:
                        logger.debug(f"[PRICE] Found {ticker}={price} from {table}")
                        return price
            except sqlite3.OperationalError:
                # テーブルやカラムが存在しない
                continue

        conn.close()
    except Exception as e:
        logger.debug(f"[PRICE] SQLite error: {e}")

    return None
