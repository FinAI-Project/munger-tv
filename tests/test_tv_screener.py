import importlib.util
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_screener.py"
    spec = importlib.util.spec_from_file_location("load_tv_screener", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TV = load_module()


def sample_row(symbol, values):
    fields = {name: None for name in TV.SCAN_COLUMNS}
    fields.update(values)
    return {"s": symbol, "d": [fields[name] for name in TV.SCAN_COLUMNS]}


class ScreenerParse(unittest.TestCase):
    def test_payload_is_global_primary_common_stock(self):
        body = TV.screener_payload(0, 500)
        self.assertEqual(body["sort"]["sortBy"], "market_cap_basic")
        self.assertIn("america", body["markets"])
        self.assertIn("china", body["markets"])
        kinds = {item["left"]: item for item in body["filter"]}
        self.assertEqual(kinds["type"]["right"], "stock")
        self.assertTrue(kinds["is_primary"]["right"])
        self.assertNotIn("Value.Traded", kinds)

    def test_parse_maps_ticker_and_chinese_sector(self):
        raw = sample_row("NASDAQ:NVDA", {
            "name": "NVDA",
            "description": "NVIDIA Corporation",
            "close": 221.97,
            "change": 2.54,
            "volume": 87084022,
            "relative_volume_10d_calc": 0.88,
            "Value.Traded": 19330040363.34,
            "currency": "USD",
            "market": "america",
            "exchange": "NASDAQ",
            "country": "United States",
            "market_cap_basic": 5e12,
            "sector": "Electronic Technology",
            "sector.tr": "电子科技",
            "is_primary": True,
            "typespecs": ["common"],
        })
        row = TV.parse_row(raw)
        self.assertEqual(row["ticker"], "NVDA.US")
        self.assertEqual(row["bbg_ticker"], "NVDA US Equity")
        self.assertEqual(row["sector"], "电子科技")
        self.assertEqual(row["map_ok"], 1)
        self.assertEqual(row["name_zh"], "")

    def test_chinese_description_fills_name_zh(self):
        raw = sample_row("SSE:600519", {
            "name": "600519",
            "description": "贵州茅台",
            "market": "china",
            "exchange": "SSE",
            "is_primary": True,
        })
        row = TV.parse_row(raw)
        self.assertEqual(row["ticker"], "600519.CH")
        self.assertEqual(row["name_zh"], "贵州茅台")

    def test_unmapped_exchange_is_kept_but_not_ok(self):
        raw = sample_row("NSE:RELIANCE", {
            "name": "RELIANCE",
            "description": "Reliance Industries",
            "market": "india",
            "exchange": "NSE",
            "is_primary": True,
        })
        row = TV.parse_row(raw)
        self.assertFalse(row["map_ok"])
        self.assertEqual(row["ticker"], "RELIANCE.NSE")

    def test_known_table_wins(self):
        raw = sample_row("NASDAQ:INTC", {"description": "Intel", "exchange": "NASDAQ", "market": "america"})
        row = TV.parse_row(raw, {"NASDAQ:INTC": "INTC.US"})
        self.assertEqual(row["ticker"], "INTC.US")

    def test_fetch_pages(self):
        chunks = [
            {"totalCount": 2, "data": [sample_row("NASDAQ:A", {"description": "A", "exchange": "NASDAQ"})]},
            {"totalCount": 2, "data": [sample_row("NASDAQ:B", {"description": "B", "exchange": "NASDAQ"})]},
        ]

        def post(_url, payload):
            start = payload["range"][0]
            return chunks[0 if start == 0 else 1]

        rows, total = TV.fetch_screener(post, page_size=1)
        self.assertEqual(total, 2)
        self.assertEqual([item["s"] for item in rows], ["NASDAQ:A", "NASDAQ:B"])

    def test_munger_ready_needs_all_keys(self):
        self.assertFalse(TV.munger_ready({}))
        self.assertTrue(TV.munger_ready({
            "MUNGER_CH_HOST": "h",
            "MUNGER_CH_HTTP_PORT": "8123",
            "MUNGER_CH_DATABASE": "munger",
            "MUNGER_CH_USER": "u",
            "MUNGER_CH_PASSWORD": "p",
        }))

    def test_enrich_prefers_universe_chinese_name(self):
        row = {
            "ticker": "NVDA.US",
            "name": "NVIDIA Corporation",
            "short_name": "NVDA",
            "name_zh": "",
        }
        filled = TV.enrich(row, {"NVDA.US": {"name": "NVIDIA CORP", "short_name": "NVIDIA CORP", "name_zh": "英伟达"}})
        self.assertEqual(filled["name_zh"], "英伟达")
        self.assertEqual(filled["name"], "NVIDIA CORP")


if __name__ == "__main__":
    unittest.main()
