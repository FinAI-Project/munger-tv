import importlib.util
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_financials.py"
    spec = importlib.util.spec_from_file_location("load_tv_financials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TV = load_module()


class SymbolMatch(unittest.TestCase):
    def test_picks_the_listing_on_the_expected_exchange(self):
        rows = [
            {"symbol": "AZN", "source_id": "NYSE", "prefix": None, "type": "stock", "is_primary_listing": True},
            {"symbol": "AZN", "source_id": "LSE", "prefix": None, "type": "stock", "is_primary_listing": True},
        ]
        self.assertEqual(TV.match_symbol(rows, "AZN", "LSE"), "LSE:AZN")

    def test_skips_a_same_code_stock_on_another_exchange(self):
        rows = [
            {"symbol": "3105", "source_id": "TSE", "prefix": None, "type": "stock", "is_primary_listing": True},
            {"symbol": "3105", "source_id": "TPEX", "prefix": None, "type": "stock", "is_primary_listing": True},
        ]
        self.assertEqual(TV.match_symbol(rows, "3105", "TPEX"), "TPEX:3105")
        self.assertIsNone(TV.match_symbol(rows, "3105", "TWSE"))

    def test_accepts_depositary_receipt_on_the_expected_exchange(self):
        rows = [
            {"symbol": "ASML", "source_id": "NASDAQ", "prefix": None, "type": "dr"},
            {"symbol": "ASML", "source_id": "EURONEXT", "prefix": "EURONEXT", "type": "stock", "is_primary_listing": True},
        ]
        self.assertEqual(TV.match_symbol(rows, "ASML", "NASDAQ"), "NASDAQ:ASML")
        self.assertEqual(TV.match_symbol(rows, "ASML", "EURONEXT"), "EURONEXT:ASML")

    def test_keeps_cash_and_short_term_investments(self):
        names = {item[0] for item in TV.ITEMS}
        self.assertIn("cash_n_short_term_invest", names)
        self.assertIn("short_term_debt", names)

    def test_slash_code_has_a_dot_candidate(self):
        self.assertEqual(TV.search_codes("BRK/B"), ["BRK/B", "BRK.B"])
        self.assertEqual(TV.ticker_code("BRK/B.US"), ("BRK/B", "US"))


class QuoteCompletion(unittest.TestCase):
    def test_ignores_the_previous_symbols_completion(self):
        message = {"m": "quote_completed", "p": ["qs_old", "NASDAQ:AAPL"]}
        self.assertFalse(TV.completion_for_us(message, "qs_new"))

    def test_accepts_completion_for_this_session(self):
        message = {"m": "quote_completed", "p": ["qs_new", "NASDAQ:MU"]}
        self.assertTrue(TV.completion_for_us(message, "qs_new"))

    def test_empty_payload_is_not_a_statement(self):
        self.assertFalse(TV.has_statement({}))
        self.assertTrue(TV.has_statement({"total_revenue_fy_h": [133188000000]}))


class Changes(unittest.TestCase):
    def test_same_value_is_not_written_again(self):
        rows = [{"item_id": "total_revenue_fy", "period_label": "2025", "value": 416161000000.0}]
        existing = {("total_revenue_fy", "2025"): 416161000000.0}
        self.assertEqual(TV.changed_lines(existing, rows), [])

    def test_new_period_is_written(self):
        rows = [{"item_id": "net_income_fq", "period_label": "2026-Q3", "value": 29789000000.0}]
        self.assertEqual(len(TV.changed_lines({}, rows)), 1)

    def test_probe_hash_ignores_tiny_float_noise(self):
        left = {"total_revenue_fy_h": [416161000000.0001], "dividend_amount_h": [0.27000001]}
        right = {"total_revenue_fy_h": [416161000000.0], "dividend_amount_h": [0.27]}
        self.assertEqual(TV.probe_hash(left), TV.probe_hash(right))

    def test_cookie_header_keeps_tradingview_session(self):
        header = TV.cookie_header(
            [
                {"name": "sessionid", "value": "abc", "domain": ".tradingview.com"},
                {"name": "noise", "value": "1", "domain": "example.com"},
            ]
        )
        self.assertEqual(header, "sessionid=abc")
        self.assertTrue(TV.cookies_usable([{"name": "sessionid", "value": "abc", "domain": ".tradingview.com", "expires": -1}]))
        self.assertFalse(TV.cookies_usable([{"name": "other", "value": "abc", "domain": ".tradingview.com"}]))


if __name__ == "__main__":
    unittest.main()
