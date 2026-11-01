"""Typed J-Quants upper-half EPS and zero profit retain their units and scope."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.sync_2q_forecasts import select_recent


class SourceFieldTest(unittest.TestCase):
    def test_eps_yen_and_zero_profit_from_same_official_2q_row(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "jquants.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE market_data_universe (date TEXT, code TEXT, ticker TEXT, is_ordinary_stock INTEGER)")
            conn.execute("CREATE TABLE jquants_tdnet_metadata (disclosure_id TEXT, title TEXT, disclosed_date TEXT)")
            conn.execute("""CREATE TABLE jquants_financials_normalized (
                local_code TEXT,current_fiscal_year_end_date TEXT,type_of_current_period TEXT,
                type_of_document TEXT,disclosed_date TEXT,raw_json TEXT)""")
            conn.execute("INSERT INTO market_data_universe VALUES ('2026-10-30','99990','9999',1)")
            conn.execute("INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?)", (
                "99990", "2027-03-31", "1Q", "1QFinancialStatements_Consolidated_JP", "2026-10-30",
                json.dumps({"DiscNo": "20261030560000", "DiscTime": "15:30:00",
                    "CurFYEn": "2027-03-31", "FSales2Q": "12000000000",
                    "FOP2Q": "0", "FOdP2Q": "-100000000", "FEPS2Q": "12.34"}),
            ))
            conn.execute("INSERT INTO jquants_financials_normalized VALUES (?,?,?,?,?,?)", (
                "99990", "2026-03-31", "FY", "FYFinancialStatements_Consolidated_JP", "2026-08-01",
                json.dumps({"DiscNo": "20260801510000", "DiscTime": "15:30:00",
                    "CurFYEn": "2026-03-31", "NxtFYEn": "2027-03-31",
                    "NxFSales2Q": "9000000000", "NxFOP2Q": "500000000",
                    "NxFNp2Q": "300000000", "NxFEPS2Q": "8.76"}),
            ))
            conn.commit()
            conn.close()
            disclosures, scopes, blanks, statuses, _ = select_recent(path, "2026-07-01")
            self.assertEqual(len(disclosures), 2)
            self.assertEqual(disclosures[1][0:3], ("9999", "2027-03-31", "consolidated"))
            self.assertEqual(disclosures[1][-1], {
                "sales": 12000.0, "operating_profit": 0.0,
                "ordinary_profit": -100.0, "eps": 12.34,
            })
            self.assertEqual(disclosures[0][-1], {
                "sales": 9000.0, "operating_profit": 500.0,
                "net_income": 300.0, "eps": 8.76,
            })
            self.assertEqual(scopes[("9999", "2027-03-31")], "consolidated")
            self.assertEqual(blanks, {})
            self.assertEqual(statuses, [])


if __name__ == "__main__":
    unittest.main()
