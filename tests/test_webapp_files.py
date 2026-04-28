import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.file_browser import build_storage_listing, resolve_storage_file


class WebAppFileBrowserTests(unittest.TestCase):
    def test_build_storage_listing_excludes_internal_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            (root / "_integrity").mkdir(parents=True)
            (root / "CMC" / "proof-001.pdf").write_text("pdf", encoding="utf-8")
            (root / "_integrity" / "onedrive.jsonl").write_text("{}", encoding="utf-8")
            (root / ".gitkeep").write_text("", encoding="utf-8")

            payload = build_storage_listing(root)

            self.assertTrue(payload["exists"])
            self.assertEqual(payload["total_files"], 1)
            self.assertEqual(payload["items"][0]["relative_path"], "CMC/proof-001.pdf")

    def test_build_storage_listing_supports_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            (root / "EMTC").mkdir(parents=True)
            (root / "CMC" / "EU22910.pdf").write_text("a", encoding="utf-8")
            (root / "EMTC" / "OTHER.png").write_text("b", encoding="utf-8")

            payload = build_storage_listing(root, source="CMC", query="22910")

            self.assertEqual(payload["total_files"], 1)
            self.assertEqual(payload["items"][0]["source_key"], "CMC")

    def test_build_storage_listing_supports_pagination(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            for index in range(30):
                (root / "CMC" / f"file-{index:02d}.txt").write_text(str(index), encoding="utf-8")

            payload = build_storage_listing(root, page=2, page_size=10)

            self.assertEqual(payload["total_files"], 30)
            self.assertEqual(payload["page"], 2)
            self.assertEqual(payload["page_size"], 10)
            self.assertEqual(payload["total_pages"], 3)
            self.assertEqual(payload["page_from"], 11)
            self.assertEqual(payload["page_to"], 20)
            self.assertEqual(len(payload["items"]), 10)

    def test_resolve_storage_file_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            root.mkdir(parents=True)
            (root / "safe.txt").write_text("ok", encoding="utf-8")
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("nope", encoding="utf-8")

            safe = resolve_storage_file(root, "safe.txt")
            blocked = resolve_storage_file(root, "../outside.txt")

            self.assertEqual(safe, (root / "safe.txt").resolve())
            self.assertIsNone(blocked)


if __name__ == "__main__":
    unittest.main()
