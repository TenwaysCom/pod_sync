import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import phase1a_runs


class Phase1aRunDiscoveryTests(unittest.TestCase):
    def test_discover_phase1a_runs_sorts_latest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_root = tmp / "out"
            older = out_root / "older"
            newer = out_root / "newer"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)

            (older / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T01:00:00Z", "document_count": 2, "artifacts": {}}),
                encoding="utf-8",
            )
            (newer / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T03:00:00Z", "document_count": 3, "artifacts": {}}),
                encoding="utf-8",
            )

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                runs = phase1a_runs.discover_phase1a_runs(limit=10)

            self.assertEqual([item["run_name"] for item in runs], ["newer", "older"])

    def test_resolve_phase1a_summary_path_uses_config_relative_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_dir = tmp / "configs"
            output_dir = tmp / "phase1a-out" / "demo"
            config_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            summary_path = output_dir / "phase1a_summary.json"
            summary_path.write_text(json.dumps({"document_count": 1}), encoding="utf-8")

            config_path = config_dir / "phase1a.json"
            config_path.write_text(json.dumps({"output_dir": "../phase1a-out/demo"}), encoding="utf-8")

            resolved = phase1a_runs.resolve_phase1a_summary_path(config_path)

            self.assertEqual(resolved, summary_path)

    def test_resolve_phase1a_summary_path_uses_latest_auto_run_dir_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_dir = tmp / "configs"
            output_root = tmp / "phase1a-out"
            older = output_root / "202604270001"
            newer = output_root / "202604270002"
            config_dir.mkdir(parents=True)
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            (older / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T01:00:00Z", "document_count": 1}),
                encoding="utf-8",
            )
            (newer / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T03:00:00Z", "document_count": 2}),
                encoding="utf-8",
            )

            config_path = config_dir / "phase1a.json"
            config_path.write_text(
                json.dumps({"output_dir": "../phase1a-out", "auto_run_dir": True}),
                encoding="utf-8",
            )

            resolved = phase1a_runs.resolve_phase1a_summary_path(config_path)

            self.assertEqual(resolved, newer / "phase1a_summary.json")

    def test_delete_phase1a_run_removes_run_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_root = tmp / "out"
            run_dir = out_root / "202604270001"
            run_dir.mkdir(parents=True)
            (run_dir / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T03:00:00Z", "document_count": 3}),
                encoding="utf-8",
            )

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                ok, error = phase1a_runs.delete_phase1a_run("202604270001")

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertFalse(run_dir.exists())

    def test_build_phase1a_document_listing_supports_pagination_and_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_root = tmp / "out"
            run_dir = out_root / "202604270999"
            storage_root = tmp / "storage" / "pod"
            (storage_root / "EMTC").mkdir(parents=True)
            run_dir.mkdir(parents=True)

            (run_dir / "phase1a_summary.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-27T03:00:00Z",
                        "document_count": 3,
                        "artifacts": {
                            "matched_report_json": str((run_dir / "pod_backfill_matched_report.json").resolve()),
                            "unmatched_report_json": str((run_dir / "pod_backfill_unmatched_report.json").resolve()),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "pod_backfill_matched_report.json").write_text(
                json.dumps(
                    [
                        {
                            "document_uid": "doc-1",
                            "file_name": "2026-03-19_3SYZXG8293874.png",
                            "source_name": "EMTC",
                            "source_type": "gdrive",
                            "source_path": "2026-03-19_3SYZXG8293874.png",
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "status": "ready",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "3SYZXG8293874",
                            "primary_tracking_number": "3SYZXG8293874",
                            "file_url": "https://example.test/1",
                            "local_path": str((storage_root / "EMTC" / "2026-03-19_3SYZXG8293874.png").resolve()),
                        },
                        {
                            "document_uid": "doc-2",
                            "file_name": "EU22114.pdf",
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "source_path": "CMC/2024 POD/EU22114.pdf",
                            "source_modified_at": "2026-04-27T01:00:00Z",
                            "status": "ready",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22114",
                            "primary_picking_name": "WCB2BOUT22114",
                            "file_url": "https://example.test/2",
                            "local_path": str((storage_root / "CMC" / "EU22114.pdf").resolve()),
                        },
                    ]
                ),
                encoding="utf-8",
            )
            (run_dir / "pod_backfill_unmatched_report.json").write_text(
                json.dumps(
                    [
                        {
                            "document_uid": "doc-3",
                            "file_name": "EU-22246-47.pdf",
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "source_path": "CMC/2024 POD/EU-22246-47.pdf",
                            "source_modified_at": "2026-04-27T00:00:00Z",
                            "status": "needs_review",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22246",
                            "reason_code": "MULTIPLE_PICKING_CANDIDATES",
                            "file_url": "https://example.test/3",
                            "local_path": str((storage_root / "CMC" / "EU-22246-47.pdf").resolve()),
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                with mock.patch.object(phase1a_runs, "resolve_storage_dir", return_value=storage_root):
                    payload = phase1a_runs.build_phase1a_document_listing(
                        "202604270999",
                        query="eu",
                        status="ready",
                        page=1,
                        page_size=1,
                    )

            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["total_pages"], 1)
            self.assertEqual(payload["items"][0]["document_uid"], "doc-2")
            self.assertEqual(payload["items"][0]["local_file_href"], "/storage/files/CMC/EU22114.pdf")
            self.assertEqual(payload["source_options"][1]["label"], "CMC / OneDrive")

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                with mock.patch.object(phase1a_runs, "resolve_storage_dir", return_value=storage_root):
                    review_payload = phase1a_runs.build_phase1a_document_listing(
                        "202604270999",
                        status="needs_review",
                        page=1,
                        page_size=10,
                    )

            self.assertEqual(review_payload["items"][0]["reason_label"], "命中多个候选出库单")
            self.assertEqual(review_payload["reason_options"][1]["label"], "命中多个候选出库单")

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                with mock.patch.object(phase1a_runs, "resolve_storage_dir", return_value=storage_root):
                    all_payload = phase1a_runs.build_phase1a_document_listing(
                        "202604270999",
                        page=1,
                        page_size=10,
                    )

            self.assertEqual(all_payload["items"][0]["document_uid"], "doc-3")
            self.assertEqual(all_payload["status_counts"]["ready"], 2)
            self.assertEqual(all_payload["status_counts"]["needs_review"], 1)
            self.assertEqual(all_payload["reason_counts"]["MULTIPLE_PICKING_CANDIDATES"], 1)


if __name__ == "__main__":
    unittest.main()
