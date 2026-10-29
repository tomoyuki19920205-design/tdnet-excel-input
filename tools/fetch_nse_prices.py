"""Fetch verified Nagoya Stock Exchange ordinary-share closes from its daily PDF.

Only securities in nse_ordinary_stocks.json are eligible. No price is embedded
in the mapping. Dry-run is read-only. The date is the report's trading date.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import requests

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
REPORT_INDEX = "https://www.nse.or.jp/market/condition/report/"
CONFIG = Path(__file__).with_name("nse_ordinary_stocks.json")
DEFAULT_DB = ROOT / "data" / "jquants.db"


def latest_report_date(html: str, today: str) -> str | None:
    dates = [x for x in re.findall(r'files/(\d{8})\.pdf', html)
             if x <= today.replace("-", "")]
    return max(dates) if dates else None


def parse_quote(pdf_bytes: bytes, code: str, name_fragment: str) -> dict | None:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for line in page.get_text(sort=True).splitlines():
                if not re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", line):
                    continue
                tail = line.split(code, 1)[1]
                if name_fragment not in tail:
                    continue
                # The daily report prints two securities per line. Stop before
                # the next five-character security code in the right column.
                next_code = re.search(r"\s{3,}\d{5}\s", tail)
                if next_code:
                    tail = tail[:next_code.start()]
                amounts = re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", tail)
                if len(amounts) < 4:
                    return None  # no trade: keep the prior closing price
                prices = [float(x) for x in amounts[:4]]
                if any(x <= 0 for x in prices) or prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]):
                    raise ValueError(f"invalid NSE OHLC for {code}")
                return {"open": prices[0], "high": prices[1], "low": prices[2],
                        "close": prices[3], "raw_line": line.strip(), "pdf_page": page.number + 1}
    return None


def quote_to_row(ticker: str, date: str, quote: dict, shares: int | None) -> dict:
    return {"ticker": ticker, "date": date, "open": quote["open"], "high": quote["high"],
            "low": quote["low"], "close": quote["close"], "volume": None,
            "turnover": None, "adj_factor": 1.0, "adj_close": quote["close"],
            "adj_volume": None, "market_cap": quote["close"] * shares if shares else None,
            "source": "nse_daily_report"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--date", help="YYYY-MM-DD trading date; default latest listed report")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    with requests.Session() as session:
        page = session.get(REPORT_INDEX, timeout=(8, 30))
        page.raise_for_status()
        today = datetime.now(JST).date().isoformat()
        date8 = (args.date or latest_report_date(page.text, today))
        if not date8:
            print("no NSE report available; existing prices retained")
            return
        date8 = date8.replace("-", "")
        if not re.fullmatch(r"\d{8}", date8) or date8 > today.replace("-", ""):
            raise ValueError("invalid future report date")
        if date8 not in re.findall(r'files/(\d{8})\.pdf', page.text):
            raise ValueError("report date absent from official index")
        report_date = f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"
        url = f"{REPORT_INDEX}files/{date8}.pdf"
        response = session.get(url, timeout=(8, 30))
        response.raise_for_status()
        raw = response.content
        if not raw.startswith(b"%PDF"):
            raise ValueError("NSE response is not PDF")
        digest = hashlib.sha256(raw).hexdigest()
    uri = args.db.resolve().as_uri() + "?mode=ro" if not args.apply else str(args.db)
    cx = sqlite3.connect(uri, uri=not args.apply, timeout=5)
    result = []
    try:
        for ticker, info in cfg.items():
            if info["security_type"] != "common_stock" or info["market"] != "NSE_MAIN" or info["code"] != ticker + "0":
                raise ValueError(f"invalid ordinary-share identity: {ticker}")
            quote = parse_quote(raw, info["code"], info["name_fragment"])
            if quote is None:
                result.append({"ticker": ticker, "date": report_date, "status": "no_trade_or_absent"})
                continue
            shares = cx.execute(
                "SELECT shares_outstanding FROM per_share_data WHERE ticker=? AND disclosed_date<=? "
                "AND shares_outstanding>0 ORDER BY disclosed_date DESC LIMIT 1",
                (ticker, report_date)).fetchone()
            row = quote_to_row(ticker, report_date, quote, int(shares[0]) if shares else None)
            previous = cx.execute("SELECT close,source FROM market_data WHERE ticker=? AND date=?",
                                  (ticker, report_date)).fetchone()
            result.append({"ticker": ticker, "date": report_date, "status": "unchanged" if previous and previous[0] == row["close"] else "new",
                           "row": row, "evidence": {"url": url, "sha256": digest,
                           "pdf_page": quote["pdf_page"], "raw_line": quote["raw_line"]}})
            if args.apply:
                cx.execute("""CREATE TABLE IF NOT EXISTS market_data_provider_universe (
                    ticker TEXT PRIMARY KEY, code TEXT NOT NULL, market TEXT NOT NULL,
                    security_type TEXT NOT NULL, source TEXT NOT NULL, identity_source TEXT NOT NULL)""")
                cx.execute("""CREATE TABLE IF NOT EXISTS nse_price_raw_evidence (
                    ticker TEXT NOT NULL, date TEXT NOT NULL, url TEXT NOT NULL, sha256 TEXT NOT NULL,
                    pdf_page INTEGER NOT NULL, raw_line TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    PRIMARY KEY(ticker,date))""")
                cx.execute("""INSERT INTO market_data_provider_universe VALUES (?,?,?,?,?,?)
                    ON CONFLICT(ticker) DO UPDATE SET code=excluded.code, market=excluded.market,
                    security_type=excluded.security_type, source=excluded.source,
                    identity_source=excluded.identity_source
                    WHERE market_data_provider_universe.code IS NOT excluded.code
                    OR market_data_provider_universe.market IS NOT excluded.market
                    OR market_data_provider_universe.security_type IS NOT excluded.security_type
                    OR market_data_provider_universe.source IS NOT excluded.source
                    OR market_data_provider_universe.identity_source IS NOT excluded.identity_source""",
                    (ticker,info["code"],info["market"],info["security_type"],"nse_daily_report",info["identity_source"]))
                fetched = datetime.now(JST).isoformat()
                cx.execute("""INSERT INTO market_data
                    (ticker,date,open,high,low,close,volume,turnover,adj_factor,adj_close,adj_volume,market_cap,source,fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(ticker,date) DO UPDATE SET open=excluded.open,high=excluded.high,
                    low=excluded.low,close=excluded.close,market_cap=excluded.market_cap,
                    fetched_at=excluded.fetched_at
                    WHERE market_data.source='nse_daily_report' AND
                    (market_data.close IS NOT excluded.close OR market_data.market_cap IS NOT excluded.market_cap)""",
                    tuple(row[k] for k in ("ticker","date","open","high","low","close","volume","turnover","adj_factor","adj_close","adj_volume","market_cap","source"))+(fetched,))
                cx.execute("""INSERT INTO nse_price_raw_evidence VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(ticker,date) DO UPDATE SET url=excluded.url,sha256=excluded.sha256,
                    pdf_page=excluded.pdf_page,raw_line=excluded.raw_line,fetched_at=excluded.fetched_at
                    WHERE nse_price_raw_evidence.sha256 IS NOT excluded.sha256
                    OR nse_price_raw_evidence.raw_line IS NOT excluded.raw_line""",
                    (ticker,report_date,url,digest,quote["pdf_page"],quote["raw_line"],fetched))
        if args.apply:
            cx.commit()
    finally:
        cx.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("NSE", report_date, "; ".join(f"{x['ticker']}:{x['status']}" for x in result),
          "apply" if args.apply else "dry-run")


if __name__ == "__main__":
    main()
