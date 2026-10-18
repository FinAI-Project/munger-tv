import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_quotes.py"
    spec = importlib.util.spec_from_file_location("load_tv_quotes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Q = load_module()


class BarRows(unittest.TestCase):
    def test_uses_utc_date_from_the_session_open(self):
        ts = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(Q.bar_date(ts), "2026-10-16")
        row = Q.bar_row("INTC.US", "NASDAQ:INTC", [ts, 93.48, 94.22, 89.19, 89.85, 123003084], "2026-10-17 00:00:00.000")
        self.assertEqual(row["as_of_date"], "2026-10-16")
        self.assertEqual(row["close"], 89.85)
        self.assertEqual(row["volume"], 123003084)

    def test_skips_a_bar_without_close(self):
        self.assertIsNone(Q.bar_row("INTC.US", "NASDAQ:INTC", [1792195200, 1, 2, 3, None], "t"))

    def test_only_writes_new_or_changed_days(self):
        rows = [
            {"as_of_date": "2026-10-16", "close": 89.85, "volume": 10.0},
            {"as_of_date": "2026-10-15", "close": 95.37, "volume": 8.0},
        ]
        existing = {"2026-10-15": (95.37, 8.0)}
        changed = Q.changed_bars(existing, rows)
        self.assertEqual([row["as_of_date"] for row in changed], ["2026-10-16"])


class Snapshot(unittest.TestCase):
    def test_keeps_close_shares_and_cap_on_the_same_day(self):
        ts = datetime(2026, 10, 16, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        row = Q.snapshot_row(
            "INTC.US",
            "NASDAQ:INTC",
            {
                "lp": 89.85,
                "lp_time": ts,
                "market_cap_basic": 474956568301,
                "total_shares_outstanding": 5286110000,
                "float_shares_outstanding": 4546847516.5,
                "volume": 123003084,
                "currency_code": "USD",
            },
            "2026-10-15",
            "2026-10-17 00:00:00.000",
        )
        self.assertEqual(row["as_of_date"], "2026-10-16")
        self.assertEqual(row["close"], 89.85)
        self.assertEqual(row["shares_outstanding"], 5286110000)
        self.assertEqual(row["market_cap"], 474956568301)
        self.assertEqual(row["currency"], "USD")

    def test_falls_back_to_the_last_bar_date(self):
        row = Q.snapshot_row("INTC.US", "NASDAQ:INTC", {"lp": 89.85}, "2026-10-16", "t")
        self.assertEqual(row["as_of_date"], "2026-10-16")
        self.assertIsNone(Q.snapshot_row("INTC.US", "NASDAQ:INTC", {}, "2026-10-16", "t"))


if __name__ == "__main__":
    unittest.main()
