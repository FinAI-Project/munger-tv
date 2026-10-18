import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_overview.py"
    spec = importlib.util.spec_from_file_location("load_tv_overview", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OV = load_module()


class OverviewRow(unittest.TestCase):
    def test_keeps_intro_and_same_day_valuation(self):
        ts = datetime(2026, 10, 16, 23, 59, 51, tzinfo=timezone.utc).timestamp()
        earn = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc).timestamp()
        row = OV.overview_row(
            "AAPL.US",
            "NASDAQ:AAPL",
            {
                "description": "Apple Inc.",
                "short_name": "AAPL",
                "business_description": "Apple, Inc. engages in the design of smartphones.",
                "sector": "Electronic Technology",
                "industry": "Telecommunications Equipment",
                "ceo": "John Ternus",
                "founded": 1976,
                "number_of_employees": 166000,
                "listed_exchange": "NASDAQ",
                "currency_code": "USD",
                "lp": 343.46,
                "lp_time": ts,
                "market_cap_basic": 5012516830763,
                "total_shares_outstanding": 14594200000,
                "price_earnings_ttm": 39.2,
                "book_value_per_share_fq": 7.36,
                "dividends_yield": 0.314,
                "dividend_payout_ratio_fy": 13.66,
                "beta_5_year": 1.069,
                "figi": {"country-composite": "BBG000B9XRY4"},
                "earnings_release_next_date": earn,
            },
            "2026-10-17 00:00:00.000",
        )
        self.assertEqual(row["as_of_date"], "2026-10-16")
        self.assertEqual(row["name"], "Apple Inc.")
        self.assertIn("smartphones", row["business_description"])
        self.assertEqual(row["close"], 343.46)
        self.assertEqual(row["shares_outstanding"], 14594200000)
        self.assertEqual(row["book_value_per_share"], 7.36)
        self.assertEqual(row["dividend_yield"], 0.314)
        self.assertEqual(row["payout_ratio"], 13.66)
        self.assertEqual(row["ceo"], "John Ternus")
        self.assertEqual(row["figi"], "BBG000B9XRY4")
        self.assertEqual(row["next_earnings_at"], "2026-11-02")
        self.assertEqual(row["founded"], 1976)
        self.assertEqual(row["employees"], 166000)

    def test_skips_a_completely_empty_payload(self):
        self.assertIsNone(OV.overview_row("AAPL.US", "NASDAQ:AAPL", {}, "2026-10-17 00:00:00.000"))

    def test_keeps_intro_when_price_is_missing(self):
        row = OV.overview_row(
            "AAPL.US",
            "NASDAQ:AAPL",
            {"description": "Apple Inc.", "business_description": "Makes iPhones."},
            "2026-10-17 11:00:00.000",
        )
        self.assertEqual(row["as_of_date"], "2026-10-17")
        self.assertEqual(row["name"], "Apple Inc.")
        self.assertIsNone(row["close"])


if __name__ == "__main__":
    unittest.main()
