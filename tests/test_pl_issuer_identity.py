import json
import sqlite3

import pytest

from lib.pipeline.issuer_identity import assess_issuer
from tools.sync_financials import read_sqlite, read_forecast_rows


@pytest.mark.parametrize("code,native", [
    ("27600", "27600"), ("2760", "27600"),
    ("276A0", "276A0"), ("276A", "276A0"),
    ("1A010", "1A010"), ("123C0", "123C0"),
    ("1A2C0", "1A2C0"), (" 276a0 ", "276A0"),
])
def test_explicit_native_identity_is_preserved(code, native):
    assert assess_issuer(code, {"Code": native})["allowed"]


@pytest.mark.parametrize("code,native", [("276A", "27600"), ("2760", "276A0"),
                                        ("418A", "41800"), ("1A01", "10010")])
def test_numeric_alpha_issuer_conflict_is_rejected(code, native):
    d = assess_issuer(code, {"Code": native})
    assert not d["allowed"]
    assert d["reason"] == "native_security_code_conflict"


@pytest.mark.parametrize("code", ["276000", "27601", "ABC", "", None])
def test_unknown_shape_is_not_guessed(code):
    assert not assess_issuer(code, {})["allowed"]


def test_no_native_identifier_is_explicitly_unverified():
    d = assess_issuer("276A", {})
    assert d["allowed"] and not d["verified"]
    assert d["warnings"] == ["native_security_code_missing"]


def test_two_conflicting_native_identifiers_are_not_silently_selected():
    assert not assess_issuer("276A", {"Code": "276A0", "LocalCode": "27600"})["allowed"]


@pytest.mark.parametrize("payload,master", [
    ({"Code": "276A0", "EdinetCode": "E10000"}, {"edinet_code": "E20000"}),
    ({"Code": "276A0", "CorporateNumber": "1234567890123"}, {"corporate_number": "9876543210123"}),
])
def test_verified_master_identifiers_can_reject_an_explicit_conflict(payload, master):
    assert not assess_issuer("276A", payload, master)["allowed"]


def test_name_fiscal_change_and_short_year_are_review_signals_only():
    d = assess_issuer("4331", {"Code": "43310", "CompanyName": "New holding name",
                               "CurFYSt": "2025-04-01", "CurFYEn": "2025-12-31"},
                      {"name_ja": "Old name", "historical_fiscal_ends": ["2025-03-31"]})
    assert d["allowed"]
    assert set(d["warnings"]) == {"company_name_review", "fiscal_period_review"}


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE jquants_financials_normalized(
        local_code TEXT, disclosed_date TEXT, current_fiscal_year_end_date TEXT,
        type_of_current_period TEXT, type_of_document TEXT, net_sales INTEGER,
        gross_profit INTEGER, operating_profit INTEGER, profit_before_tax INTEGER,
        raw_json TEXT, fetched_at TEXT)""")
    return c


def put(c, *, code="276A0", native="276A0", period="2026-08-31", quarter="FY",
        date="2026-10-15", sales=7020, gp=1816, op=1144, malformed=False):
    raw = {"Code": native, "DiscNo": date.replace("-", "") + "000001",
           "CurFYEn": period, "CurPerType": quarter,
           "Sales": None if sales is None else str(sales * 1000000),
           "OP": None if op is None else str(op * 1000000),
           "NxFSales": "11000000000", "NxFOP": "2200000000"}
    c.execute("INSERT INTO jquants_financials_normalized VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (code, date, period, quarter, quarter + "FinancialStatements_Consolidated_JP",
               None if sales is None else sales * 1000000,
               None if gp is None else gp * 1000000,
               None if op is None else op * 1000000, None,
               "{not-json" if malformed else json.dumps(raw), date))


def test_foreign_latest_and_foreign_sparse_correction_never_enter_pl(tmp_path, caplog):
    path = tmp_path / "source.db"
    c = make_db(path)
    put(c)
    put(c, date="2026-10-20", sales=7100, gp=None, op=1200)  # legitimate correction
    put(c, native="27600", date="2026-10-21", sales=None, gp=9306, op=4066)
    put(c, native="27600", period="2027-03-31", quarter="1Q", sales=60322, op=4066)
    c.commit();c.close()
    rows, stats = read_sqlite(str(path))
    assert len(rows) == 1
    assert (rows[0]["ticker"], rows[0]["period"], rows[0]["quarter"]) == ("276A", "2026-08-31", "FY")
    assert (rows[0]["sales"], rows[0]["gross_profit"], rows[0]["operating_profit"]) == (7100, 1816, 1200)
    assert len(stats["issuer_quarantined"]) == 2
    assert "ISSUER_QUARANTINE" in caplog.text
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM jquants_financials_normalized").fetchone()[0] == 4
    c.close()


def test_forecast_uses_same_identity_gate_before_latest_selection(tmp_path):
    path = tmp_path / "source.db";c = make_db(path)
    put(c)
    put(c, native="27600", date="2026-10-21", sales=60322, gp=9306, op=4066)
    c.commit();c.close()
    rows = read_forecast_rows(str(path))
    assert len(rows) == 1
    assert (rows[0]["ticker"], rows[0]["period"], rows[0]["sales"], rows[0]["operating_profit"]) == ("276A", "2027-08-31", 11000, 2200)


def test_invalid_native_json_is_logged_and_retained_without_breaking_sql(tmp_path):
    path = tmp_path / "source.db";c = make_db(path)
    put(c, malformed=True);c.commit();c.close()
    rows, stats = read_sqlite(str(path))
    assert rows == []
    assert stats["issuer_quarantined"][0]["reason"] == "invalid_native_payload"
