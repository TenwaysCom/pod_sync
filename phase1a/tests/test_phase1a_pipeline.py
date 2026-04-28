import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from phase1a_pod import parsing, pipeline


class Phase1APipelineTests(unittest.TestCase):
    def test_extracts_multiple_eu_candidates(self):
        candidates = parsing.extract_picking_candidates("EU22080-82-83.pdf")
        self.assertEqual(candidates, ["EU22080", "EU22082", "EU22083"])

    def test_treats_single_digit_eu_suffix_as_page_marker(self):
        candidates = parsing.extract_picking_candidates("EU21916-1.png")
        self.assertEqual(candidates, ["EU21916"])

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
                "source_path": "2026-03-19_3SYZXG8293874.png",
                "file_name": "2026-03-19_3SYZXG8293874.png",
                "source_url": "https://example.test/file.png",
            }
        )
        self.assertEqual(doc["inferred_tracking_numbers"], ["3SYZXG8293874"])

    def test_restores_cmc_eu_filename_to_wcb2bout_picking(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c1",
                "source_path": "CMC/2024 POD/EU22114.pdf",
                "file_name": "EU22114.pdf",
                "source_url": "https://example.test/file.pdf",
            }
        )
        self.assertEqual(doc["inferred_picking_names"][0], "WCB2BOUT22114")
        self.assertIn("EU22114", doc["inferred_picking_names"])

    def test_restores_cmc_hk_filename_to_wcb2bout_picking(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c2",
                "source_path": "CMC/2025 POD/HK23898.pdf",
                "file_name": "HK23898.pdf",
                "source_url": "https://example.test/file.pdf",
            }
        )
        self.assertEqual(doc["inferred_picking_names"][0], "WCB2BOUT23898")
        self.assertIn("HK23898", doc["inferred_picking_names"])

    def test_extracts_postal_tracking_with_suffix_letters(self):
        doc = parsing.infer_document_fields(
            {
                "source": "gdrive",
                "warehouse": "EMTC",
                "source_file_id": "e2",
                "source_path": "2026-02-04_LA857793468NL.png",
                "file_name": "2026-02-04_LA857793468NL.png",
                "source_url": "https://example.test/file.png",
            }
        )
        self.assertEqual(doc["inferred_tracking_numbers"], ["LA857793468NL"])

    def test_extracts_cf_postal_tracking_with_suffix_letters(self):
        doc = parsing.infer_document_fields(
            {
                "source": "gdrive",
                "warehouse": "EMTC",
                "source_file_id": "e3",
                "source_path": "2026-02-02_CF438625735NL.png",
                "file_name": "2026-02-02_CF438625735NL.png",
                "source_url": "https://example.test/file.png",
            }
        )
        self.assertEqual(doc["inferred_tracking_numbers"], ["CF438625735NL"])

    def test_extracts_embedded_wcb2bout_as_primary_picking_candidate(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c3",
                "source_path": "CMC/2024 POD/POD-JVGL06204739000635595024(WCB2BOUT20655).png",
                "file_name": "POD-JVGL06204739000635595024(WCB2BOUT20655).png",
                "source_url": "https://example.test/file.png",
            }
        )
        self.assertIn("WCB2BOUT20655", doc["inferred_picking_names"])

    def test_extracts_wcb2cout_as_picking_candidate(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c4",
                "source_path": "CMC/2025 POD/WCB2C-OUT-23898.jpg",
                "file_name": "WCB2C-OUT-23898.jpg",
                "source_url": "https://example.test/file.jpg",
            }
        )
        self.assertIn("WCB2COUT23898", doc["inferred_picking_names"])

    def test_extracts_wcb2cout_with_numeric_prefix_as_picking_candidate(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c5",
                "source_path": "CMC/2025 POD/Photos/9WCB2C-OUT-24104.jpg",
                "file_name": "9WCB2C-OUT-24104.jpg",
                "source_url": "https://example.test/file.jpg",
            }
        )
        self.assertIn("WCB2COUT24104", doc["inferred_picking_names"])

    def test_restores_cmc_numeric_filename_to_wcb2bout_picking(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c6",
                "source_path": "CMC/2025 POD/23735.pdf",
                "file_name": "23735.pdf",
                "source_url": "https://example.test/file.pdf",
            }
        )
        self.assertEqual(doc["inferred_picking_names"], ["WCB2BOUT23735"])

    def test_restores_cmc_numeric_page_filename_to_single_picking(self):
        doc = parsing.infer_document_fields(
            {
                "source": "onedrive",
                "warehouse": "CMC",
                "source_file_id": "c7",
                "source_path": "CMC/2025 POD/23735-2.pdf",
                "file_name": "23735-2.pdf",
                "source_url": "https://example.test/file.pdf",
            }
        )
        self.assertEqual(doc["inferred_picking_names"], ["WCB2BOUT23735"])

    def test_run_phase1a_parse_only_treats_hk_and_restored_wcb2bout_as_single_picking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-hk-1",
                        "source_path": "CMC/2025 POD/HK23898.pdf",
                        "file_name": "HK23898.pdf",
                        "source_modified_time": "2026-04-27T09:00:00Z",
                        "source_url": "https://example.test/cmc/HK23898.pdf",
                        "mime_type": "application/pdf",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-hk-parse-only",
                        "processing_mode": "parse_only",
                        "output_dir": str(tmp / "out"),
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

            summary = pipeline.run_phase1a(config_path)
            ready_report = json.loads((tmp / "out" / "pod_backfill_matched_report.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["ready_document_count"], 1)
            self.assertEqual(summary["review_document_count"], 0)
            self.assertEqual(ready_report[0]["primary_picking_name"], "WCB2BOUT23898")

    def test_run_phase1a_parse_only_splits_multi_eu_file_into_multiple_ready_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-eu-split-1",
                        "source_path": "CMC/2025 POD/EU-22246-47.pdf",
                        "file_name": "EU-22246-47.pdf",
                        "source_modified_time": "2026-04-27T09:00:00Z",
                        "source_url": "https://example.test/cmc/EU-22246-47.pdf",
                        "mime_type": "application/pdf",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-eu-split-parse-only",
                        "processing_mode": "parse_only",
                        "output_dir": str(tmp / "out"),
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

            summary = pipeline.run_phase1a(config_path)
            ready_report = json.loads((tmp / "out" / "pod_backfill_matched_report.json").read_text(encoding="utf-8"))
            review_report = json.loads((tmp / "out" / "pod_backfill_unmatched_report.json").read_text(encoding="utf-8"))
            payload = json.loads((tmp / "out" / "odoo_backfill_payload.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["document_count"], 1)
            self.assertEqual(summary["ready_document_count"], 2)
            self.assertEqual(summary["review_document_count"], 0)
            self.assertEqual(summary["payload_item_count"], 2)
            self.assertEqual(review_report, [])
            self.assertEqual(
                [row["primary_picking_name"] for row in ready_report],
                ["WCB2BOUT22246", "WCB2BOUT22247"],
            )
            self.assertEqual(
                [item["primary_picking_name"] for item in payload["items"]],
                ["WCB2BOUT22246", "WCB2BOUT22247"],
            )

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
                                "picking_name": "WCB2B/OUT/22910",
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
            self.assertEqual(payload_by_name["WCB2B/OUT/22910"]["documents"][0]["document_uid"].startswith("pod:onedrive:CMC:"), True)
            self.assertEqual(payload_by_name["EMTC-POD-TRACKING"]["documents"][0]["tracking_number"], "3SHOHR465528827")

            unmatched_report = json.loads((tmp / "out" / "pod_backfill_unmatched_report.json").read_text(encoding="utf-8"))
            self.assertEqual(unmatched_report[0]["reason_code"], "NO_PICKING_FOUND")
            dashboard_data = json.loads((tmp / "out" / "phase1a_dashboard_data.json").read_text(encoding="utf-8"))
            dashboard_html = (tmp / "out" / "phase1a_dashboard.html").read_text(encoding="utf-8")
            self.assertEqual(dashboard_data["run_name"], summary["run_name"])
            self.assertEqual(dashboard_data["totals"]["matched_documents"], 2)
            self.assertEqual(dashboard_data["totals"]["unmatched_documents"], 1)
            self.assertIn("Phase 1A 巡检看板", dashboard_html)
            self.assertIn("job-test-1", dashboard_html)
            self.assertIn('id="pageSizeSelect"', dashboard_html)
            self.assertIn('id="documentPrevPage"', dashboard_html)
            self.assertIn('id="documentNextPage"', dashboard_html)
            self.assertIn('id="documentTableMeta"', dashboard_html)
            self.assertIn("/phase1a/runs/${encodeURIComponent(payload.run_name)}/artifacts/${encodeURIComponent(item.key)}", dashboard_html)
            embedded = re.search(
                r'<script id="dashboard-data" type="application/json">(.*?)</script>',
                dashboard_html,
                re.S,
            )
            self.assertIsNotNone(embedded)
            embedded_payload = json.loads(embedded.group(1))
            self.assertEqual(embedded_payload["job_id"], "job-test-1")

    def test_run_phase1a_parse_only_outputs_ready_payload_without_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            emtc_manifest_path = tmp / "emtc.jsonl"
            review_manifest_path = tmp / "review.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-parse-1",
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
            emtc_manifest_path.write_text(
                json.dumps(
                    {
                        "source": "gdrive",
                        "warehouse": "EMTC",
                        "source_file_id": "emtc-parse-1",
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
            review_manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-review-1",
                        "source_path": "Tenways-CMC POD/2024 POD/no-key.pdf",
                        "file_name": "proof-no-key.pdf",
                        "source_modified_time": "2026-04-22T01:02:03Z",
                        "source_url": "https://example.test/cmc/no-key.pdf",
                        "mime_type": "application/pdf",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-test-parse-only",
                        "processing_mode": "parse_only",
                        "output_dir": str(tmp / "out"),
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
                            },
                            {
                                "manifest_path": str(review_manifest_path),
                                "source_name": "CMC",
                                "source_type": "onedrive",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = pipeline.run_phase1a(config_path)

            self.assertEqual(summary["processing_mode"], "parse_only")
            self.assertEqual(summary["ready_document_count"], 2)
            self.assertEqual(summary["review_document_count"], 1)
            self.assertEqual(summary["payload_item_count"], 2)
            self.assertIsNone(summary["odoo_scope_path"])

            payload = json.loads((tmp / "out" / "odoo_backfill_payload.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["processing_mode"], "parse_only")
            self.assertEqual(len(payload["items"]), 2)
            payload_by_source = {item["source_type"]: item for item in payload["items"]}
            self.assertEqual(payload_by_source["onedrive"]["primary_picking_name"], "WCB2BOUT22910")
            self.assertEqual(payload_by_source["gdrive"]["primary_tracking_number"], "3SHOHR465528827")

            ready_report = json.loads((tmp / "out" / "pod_backfill_matched_report.json").read_text(encoding="utf-8"))
            self.assertEqual(ready_report[0]["status"], "ready")
            review_report = json.loads((tmp / "out" / "pod_backfill_unmatched_report.json").read_text(encoding="utf-8"))
            self.assertEqual(review_report[0]["reason_code"], "NO_IDENTIFIER_FOUND")
            dashboard_data = json.loads((tmp / "out" / "phase1a_dashboard_data.json").read_text(encoding="utf-8"))
            self.assertEqual(dashboard_data["run_name"], summary["run_name"])
            self.assertEqual(dashboard_data["processing_mode"], "parse_only")
            self.assertEqual(dashboard_data["totals"]["matched_documents"], 2)
            self.assertEqual(dashboard_data["totals"]["unmatched_documents"], 1)

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

    def test_auto_run_dir_generates_daily_sequence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source": "onedrive",
                        "warehouse": "CMC",
                        "source_file_id": "cmc-1",
                        "source_path": "CMC/2024 POD/EU22114.pdf",
                        "file_name": "EU22114.pdf",
                        "source_modified_time": "2026-04-20T01:02:03Z",
                        "source_url": "https://example.test/cmc/EU22114.pdf",
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
                                "odoo_picking_id": 501,
                                "picking_name": "WCB2B/OUT/22114",
                                "tracking_numbers": [],
                                "eligible": True,
                                "in_scope": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            output_root = tmp / "out"
            (output_root / "202604270001").mkdir(parents=True)

            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "output_dir": str(output_root),
                        "auto_run_dir": True,
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

            fake_now = datetime.fromisoformat("2026-04-27T14:30:00+08:00")
            with mock.patch.object(pipeline, "datetime") as mock_datetime:
                mock_datetime.now.return_value = fake_now
                summary = pipeline.run_phase1a(config_path)

            self.assertEqual(summary["run_name"], "202604270002")
            self.assertEqual(Path(summary["output_dir"]).name, "202604270002")
            self.assertEqual(summary["job_id"], "pod-recognition-202604270002")

    def test_dedupes_duplicate_manifest_records_before_matching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manifest_path = tmp / "cmc.jsonl"
            manifest_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "source": "onedrive",
                                "warehouse": "CMC",
                                "source_file_id": "cmc-dup-1",
                                "source_path": "CMC/2024 POD/EU22114.pdf",
                                "file_name": "EU22114.pdf",
                                "source_modified_time": "2026-04-20T01:02:03Z",
                                "downloaded_at": "2026-04-27T04:00:00Z",
                                "source_url": "https://example.test/cmc/EU22114.pdf",
                                "mime_type": "application/pdf",
                            }
                        ),
                        json.dumps(
                            {
                                "source": "onedrive",
                                "warehouse": "CMC",
                                "source_file_id": "cmc-dup-1",
                                "source_path": "CMC/2024 POD/EU22114.pdf",
                                "file_name": "EU22114.pdf",
                                "source_modified_time": "2026-04-20T01:02:03Z",
                                "downloaded_at": "2026-04-27T05:00:00Z",
                                "source_url": "https://example.test/cmc/EU22114.pdf",
                                "mime_type": "application/pdf",
                            }
                        ),
                    ]
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
                                "odoo_picking_id": 601,
                                "picking_name": "WCB2B/OUT/22114",
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

            summary = pipeline.run_phase1a(config_path)
            dashboard = json.loads((tmp / "out" / "phase1a_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["document_count"], 1)
            self.assertEqual(summary["matched_document_count"], 1)
            self.assertEqual(dashboard["document_count"], 1)


if __name__ == "__main__":
    unittest.main()
