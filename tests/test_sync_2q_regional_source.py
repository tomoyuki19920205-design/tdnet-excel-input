"""Regional exchange 2Q guidance keeps the original scope and metric meaning."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.sync_2q_forecasts import (
    regional_scope_from_text,
    regional_source_url,
    select_recent,
    supplement_regional_current_revision,
)


class RegionalSourceTest(unittest.TestCase):
    def test_exchange_urls_and_partial_bank_values(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jquants.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE market_data_universe (date TEXT, code TEXT, ticker TEXT, is_ordinary_stock INTEGER)")
            conn.execute("CREATE TABLE jquants_tdnet_metadata (disclosure_id TEXT, title TEXT, disclosed_date TEXT)")
            conn.execute("""CREATE TABLE jquants_financials_normalized (
                local_code TEXT,current_fiscal_year_end_date TEXT,type_of_current_period TEXT,
                type_of_document TEXT,disclosed_date TEXT,raw_json TEXT)""")
            conn.execute("INSERT INTO market_data_universe VALUES ('2026-10-30','99990','9999',1)")
            cases = [
                ("59790", "2027-03-31", "2026-10-27", "20261027556832",
                 {"FNCSales2Q": "3816000000", "FNCOdP2Q": "272000000", "FNCNP2Q": "184000000", "FNCEPS2Q": "64.96"}),
                ("62250", "2027-07-31", "2026-09-11", "20260911534695",
                 {"FNCSales2Q": "1170000000", "FNCOP2Q": "154000000", "FNCOdP2Q": "161000000"}),
                ("83980", "2027-03-31", "2026-10-27", "20261022554787",
                 {"FSales2Q": "13925000000", "FOdP2Q": "1295000000", "FNP2Q": "805000000", "FEPS2Q": "137.59"}),
            ]
            for local, period, date, filing, fields in cases:
                conn.execute("INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?)", (
                    local, period, "2Q", "EarnForecastRevision", date,
                    json.dumps({"DiscNo": filing, "DiscTime": "15:30:00", "CurFYEn": period, **fields}),
                ))
            conn.commit()
            conn.close()
            regional = {"59790": ("5979", "nse", False), "62250": ("6225", "nse", False),
                        "83980": ("8398", "fse", True)}
            disclosures, *_ = select_recent(db, "2026-09-01", regional)
            by_ticker = {item[0]: item for item in disclosures}
            self.assertEqual(by_ticker["5979"][5], regional_source_url("nse", "59790", "20261027556832", "2026-10-27"))
            self.assertEqual(by_ticker["6225"][-1]["operating_profit"], 154.0)
            self.assertEqual(by_ticker["8398"][5], "https://www.fse.or.jp/files/lis_tkj/26102783980.pdf")
            self.assertNotIn("sales", by_ticker["8398"][-1])
            self.assertEqual(by_ticker["8398"][-1]["ordinary_profit"], 1295.0)

    def test_current_revision_row_and_scope(self):
        text = "個別業績予想\n売上高 営業利益 経常利益 中間純利益 EPS\n前回発表予想 (A)\n3,930\n356\n372\n256\n89.92\n今回修正予想 (B)\n3,816\n255\n272\n184\n64.96\n"
        values = {"sales": 3816.0, "operating_profit": None,
                  "ordinary_profit": 272.0, "net_income": 184.0, "eps": 64.96}
        self.assertEqual(supplement_regional_current_revision(text, values)["operating_profit"], 255.0)
        self.assertEqual(regional_scope_from_text("連結業績予想数値の修正個別業績予想数値の修正"), "consolidated")
        self.assertEqual(regional_scope_from_text("個別業績予想数値の修正"), "nonconsolidated")
        self.assertEqual(regional_scope_from_text("非連結業績予想数値の修正"), "nonconsolidated")


if __name__ == "__main__":
    unittest.main()
