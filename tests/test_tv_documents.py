import importlib.util
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_tv_documents.py"
    spec = importlib.util.spec_from_file_location("load_tv_documents", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOC = load_module()


class DailySelection(unittest.TestCase):
    def test_new_document_is_fetched_and_old_one_is_left(self):
        documents = [
            {"id": "new", "reported": 1_000_000, "views": [{"id": "v1", "type": "transcript"}]},
            {"id": "old", "reported": 1, "views": [{"id": "late", "type": "summary"}]},
            {"id": "recent", "reported": 900_000, "views": [{"id": "have", "type": "transcript"}, {"id": "added", "type": "summary"}]},
        ]
        new_docs, extra = DOC.select_document_work(
            documents, {"old", "recent"}, {"have"}, 1_000_000, recent_seconds=200_000
        )
        self.assertEqual([item["id"] for item in new_docs], ["new"])
        self.assertEqual([(item["id"], view["id"]) for item, view in extra], [("recent", "added")])


class DocumentText(unittest.TestCase):
    def test_turns_the_transcript_tree_into_lines(self):
        node = {
            "type": "root",
            "children": [
                {"type": "p", "children": [{"type": "b", "children": ["Tim Cook"]}, ": Hello"]},
                {"type": "list", "children": [{"type": "*", "children": ["Revenue reached 109.4 billion"]}]},
            ],
        }
        text = DOC.ast_text(node)
        self.assertIn("Tim Cook: Hello", text)
        self.assertIn("- Revenue reached 109.4 billion", text)

    def test_depositary_receipt_matches_when_it_is_the_listing(self):
        rows = [
            {"symbol": "TSM", "source_id": "NYSE", "prefix": None, "type": "dr", "is_primary_listing": None},
            {"symbol": "TSM", "source_id": "TWSE", "prefix": None, "type": "stock", "is_primary_listing": True},
        ]
        self.assertEqual(DOC.match_listing(rows, "TSM", "NYSE"), "NYSE:TSM")
        self.assertEqual(DOC.match_listing(rows, "TSM", "TWSE"), "TWSE:TSM")
