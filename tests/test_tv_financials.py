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
        self.assertEqual(TV.search_codes("RR/"), ["RR/", "RR.", "RR"])


class Listings(unittest.TestCase):
    def test_inferred_exchange_from_ticker_suffix(self):
        self.assertEqual(TV.inferred_exchanges("2628.HK"), ["HKG"])
        self.assertEqual(TV.inferred_exchanges("600031.CH"), ["SHH"])
        self.assertEqual(TV.inferred_exchanges("002138.CH"), ["SHZ"])
        self.assertEqual(TV.inferred_exchanges("RR/.LN"), ["LSE"])
        self.assertEqual(TV.inferred_exchanges("MOD.US")[0], "NMS")
        self.assertIn("NYQ", TV.inferred_exchanges("MOD.US"))

    def test_exchange_candidates_keep_profile_first(self):
        self.assertEqual(TV.exchange_candidates("MOD.US", "NYQ")[0], "NYQ")
        self.assertEqual(TV.exchange_candidates("2628.HK", ""), ["HKG"])

    def test_listings_reads_munger_universe(self):
        env = {
            "MUNGER_CH_HOST": "h",
            "MUNGER_CH_HTTP_PORT": "8123",
            "MUNGER_CH_DATABASE": "munger",
            "MUNGER_CH_USER": "u",
            "MUNGER_CH_PASSWORD": "p",
        }

        def fake_munger(_env, sql):
            if "FROM universe" in sql:
                return [{"ticker": "2628.HK"}, {"ticker": "002138.CH"}]
            if "FROM stock_profile" in sql:
                return [{"ticker": "2628.HK", "exchange": "HKG", "currency": "HKD"}]
            return []

        def fake_pangu(_env, sql):
            return [{"ticker": "AAPL.US", "exchange": "NMS", "currency": "USD"}]

        old_munger = TV.ch_query_munger
        old_pangu = TV.ch_query
        TV.ch_query_munger = fake_munger
        TV.ch_query = fake_pangu
        try:
            items = TV.listings(env)
        finally:
            TV.ch_query_munger = old_munger
            TV.ch_query = old_pangu
        self.assertEqual([item["ticker"] for item in items], ["2628.HK", "002138.CH"])
        by_ticker = {item["ticker"]: item for item in items}
        self.assertEqual(by_ticker["2628.HK"]["exchange"], "HKG")
        self.assertEqual(by_ticker["002138.CH"]["exchange"], "SHZ")

    def test_listings_need_munger_account(self):
        with self.assertRaises(TV.StopRun):
            TV.listings({})


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

    def test_keep_alive_does_not_fall_back_to_password(self):
        import inspect

        source = inspect.getsource(TV.login)
        self.assertIn("allow_password", source)
        self.assertIn("browser_refresh_token", source)
        self.assertIn("reuse_saved", source)

    def _jwt(self, exp_offset: float) -> str:
        import base64
        import json
        import time

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + exp_offset}).encode()).decode().rstrip("=")
        return "%s.%s.sig" % (header, payload)

    def test_extracts_live_token_from_homepage_html(self):
        token = self._jwt(3600)
        html = '<html class="is-authenticated is-pro"><script>window.user={"auth_token":"%s"}</script>' % token
        self.assertEqual(TV.extract_auth_token(html), token)

    def test_skips_expired_token_in_homepage_html(self):
        html = '{"auth_token":"%s"}' % self._jwt(-60)
        self.assertEqual(TV.extract_auth_token(html), "")

    def test_refresh_reads_homepage_not_retired_account_api(self):
        import inspect

        source = inspect.getsource(TV.refresh_token)
        self.assertIn("HOME_URL", source)
        self.assertNotIn("accounts/current", source)
        keep_alive = (Path(__file__).resolve().parents[1] / "scripts" / "tv_keep_alive.py").read_text(encoding="utf-8")
        self.assertIn("reuse_saved=False", keep_alive)
        self.assertIn("allow_password=False", keep_alive)


if __name__ == "__main__":
    unittest.main()
