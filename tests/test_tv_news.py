import importlib.util
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_news.py"
    spec = importlib.util.spec_from_file_location("load_tv_news", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NEWS = load_module()


class NewHeadlines(unittest.TestCase):
    def test_keeps_only_ids_not_in_the_database(self):
        headlines = [{"id": "a"}, {"id": "b"}, {"id": ""}]
        self.assertEqual(NEWS.new_headlines(headlines, {"a"}), [{"id": "b"}])


class RelatedSymbols(unittest.TestCase):
    def test_joins_unique_symbols(self):
        item = {
            "relatedSymbols": [
                {"symbol": "NASDAQ:NVDA"},
                {"symbol": "NASDAQ:NVDA"},
                {"symbol": "NASDAQ:AMD"},
            ]
        }
        self.assertEqual(NEWS.related_symbols(item), "NASDAQ:NVDA,NASDAQ:AMD")
