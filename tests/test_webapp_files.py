import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
import asyncio
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.file_browser import build_storage_listing, resolve_storage_file
from webapp.file_odoo_push import plan_storage_file_odoo_push, push_storage_file_to_odoo


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

    def test_build_storage_listing_keeps_source_options_when_source_filtered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            (root / "EMTC").mkdir(parents=True)
            (root / "CMC" / "cmc.pdf").write_text("a", encoding="utf-8")
            (root / "EMTC" / "emtc.png").write_text("b", encoding="utf-8")

            payload = build_storage_listing(root, source="CMC")

            self.assertEqual(payload["total_files"], 1)
            self.assertEqual(payload["items"][0]["source_key"], "CMC")
            self.assertEqual([item["key"] for item in payload["source_options"]], ["CMC", "EMTC"])

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

    def test_build_storage_listing_defaults_to_newest_stored_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            files = [
                (root / "CMC" / "old.pdf", 1000),
                (root / "CMC" / "middle.pdf", 2000),
                (root / "CMC" / "new.pdf", 3000),
            ]
            for path, stored_ts in files:
                path.write_text(path.name, encoding="utf-8")
                os.utime(path, (stored_ts, stored_ts))

            payload = build_storage_listing(root)

            self.assertEqual(payload["default_sort"], "stored_at_desc")
            self.assertEqual(
                [item["relative_path"] for item in payload["items"]],
                ["CMC/new.pdf", "CMC/middle.pdf", "CMC/old.pdf"],
            )
            self.assertEqual(payload["items"][0]["stored_at"], payload["items"][0]["modified_at"])
            self.assertEqual(payload["items"][0]["stored_ts"], payload["items"][0]["modified_ts"])

    def test_build_storage_listing_filters_by_stored_date_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            (root / "CMC").mkdir(parents=True)
            (root / "EMTC").mkdir(parents=True)
            files = [
                (root / "CMC" / "aug.pdf", datetime(2026, 8, 31, 23, 0, 0)),
                (root / "CMC" / "sep-start.pdf", datetime(2026, 9, 1, 0, 30, 0)),
                (root / "CMC" / "sep-mid.pdf", datetime(2026, 9, 10, 12, 0, 0)),
                (root / "EMTC" / "sep-end.png", datetime(2026, 9, 15, 23, 59, 0)),
                (root / "CMC" / "oct.pdf", datetime(2026, 10, 1, 0, 0, 0)),
            ]
            for path, stored_at in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
                ts = stored_at.timestamp()
                os.utime(path, (ts, ts))

            payload = build_storage_listing(
                root, date_from="2026-09-01", date_to="2026-09-15"
            )

            self.assertEqual(payload["date_from"], "2026-09-01")
            self.assertEqual(payload["date_to"], "2026-09-15")
            self.assertEqual(
                [item["relative_path"] for item in payload["items"]],
                ["EMTC/sep-end.png", "CMC/sep-mid.pdf", "CMC/sep-start.pdf"],
            )
            self.assertEqual(
                [item["key"] for item in payload["source_options"]],
                ["CMC", "EMTC"],
            )
            self.assertEqual(
                {item["key"]: item["count"] for item in payload["source_options"]},
                {"CMC": 2, "EMTC": 1},
            )

            from_only = build_storage_listing(root, date_from="2026-10-01")
            self.assertEqual(
                [item["relative_path"] for item in from_only["items"]],
                ["CMC/oct.pdf"],
            )

            to_only = build_storage_listing(root, date_to="2026-08-31")
            self.assertEqual(
                [item["relative_path"] for item in to_only["items"]],
                ["CMC/aug.pdf"],
            )

            ignored = build_storage_listing(
                root, date_from="not-a-date", date_to="2026-13-40"
            )
            self.assertEqual(ignored["total_files"], 5)
            self.assertEqual(ignored["date_from"], "")
            self.assertEqual(ignored["date_to"], "")

            source_filtered = build_storage_listing(
                root, source="CMC", date_from="2026-09-01", date_to="2026-09-15"
            )
            self.assertEqual(
                [item["relative_path"] for item in source_filtered["items"]],
                ["CMC/sep-mid.pdf", "CMC/sep-start.pdf"],
            )
            self.assertEqual(
                {item["key"]: item["count"] for item in source_filtered["source_options"]},
                {"CMC": 2, "EMTC": 1},
            )

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


