import importlib.util
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


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


class NewsFilters(unittest.TestCase):
    def test_asks_english_and_simplified_chinese_separately(self):
        sets = NEWS.news_filter_sets("SZSE:300623")
        self.assertEqual(sets[0], ["symbol:SZSE:300623"])
        self.assertEqual(sets[1], ["lang:zh-Hans", "symbol:SZSE:300623"])
        self.assertEqual(sets[1], sorted(sets[1]))


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


class HttpJson(unittest.TestCase):
    def test_retries_connection_reset_then_succeeds(self):
        reset = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        payload = mock.Mock()
        payload.read.return_value = b'{"ok": true}'
        payload.__enter__ = mock.Mock(return_value=payload)
        payload.__exit__ = mock.Mock(return_value=False)
        with mock.patch("time.sleep"), mock.patch(
            "urllib.request.urlopen",
            side_effect=[reset, payload],
        ):
            self.assertEqual(NEWS.http_json("https://example.test/news"), {"ok": True})

    def test_rate_limit_stops_without_retrying(self):
        err = urllib.error.HTTPError("https://example.test/news", 429, "Too Many", hdrs=None, fp=None)
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", side_effect=err) as opener:
            with self.assertRaises(NEWS.StopRun):
                NEWS.http_json("https://example.test/news")
        self.assertEqual(opener.call_count, 1)

    def test_story_reset_keeps_headline(self):
        item = {"id": "n1", "title": "hello", "published": 0, "paywall": False}
        reset = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        with mock.patch.object(NEWS, "story_of", side_effect=reset), mock.patch("time.sleep"):
            row = NEWS.news_row("NVDA.US", "NASDAQ:NVDA", item, 0, "2026-10-26 00:00:00.000")
        self.assertEqual(row["title"], "hello")
        self.assertEqual(row["body_text"], "")
        self.assertEqual(row["content_status"], "empty")
