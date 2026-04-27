import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from phase1a_pod import parsing, pipeline


class Phase1APipelineTests(unittest.TestCase):
    def test_extracts_multiple_eu_candidates(self):
        candidates = parsing.extract_picking_candidates("EU22080-82-83.pdf")
        self.assertEqual(candidates, ["EU22080", "EU22082", "EU22083"])

    def test_extracts_whout_and_tracking(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "x1",
                "source_path": "Tenways-CMC POD/2024 POD/WHOUT15504/WHOUT15504-1.jpg",
                "file_name": "WHOUT15504-1.jpg",
                "source_url": "https://example.test/file.jpg",
            }
        )
        self.assertEqual(doc["inferred_picking_names"], ["WHOUT15504"])
        self.assertEqual(doc["inferred_tracking_numbers"], [])

    def test_extracts_tracking_after_underscore(self):
        doc = parsing.infer_document_fields(
            {
                "source": "gdrive",
                "warehouse": "EMTC",
                "source_file_id": "e1",
                "source_path": "2026-02-02_3SHOHR465528827.png",
                "file_name": "2026-02-02_3SHOHR465528827.png",
                "source_url": "https://example.test/file.png",
            }
        )
        self.assertEqual(doc["inferred_tracking_numbers"], ["3SHOHR465528827"])

    def test_run_phase1a_outputs_reports_and_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            emtc_manifest_path = tmp / "emtc.jsonl"
            manifest_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "source": "onedrive",
                                "warehouse": "CMC",
                                "source_file_id": "cmc-1",
                                "source_path": "Tenways-CMC POD/2024 POD/EU22910.png",
                                "file_name": "EU22910.png",
                                "source_modified_time": "2026-04-20T01:02:03Z",
                                "source_url": "https://example.test/cmc/EU22910.png",
                                "mime_type": "image/png",
                            }
                        ),
                        json.dumps(
                            {
                                "source": "onedrive",
                                "warehouse": "CMC",
                                "source_file_id": "cmc-2",
                                "source_path": "Tenways-CMC POD/2024 POD/EU22080-82-83.pdf",
                                "file_name": "EU22080-82-83.pdf",
                                "source_modified_time": "2026-04-20T01:03:03Z",
                                "source_url": "https://example.test/cmc/EU22080-82-83.pdf",
                                "mime_type": "application/pdf",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            emtc_manifest_path.write_text(
                json.dumps(
                    {
                        "source": "gdrive",
                        "warehouse": "EMTC",
                        "source_file_id": "emtc-1",
                        "source_path": "2026-02-02_3SHOHR465528827.png",
                        "file_name": "2026-02-02_3SHOHR465528827.png",
                        "source_modified_time": "2026-04-21T08:09:10Z",
                        "source_url": "https://example.test/emtc/3SHOHR465528827.png",
                        "mime_type": "image/png",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            scope_path = tmp / "scope.json"
            scope_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "odoo_picking_id": 101,
                                "picking_name": "EU22910",
                                "tracking_numbers": [],
                                "eligible": True,
                                "in_scope": True,
                            },
                            {
                                "odoo_picking_id": 102,
                                "picking_name": "EMTC-POD-TRACKING",
                                "tracking_numbers": ["3SHOHR465528827"],
                                "eligible": True,
                                "in_scope": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-1",
                        "output_dir": str(tmp / "out"),
                        "odoo_scope_path": str(scope_path),
                        "sources": [
                            {
                                "manifest_path": str(manifest_path),
                                "source_name": "CMC",
                                "source_type": "onedrive",
                            },
                            {
                                "manifest_path": str(emtc_manifest_path),
                                "source_name": "EMTC",
                                "source_type": "gdrive",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = pipeline.run_phase1a(config_path)

            self.assertEqual(summary["matched_document_count"], 2)
            self.assertEqual(summary["unmatched_document_count"], 1)

            payload = json.loads((tmp / "out" / "odoo_backfill_payload.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["job_id"], "job-test-1")
            self.assertEqual(len(payload["items"]), 2)
            payload_by_name = {item["picking_name"]: item for item in payload["items"]}
            self.assertEqual(payload_by_name["EU22910"]["documents"][0]["document_uid"].startswith("pod:onedrive:CMC:"), True)
            self.assertEqual(payload_by_name["EMTC-POD-TRACKING"]["documents"][0]["tracking_number"], "3SHOHR465528827")

            unmatched_report = json.loads((tmp / "out" / "pod_backfill_unmatched_report.json").read_text(encoding="utf-8"))
            self.assertEqual(unmatched_report[0]["reason_code"], "NO_PICKING_FOUND")
            dashboard_data = json.loads((tmp / "out" / "phase1a_dashboard_data.json").read_text(encoding="utf-8"))
            dashboard_html = (tmp / "out" / "phase1a_dashboard.html").read_text(encoding="utf-8")
            self.assertEqual(dashboard_data["totals"]["matched_documents"], 2)
            self.assertEqual(dashboard_data["totals"]["unmatched_documents"], 1)
            self.assertIn("Inspection Deck", dashboard_html)
            self.assertIn("job-test-1", dashboard_html)
            embedded = re.search(
                r'<script id="dashboard-data" type="application/json">(.*?)</script>',
                dashboard_html,
                re.S,
            )
            self.assertIsNotNone(embedded)
            embedded_payload = json.loads(embedded.group(1))
            self.assertEqual(embedded_payload["job_id"], "job-test-1")

    def test_match_key_uses_actual_selected_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-9",
                        "source_path": "Tenways-CMC POD/2024 POD/EU22080-82-83.pdf",
                        "file_name": "EU22080-82-83.pdf",
                        "source_modified_time": "2026-04-20T01:03:03Z",
                        "source_url": "https://example.test/cmc/EU22080-82-83.pdf",
                        "mime_type": "application/pdf",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            scope_path = tmp / "scope.json"
            scope_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "odoo_picking_id": 202,
                                "picking_name": "EU22082",
                                "tracking_numbers": [],
                                "eligible": True,
                                "in_scope": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-2",
                        "output_dir": str(tmp / "out"),
                        "odoo_scope_path": str(scope_path),
                        "sources": [
                            {
                                "manifest_path": str(manifest_path),
                                "source_name": "CMC",
                                "source_type": "onedrive",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            pipeline.run_phase1a(config_path)
            matched_report = json.loads((tmp / "out" / "pod_backfill_matched_report.json").read_text(encoding="utf-8"))
            self.assertEqual(matched_report[0]["match_key"], "EU22082")

    def test_loads_sources_from_sync_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            base_dir = tmp / "pod"
            manifest_path = base_dir / "_integrity" / "CMC" / "onedrive.jsonl"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-1",
                        "source_path": "Tenways-CMC POD/2024 POD/EU22910.png",
                        "file_name": "EU22910.png",
                        "source_modified_time": "2026-04-20T01:02:03Z",
                        "source_url": "https://example.test/cmc/EU22910.png",
                        "mime_type": "image/png",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            sync_config_path = tmp / "sync_config.json"
            sync_config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": str(base_dir)},
                        "cmc_onedrive": {"enabled": True, "warehouse": "CMC"},
                        "emtc_gdrive": {"enabled": False, "warehouse": "EMTC"},
                    }
                ),
                encoding="utf-8",
            )

            scope_path = tmp / "scope.json"
            scope_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "odoo_picking_id": 301,
                                "picking_name": "EU22910",
                                "tracking_numbers": [],
                                "eligible": True,
                                "in_scope": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-3",
                        "output_dir": str(tmp / "out"),
                        "sync_config_path": str(sync_config_path),
                        "odoo_scope_path": str(scope_path),
                    }
                ),
                encoding="utf-8",
            )

            summary = pipeline.run_phase1a(config_path)
            self.assertEqual(summary["matched_document_count"], 1)
            self.assertEqual(summary["sync_config_path"], str(sync_config_path.resolve()))


if __name__ == "__main__":
    unittest.main()
