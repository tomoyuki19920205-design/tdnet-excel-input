import sys
from datetime import date
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.sync_margin_interest import (  # noqa: E402
    api_pages, last_days_of_weeks, normalize_snapshot, official_nikkei_closes, subtract_months, trading_days,
)


class Response:
    status_code = 200
    def __init__(self, body=None, content=b""):
        self.body, self.content = body, content
    def raise_for_status(self):
        return None
    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses, self.queries = list(responses), []
    def get(self, url, **kwargs):
        self.queries.append(kwargs.get("params"))
        return self.responses.pop(0)


class MarginTests(unittest.TestCase):
    def test_pagination_reaches_last_page(self):
        session = Session([Response({"data": [{"Code": "12340"}], "pagination_key": "second"}),
                           Response({"data": [{"Code": "12341"}]})])
        rows = api_pages(session, "/markets/margin-interest", {"date": "2026-10-21"}, {})
        self.assertEqual([row["Code"] for row in rows], ["12340", "12341"])
        self.assertEqual(session.queries[-1]["pagination_key"], "second")

    def test_duplicate_page_key_fails_without_publication(self):
        session = Session([Response({"data": [{}], "pagination_key": "same"}),
                           Response({"data": [{}], "pagination_key": "same"})])
        with self.assertRaisesRegex(ValueError, "pagination_key"):
            api_pages(session, "/markets/margin-interest", {"date": "2026-10-21"}, {})

    def test_duplicate_security_and_missing_money_fail(self):
        row = {"Date": "2026-10-21", "PubDate": "2026-10-22", "Code": "12340",
               "LongVol": 10, "ShrtVol": 2, "LongVal": 101, "ShrtVal": 2}
        master = {"12340": {"S33": "2050"}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize_snapshot([row, row], date(2026, 10, 21), master)
        with self.assertRaisesRegex(ValueError, "LongVal"):
            normalize_snapshot([{**row, "LongVal": None}], date(2026, 10, 21), master)

    def test_exact_codes_and_categories_preserve_reconciliation(self):
        base = {"Date": "2026-10-21", "PubDate": "2026-10-22", "LongVol": 5, "ShrtVol": 1, "ShrtVal": 1}
        raw = [{**base, "Code": "12340", "LongVal": 100},
               {**base, "Code": "12341", "LongVal": 200},
               {**base, "Code": "12342", "LongVal": 300}]
        master = {"12340": {"S33": "2050"}, "12341": {"S33": "9999"}}
        rows, pub = normalize_snapshot(raw, date(2026, 10, 21), master)
        self.assertEqual([row["category"] for row in rows], ["sector", "outside", "unknown"])
        self.assertEqual(sum(row["long_val"] for row in rows), 600)
        self.assertEqual(pub, date(2026, 10, 22))

    def test_old_week_volume_stays_without_money(self):
        raw = [{"Date": "2026-09-18", "PubDate": None, "Code": "12340",
                "LongVol": 3, "ShrtVol": 1, "LongVal": None, "ShrtVal": None}]
        rows, pub = normalize_snapshot(raw, date(2026, 9, 18), {"12340": {"S33": "2050"}})
        self.assertIsNone(rows[0]["long_val"])
        self.assertIsNone(pub)

    def test_week_final_uses_official_holiday_calendar(self):
        # Thursday is valid only when the following Friday is a market holiday.
        days = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22),
                date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)]
        self.assertEqual(last_days_of_weeks(days), {date(2026, 10, 22), date(2026, 10, 30)})

    def test_ose_holiday_trading_is_not_tse_business_day(self):
        session = Session([Response({"data": [
            {"Date": "2026-10-09", "HolDiv": "1"},
            {"Date": "2026-10-12", "HolDiv": "3"},
            {"Date": "2026-10-13", "HolDiv": "1"},
        ]})])
        self.assertEqual(trading_days(session, date(2026, 10, 9), date(2026, 10, 13), {}),
                         [date(2026, 10, 9), date(2026, 10, 13)])

    def test_old_two_day_week_has_no_weekly_publication(self):
        self.assertEqual(last_days_of_weeks([date(2026, 5, 7), date(2026, 5, 8)]), set())

    def test_calendar_month_cutoffs_are_inclusive(self):
        self.assertEqual(subtract_months(date(2026, 10, 22), 1), date(2026, 9, 22))
        self.assertEqual(subtract_months(date(2026, 8, 31), 6), date(2026, 2, 28))

    def test_official_nikkei_csv_is_cash_close_not_open(self):
        content = 'データ日付,終値,始値,高値,安値\n"2026/10/21","64621.24","64885.95","65404.63","64558.60"\n'.encode("cp932")
        closes = official_nikkei_closes(Session([Response(content=content)]))
        self.assertEqual(closes[date(2026, 10, 21)], "64621.24")


if __name__ == "__main__":
    unittest.main()
