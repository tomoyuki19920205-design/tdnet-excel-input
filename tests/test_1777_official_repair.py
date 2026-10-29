import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import pytest
import requests

from tools.fetch_nse_prices import main as nse_main, parse_quote, quote_to_row
from tools.repair_1777_official_pl import FIXTURE, plan
from tools.sync_market_data import read_sqlite


ROOT = Path(__file__).parent / "fixtures"
METRIC_LABELS = {
    "sales": "完成工事高", "cost_of_sales": "完成工事原価",
    "gross_profit": "完成工事総利益", "sga": "販売費及び一般管理費",
    "operating_profit": "営業利益", "ordinary_profit": "経常利益",
}


def test_official_pdf_13_periods_and_standalone():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cumulative = fixture["cumulative"]
    standalone = fixture["standalone"]
    assert len(cumulative) == len(standalone) == 13
    assert len({(r["period"], r["quarter"]) for r in cumulative}) == 13
    by_key = {(r["period"], r["quarter"]): r for r in cumulative}
    for row, q in zip(cumulative, standalone):
        name = Path(row["url"]).name
        path = ROOT / "official_pdf_1777" / name.replace("ktan-20250301", "q1_25").replace("ktan-20250302", "q2_25").replace("ktan-20250303", "q3_25").replace("ktan-20250304", "fy25").replace("ktan-20260302", "q2_26").replace("ktan-20260303", "q3_26").replace("ktan-20260304", "fy26").replace("ktan-20270301", "q1_27").replace(".pdf", "_pl.txt")
        text = path.read_text(encoding="utf-8")
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        assert "非連結" in text and "百万円" in text
        col = 0 if row["column"] == "prior" else 1
        for metric, label in METRIC_LABELS.items():
            if metric == "sga" and row["quarter"] == "FY":
                label += "合計"
            lines = [x for x in text.splitlines() if re.match(r"^\s*" + label + r"\s+\d", x)]
            assert len(lines) == 1, (row["period"], metric)
            numbers = [int(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})*", lines[0].split(label, 1)[1])]
            assert numbers[col] == row[metric]
        net_label = "当期純利益" if row["quarter"] == "FY" else "中間純利益" if row["quarter"] == "2Q" else "四半期純利益"
        line = next(x for x in text.splitlines() if re.match(r"^\s*" + net_label + r"\s+\d", x))
        assert [int(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})*", line.split(net_label, 1)[1])][col] == row["net_income"]
        assert row["gross_profit"] / row["sales"] * 100 == row["gross_margin_rate"]
        prior_q = {"1Q": None, "2Q": "1Q", "3Q": "2Q", "FY": "3Q"}[row["quarter"]]
        prior = by_key.get((row["period"], prior_q)) if prior_q else None
        for metric in (*METRIC_LABELS, "net_income"):
            assert q[metric] == row[metric] - (prior[metric] if prior else 0)
        assert q["gross_margin_rate"] == q["gross_profit"] / q["sales"] * 100
    assert by_key[("2026-03-31", "FY")]["sga"] == 3636
    assert by_key[("2026-03-31", "FY")]["gross_profit"] - by_key[("2026-03-31", "FY")]["operating_profit"] == 3637


def test_pl_plan_idempotence_and_legacy_sga_alias():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = plan(fixture, [])
    assert first["insert_count"] == 78 and first["conflict_count"] == 0
    second = plan(fixture, first["inserts"])
    assert second["insert_count"] == 0 and second["conflict_count"] == 0
    sga = next(x for x in first["inserts"] if x["period"] == "2027-03-31" and x["metric"] == "selling_general_and_administrative_expenses")
    alias = {**sga, "metric": "sga", "source": "jquants", "source_priority": 2, "source_row_key": "legacy-sga"}
    third = plan(fixture, [alias])
    assert not any(x["period"] == "2027-03-31" and x["metric"] == sga["metric"] for x in third["inserts"])


def test_nse_daily_pdf_and_provider_specific_sync(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 30), "17770 KAWASAKI   2329  2352  2302  2345  -1  2.0")
    raw = doc.tobytes()
    quote = parse_quote(raw, "17770", "KAWASAKI")
    assert quote and (quote["open"], quote["high"], quote["low"], quote["close"]) == (2329, 2352, 2302, 2345)
    assert parse_quote(raw, "17770", "OTHER") is None
    db = tmp_path / "market.db"
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    with sqlite3.connect(db) as cx:
        cx.execute("CREATE TABLE market_data (ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER,turnover REAL,adj_factor REAL,adj_close REAL,adj_volume INTEGER,market_cap REAL,source TEXT,fetched_at TEXT)")
        cx.execute("CREATE TABLE market_data_universe (date TEXT,ticker TEXT,is_common_stock INTEGER)")
        cx.execute("CREATE TABLE market_data_provider_universe (ticker TEXT,code TEXT,market TEXT,security_type TEXT,source TEXT)")
        cx.execute("INSERT INTO market_data_provider_universe VALUES ('1777','17770','NSE_MAIN','common_stock','nse_daily_report')")
        cx.execute("INSERT INTO market_data_universe VALUES (?,?,1)", (today,"7203"))
        nse = quote_to_row("1777", today, quote, 12_000_000)
        tse = {**nse,"ticker":"7203","source":"jquants"}
        invalid = {**nse,"ticker":"9999","source":"nse_daily_report"}
        for row in (nse,tse,invalid):
            cx.execute("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[k] for k in ("ticker","date","open","high","low","close","volume","turnover","adj_factor","adj_close","adj_volume","market_cap","source"))+("test",))
    selected = read_sqlite(str(db), recent_days=0)
    assert {(r["ticker"],r["source"]) for r in selected} == {( "1777","nse_daily_report"),("7203","jquants")}
    assert [r["ticker"] for r in read_sqlite(str(db), recent_days=0, ticker="1777")] == ["1777"]
    assert next(r for r in selected if r["ticker"] == "1777")["market_cap"] == 28_140_000_000


def test_nse_no_trade_keeps_previous_price():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 30), "17770 KAWASAKI     no trade")
    assert parse_quote(doc.tobytes(), "17770", "KAWASAKI") is None


@pytest.mark.parametrize("failure", ["timeout", "rate_limit"])
def test_nse_fetch_failure_never_opens_write_db(monkeypatch, tmp_path, failure):
    db = tmp_path / "market.db"
    def failed_get(*args, **kwargs):
        if failure == "timeout":
            raise requests.Timeout("test timeout")
        response = requests.Response()
        response.status_code = 429
        response.url = "https://www.nse.or.jp/market/condition/report/"
        return response
    monkeypatch.setattr(requests.Session, "get", failed_get)
    monkeypatch.setattr(sys, "argv", ["fetch_nse_prices.py", "--db", str(db), "--apply"])
    with pytest.raises(requests.RequestException):
        nse_main()
    assert not db.exists()