class WebAppFileOdooPushTests(unittest.TestCase):
    def _write_pod(self, root: Path, relative_path: str) -> Path:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return path

    def test_listing_marks_direct_push_eligibility(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            self._write_pod(root, "CMC/EU34564.pdf")
            self._write_pod(root, "Westerman/WDB2BOUT01391.pdf")
            payload = build_storage_listing(root, page_size=50)
            by_path = {item["relative_path"]: item for item in payload["items"]}

            self.assertTrue(by_path["CMC/EU34564.pdf"]["odoo_push_allowed"])
            self.assertEqual(by_path["CMC/EU34564.pdf"]["odoo_push_blocked_reason"], "")
            self.assertFalse(by_path["Westerman/WDB2BOUT01391.pdf"]["odoo_push_allowed"])
            self.assertIn("待拉取池", by_path["Westerman/WDB2BOUT01391.pdf"]["odoo_push_blocked_reason"])

    def test_plan_cmc_shorthand_file_uses_picking_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            self._write_pod(root, "CMC/2026 POD/EU34564.pdf")
            plan = plan_storage_file_odoo_push("CMC/2026 POD/EU34564.pdf", storage_root=root)

        self.assertTrue(plan["ok"])
        self.assertEqual(
            plan["targets"],
            [{"match_key_type": "picking_name", "match_key": "WCB2BOUT34564"}],
        )

    def test_plan_emtc_tracking_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            self._write_pod(root, "EMTC/2026-03-19_3SYZXG8293874.png")
            plan = plan_storage_file_odoo_push(
                "EMTC/2026-03-19_3SYZXG8293874.png", storage_root=root
            )

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["targets"][0]["match_key_type"], "tracking_number")
        self.assertEqual(plan["targets"][0]["match_key"], "3SYZXG8293874")

    def test_plan_rejects_westerman_and_unparsed_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            self._write_pod(root, "Westerman/proof.pdf")
            self._write_pod(root, "CMC/readme.txt")

            blocked = plan_storage_file_odoo_push("Westerman/proof.pdf", storage_root=root)
            unparsed = plan_storage_file_odoo_push("CMC/readme.txt", storage_root=root)
            missing = plan_storage_file_odoo_push("CMC/missing.pdf", storage_root=root)

        self.assertEqual(blocked["error_code"], "WESTERMAN_SINGLE_PUSH_REQUIRES_POOL_PAYLOAD")
        self.assertEqual(unparsed["error_code"], "NO_IDENTIFIER_FOUND")
        self.assertEqual(missing["error_code"], "FILE_NOT_FOUND")

    def test_push_storage_file_calls_single_item_push(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage" / "pod"
            local = self._write_pod(root, "CMC/EU34564.pdf")
            with mock.patch(
                "webapp.file_odoo_push.push_single_item",
                return_value={
                    "ok": True,
                    "status": "pushed_tracking",
                    "match_key": "WCB2BOUT34564",
                    "match_key_type": "picking_name",
                },
            ) as push_mock:
                result = push_storage_file_to_odoo(
                    "CMC/EU34564.pdf",
                    config_path="config.local.json",
                    storage_root=root,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "pushed_tracking")
        push_mock.assert_called_once_with(
            config_path="config.local.json",
            match_key="WCB2BOUT34564",
            match_key_type="picking_name",
            file_url="",
            local_path=str(local.resolve()),
            source_name="CMC",
            dry_run=False,
        )

    def test_files_page_exposes_direct_odoo_push_control(self):
        template = (PROJECT_ROOT / "webapp" / "templates" / "files.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("api_files_push_odoo", template)
        self.assertIn("推送到 Odoo", template)
        self.assertIn("file-push-odoo", template)
        self.assertIn("fileDateFrom", template)
        self.assertIn("fileDateTo", template)
        self.assertIn("date_from", template)
        self.assertIn("date_to", template)

    def test_files_push_endpoint_writes_through_helper(self):
        from webapp import app as app_module

        payload = {
            "ok": True,
            "status": "pushed_tracking",
            "message": "已写入 Odoo stock.tracking",
        }
        with mock.patch.object(
            app_module,
            "push_storage_file_to_odoo",
            return_value=payload,
        ) as push_mock:
            response = asyncio.run(
                app_module.api_files_push_odoo(relative_path="CMC/EU34564.pdf")
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        push_mock.assert_called_once()
        self.assertEqual(push_mock.call_args.args[0], "CMC/EU34564.pdf")


if __name__ == "__main__":
    unittest.main()
