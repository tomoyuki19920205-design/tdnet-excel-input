import sys
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.sync_margin_interest import (  # noqa: E402
    SECTOR_INDEX_CODES, api_pages, last_days_of_weeks, normalize_snapshot,
    official_nikkei_closes, sector_index_bars, sector_index_closes, subtract_months, trading_days,
    sync_sector_indices, sync_topix_indices,
)
from tools.sync_margin_weekly_history import weekly_bar  # noqa: E402
from tools.backfill_margin_weekly_jpx_market import source_rows  # noqa: E402


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
    def test_indices_sync_uses_trading_dates_after_latest_credit_date(self):
        days = [date(2026, 10, 22), date(2026, 10, 23)]
        sector_calls = []
        def sector_api(_session, _endpoint, params, _headers):
            sector_calls.append(params["date"])
            return [{"Date": params["date"], "Code": code,
                     "O": "10", "H": "12", "L": "9", "C": "11"}
                    for code in SECTOR_INDEX_CODES.values()]
        with patch("tools.sync_margin_interest.supabase_get", return_value=[]) as saved, \
                patch("tools.sync_margin_interest.api_pages", side_effect=sector_api):
            targets, missing, errors = sync_sector_indices(
                Session([]), {}, {}, days, {days[-1]}, apply=False)
        self.assertEqual((targets, missing, errors), (2, 66, 0))
        self.assertEqual(sector_calls, [day.isoformat() for day in days])
        self.assertTrue(all(call.args[2] == "margin_index_daily" for call in saved.call_args_list))

        topix_raw = [{"Date": days[-1].isoformat(), "O": "3985.7", "H": "3986.57",
                      "L": "3907.33", "C": "3965.81"}]
        with patch("tools.sync_margin_interest.supabase_get", return_value=[]):
            targets, missing, errors = sync_topix_indices(
                Session([]), {}, topix_raw, days, {days[-1]}, apply=False)
        self.assertEqual((targets, missing, errors), (2, 2, 0))

    def test_official_sector_price_codes_are_explicit_and_complete(self):
        self.assertEqual(len(SECTOR_INDEX_CODES), 33)
        self.assertEqual(SECTOR_INDEX_CODES["0050"], "0040")
        self.assertEqual(SECTOR_INDEX_CODES["9050"], "0060")
        self.assertEqual(len(set(SECTOR_INDEX_CODES.values())), 33)

    def test_sector_index_joins_exact_date_and_rejects_invalid_close(self):
        day = date(2026, 10, 21)
        rows = [{"Date": day.isoformat(), "Code": "0040", "C": "1234.56"},
                {"Date": day.isoformat(), "Code": "0041", "C": None},
                {"Date": day.isoformat(), "Code": "0000", "C": "999"}]
        self.assertEqual(sector_index_closes(rows, day), {"0040": "1234.56"})
        with self.assertRaisesRegex(ValueError, "mixed date"):
            sector_index_closes([{**rows[0], "Date": "2026-10-20"}], day)
        with self.assertRaisesRegex(ValueError, "nonpositive"):
            sector_index_closes([{**rows[0], "C": "0"}], day)

    def test_sector_index_ohlc_is_exact_and_never_inferred_from_close(self):
        day = date(2026, 10, 21)
        row = {"Date": day.isoformat(), "Code": "0040", "O": "10", "H": "12",
               "L": "9", "C": "11"}
        self.assertEqual(sector_index_bars([row], day)["0040"],
                         {"open": "10", "high": "12", "low": "9", "close": "11"})
        self.assertIsNone(sector_index_bars([{**row, "O": None}], day)["0040"]["open"])
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            sector_index_bars([{**row, "H": "10"}], day)

    def test_topix_uses_same_exact_date_ohlc_validation(self):
        day = date(2026, 10, 21)
        row = {"Date": day.isoformat(), "Code": "TOPIX", "O": "4000", "H": "4050",
               "L": "3980", "C": "4025"}
        self.assertEqual(sector_index_bars([row], day, {"TOPIX"})["TOPIX"],
                         {"open": "4000", "high": "4050", "low": "3980", "close": "4025"})
        with self.assertRaisesRegex(ValueError, "mixed date"):
            sector_index_bars([{**row, "Date": "2026-10-20"}], day, {"TOPIX"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            sector_index_bars([row, row], day, {"TOPIX"})

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

    def test_weekly_candle_uses_all_days_and_requires_complete_ohlc(self):
        days = [date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]
        bars = {
            days[0]: {"open": "10", "high": "12", "low": "9", "close": "11"},
            days[1]: {"open": "11", "high": "15", "low": "10", "close": "13"},
            days[2]: {"open": "13", "high": "14", "low": "8", "close": "9"},
            days[3]: {"open": "9", "high": "11", "low": "9", "close": "10"},
        }
        self.assertEqual(weekly_bar(days, bars, "sample"), {
            "open": "10", "high": "15", "low": "8", "close": "10", "trading_days": 4,
        })
        self.assertIsNone(weekly_bar(days, {days[3]: bars[days[3]]}, "sample"))
        self.assertIsNone(weekly_bar(days, {**bars, days[1]: {**bars[days[1]], "open": None}}, "sample"))

    def test_official_old_market_sources_keep_tokyo_separate(self):
        records = source_rows()
        self.assertEqual(len(records), 10)
        final = records[-1]
        self.assertEqual(final["date"], "2026-09-18")
        self.assertEqual(int(final["jpx_two_market_million_yen"]) -
                         int(final["nse_nagoya_million_yen"]), 6452830)

    def test_calendar_month_cutoffs_are_inclusive(self):
        self.assertEqual(subtract_months(date(2026, 10, 22), 1), date(2026, 9, 22))
        self.assertEqual(subtract_months(date(2026, 8, 31), 6), date(2026, 2, 28))

    def test_official_nikkei_csv_is_cash_close_not_open(self):
        content = 'データ日付,終値,始値,高値,安値\n"2026/10/21","64621.24","64885.95","65404.63","64558.60"\n'.encode("cp932")
        closes = official_nikkei_closes(Session([Response(content=content)]))
        self.assertEqual(closes[date(2026, 10, 21)], "64621.24")


if __name__ == "__main__":
    unittest.main()
