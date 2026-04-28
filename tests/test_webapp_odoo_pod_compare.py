import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_pod_compare


class OdooPodCompareTests(unittest.TestCase):
    def test_build_snapshot_compares_latest_pod_identifiers_against_odoo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            matched_path = tmp / "matched.json"
            matched_path.write_text(
                json.dumps(
                    [
                        {
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "file_name": "EU22114.pdf",
                            "source_path": "CMC/2024 POD/EU22114.pdf",
                            "file_url": "https://example.test/cmc/EU22114.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "2024 POD" / "EU22114.pdf").resolve()),
                            "source_modified_at": "2026-04-27T01:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22114",
                        },
                        {
                            "source_name": "EMTC",
                            "source_type": "gdrive",
                            "file_name": "2026-03-19_3SYZXG8293874.png",
                            "source_path": "EMTC/2026-03-19_3SYZXG8293874.png",
                            "file_url": "https://example.test/emtc/3SYZXG8293874.png",
                            "local_path": str((tmp / "storage" / "EMTC" / "2026-03-19_3SYZXG8293874.png").resolve()),
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "3SYZXG8293874",
                        },
                        {
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "file_name": "3040_0180006140972_006.pdf",
                            "source_path": "CMC/2025 POD/3040_0180006140972_006.pdf",
                            "file_url": "https://example.test/cmc/3040_0180006140972_006.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "2025 POD" / "3040_0180006140972_006.pdf").resolve()),
                            "source_modified_at": "2026-04-27T03:00:00Z",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "0180006140972",
                        },
                        {
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "file_name": "POD-WCB2BOUT16079.png",
                            "source_path": "CMC/2024 POD/POD-WCB2BOUT16079.png",
                            "file_url": "https://example.test/cmc/POD-WCB2BOUT16079.png",
                            "local_path": str((tmp / "storage" / "CMC" / "2024 POD" / "POD-WCB2BOUT16079.png").resolve()),
                            "source_modified_at": "2026-04-27T03:10:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT16079",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            run = {
                "run_name": "202604270999",
                "summary": {
                    "generated_at": "2026-04-27T03:30:00Z",
                    "ready_document_count": 3,
                    "review_document_count": 1,
                },
            }
            odoo_snapshot = {
                "generated_at": "2026-04-27T04:00:00Z",
                "totals": {"total_done_outgoing": 10, "without_tracking_number": 2},
                "total_detail_rows": [
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 101,
                        "picking_name": "WCB2B/OUT/22114",
                        "state": "done",
                        "business_datetime": "2026-04-20 10:00:00",
                        "carrier_tracking_ref": None,
                    },
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 201,
                        "picking_name": "WCB2B/OUT/30000",
                        "state": "done",
                        "business_datetime": "2026-04-21 10:00:00",
                        "carrier_tracking_ref": "0180006140972",
                    },
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 202,
                        "picking_name": "WCB2B/OUT/30001",
                        "state": "done",
                        "business_datetime": "2026-04-21 11:00:00",
                        "carrier_tracking_ref": "0180006140972",
                    },
                    {
                        "carrier_id": 24,
                        "carrier_name": "EMTC",
                        "odoo_picking_id": 301,
                        "picking_name": "WRB2B/OUT/99999",
                        "state": "cancel",
                        "business_datetime": "2026-04-22 09:00:00",
                        "carrier_tracking_ref": "3SYZXG8293874",
                    },
                    {
                        "carrier_id": 88,
                        "carrier_name": "Legacy Carrier",
                        "odoo_picking_id": 401,
                        "picking_name": "WCB2B/OUT/16079",
                        "state": "done",
                        "business_datetime": "2026-04-22 11:00:00",
                        "carrier_tracking_ref": None,
                    },
                ],
            }

            with mock.patch.object(odoo_pod_compare, "get_latest_phase1a_run", return_value=run), \
                mock.patch.object(odoo_pod_compare, "resolve_artifact_path", return_value=matched_path), \
                mock.patch.object(odoo_pod_compare, "load_odoo_missing_snapshot", return_value=odoo_snapshot), \
                mock.patch.object(odoo_pod_compare, "resolve_storage_dir", return_value=(tmp / "storage")):
                snapshot = odoo_pod_compare.build_pod_odoo_compare_snapshot()

            self.assertEqual(snapshot["totals"]["unique_identifier_count"], 4)
            self.assertEqual(snapshot["totals"]["matched_count"], 1)
            self.assertEqual(snapshot["totals"]["matched_cancelled_count"], 1)
            self.assertEqual(snapshot["totals"]["matched_other_carrier_count"], 1)
            self.assertEqual(snapshot["totals"]["not_found_count"], 0)
            self.assertEqual(snapshot["totals"]["ambiguous_count"], 1)
            self.assertEqual(snapshot["totals"]["match_rate_percent"], 75.0)
            self.assertEqual(snapshot["summary_rows"][0]["source_name"], "EMTC")
            matched_cancelled = next(row for row in snapshot["matched_cancelled_rows"] if row["source_name"] == "EMTC")
            matched = next(row for row in snapshot["matched_rows"] if row["source_name"] == "CMC")
            matched_other_carrier = next(row for row in snapshot["matched_other_carrier_rows"] if row["source_name"] == "CMC")
            self.assertEqual(matched_cancelled["preferred_key_display"], "3SYZXG8293874")
            self.assertEqual(matched_cancelled["matched_odoo_states"], "cancel")
            self.assertEqual(matched["preferred_key_display"], "WCB2B/OUT/22114")
            self.assertEqual(matched["source_file_path"], "CMC/2024 POD/EU22114.pdf")
            self.assertEqual(matched["sample_documents"][0]["local_file_href"], "/storage/files/CMC/2024%20POD/EU22114.pdf")
            self.assertEqual(matched_other_carrier["matched_odoo_carrier_names"], "Legacy Carrier")
            self.assertEqual(matched_other_carrier["matched_odoo_states"], "done")

    def test_build_listing_filters_by_status(self):
        snapshot = {
            "generated_at": "2026-04-27T12:00:00Z",
            "matched_rows": [
                {
                    "compare_status": "matched",
                    "source_name": "CMC",
                    "preferred_key": "WCB2BOUT22114",
                    "preferred_key_display": "WCB2B/OUT/22114",
                    "matched_odoo_count": 1,
                }
            ],
            "matched_cancelled_rows": [
                {
                    "compare_status": "matched_cancelled",
                    "source_name": "EMTC",
                    "preferred_key": "3SYZXG8293874",
                    "preferred_key_display": "3SYZXG8293874",
                    "matched_odoo_count": 1,
                    "matched_odoo_states": "cancel",
                }
            ],
            "matched_other_carrier_rows": [
                {
                    "compare_status": "matched_other_carrier",
                    "source_name": "CMC",
                    "preferred_key": "WCB2BOUT16079",
                    "preferred_key_display": "WCB2B/OUT/16079",
                    "matched_odoo_count": 1,
                    "matched_odoo_carrier_names": "Legacy Carrier",
                }
            ],
            "not_found_rows": [
                {
                    "compare_status": "odoo_not_found",
                    "source_name": "EMTC",
                    "preferred_key": "3SYZXG8293874",
                    "preferred_key_display": "3SYZXG8293874",
                    "matched_odoo_count": 0,
                }
            ],
            "ambiguous_rows": [
                {
                    "compare_status": "multiple_odoo_matches",
                    "source_name": "CMC",
                    "preferred_key": "0180006140972",
                    "preferred_key_display": "0180006140972",
                    "matched_odoo_count": 2,
                }
            ],
        }

        payload = odoo_pod_compare.build_pod_odoo_compare_listing(
            query="cancel",
            status="matched_cancelled",
            page=1,
            page_size=10,
            snapshot=snapshot,
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["source_name"], "EMTC")
        self.assertEqual(payload["status_options"][1]["label"], "按匹配键未命中 Odoo")
        self.assertEqual(payload["status_options"][4]["label"], "匹配到已取消单")
        self.assertEqual(payload["status_options"][5]["label"], "命中其他承运商")

        matched_any_payload = odoo_pod_compare.build_pod_odoo_compare_listing(
            status="matched_any",
            page=1,
            page_size=10,
            snapshot=snapshot,
        )
        self.assertEqual(matched_any_payload["total"], 3)

    def test_save_and_resolve_artifacts(self):
        snapshot = {
            "generated_at": "2026-04-27T12:00:00Z",
            "phase1a": {"run_name": "202604270999", "generated_at": "2026-04-27T03:30:00Z"},
            "odoo": {"generated_at": "2026-04-27T04:00:00Z"},
            "totals": {"unique_identifier_count": 3, "matched_count": 1, "matched_cancelled_count": 1, "matched_other_carrier_count": 1, "not_found_count": 0, "ambiguous_count": 0, "match_rate_percent": 100.0, "review_document_count": 0, "matched_odoo_picking_count": 3},
            "summary_rows": [
                {
                    "source_name": "CMC",
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "unique_identifier_count": 2,
                    "matched_count": 1,
                    "matched_cancelled_count": 0,
                    "matched_other_carrier_count": 1,
                    "not_found_count": 0,
                    "ambiguous_count": 0,
                    "match_rate_percent": 100.0,
                }
            ],
            "matched_rows": [
                {
                    "compare_status": "matched",
                    "source_name": "CMC",
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WCB2BOUT22114",
                    "preferred_key_display": "WCB2B/OUT/22114",
                    "document_count": 1,
                    "latest_source_modified_at": "2026-04-27T01:00:00Z",
                    "sample_file_names": "EU22114.pdf",
                    "sample_source_paths": "CMC/2024 POD/EU22114.pdf",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_ids": "101",
                    "matched_odoo_picking_names": "WCB2B/OUT/22114",
                    "matched_odoo_tracking_numbers": "",
                    "matched_odoo_pod_links": "",
                    "matched_odoo_states": "done",
                }
            ],
            "matched_cancelled_rows": [
                {
                    "compare_status": "matched_cancelled",
                    "source_name": "EMTC",
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "3SYZXG8293874",
                    "preferred_key_display": "3SYZXG8293874",
                    "document_count": 1,
                    "latest_source_modified_at": "2026-04-27T02:00:00Z",
                    "sample_file_names": "2026-03-19_3SYZXG8293874.png",
                    "sample_source_paths": "EMTC/2026-03-19_3SYZXG8293874.png",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_ids": "301",
                    "matched_odoo_picking_names": "WRB2B/OUT/99999",
                    "matched_odoo_tracking_numbers": "3SYZXG8293874",
                    "matched_odoo_pod_links": "",
                    "matched_odoo_states": "cancel",
                }
            ],
            "matched_other_carrier_rows": [
                {
                    "compare_status": "matched_other_carrier",
                    "source_name": "CMC",
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WCB2BOUT16079",
                    "preferred_key_display": "WCB2B/OUT/16079",
                    "document_count": 1,
                    "latest_source_modified_at": "2026-04-27T04:00:00Z",
                    "sample_file_names": "POD-WCB2BOUT16079.png",
                    "sample_source_paths": "CMC/2024 POD/POD-WCB2BOUT16079.png",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_ids": "401",
                    "matched_odoo_picking_names": "WCB2B/OUT/16079",
                    "matched_odoo_carrier_names": "Legacy Carrier",
                    "matched_odoo_tracking_numbers": "",
                    "matched_odoo_pod_links": "",
                    "matched_odoo_states": "done",
                }
            ],
            "not_found_rows": [],
            "ambiguous_rows": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            with mock.patch.object(odoo_pod_compare, "RUNTIME_DIR", runtime_dir), \
                mock.patch.object(odoo_pod_compare, "SNAPSHOT_PATH", runtime_dir / "latest_snapshot.json"), \
                mock.patch.object(odoo_pod_compare, "SUMMARY_CSV_PATH", runtime_dir / "summary.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_CSV_PATH", runtime_dir / "matched.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_CANCELLED_CSV_PATH", runtime_dir / "matched_cancelled.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_OTHER_CARRIER_CSV_PATH", runtime_dir / "matched_other_carrier.csv"), \
                mock.patch.object(odoo_pod_compare, "NOT_FOUND_CSV_PATH", runtime_dir / "not_found.csv"), \
                mock.patch.object(odoo_pod_compare, "AMBIGUOUS_CSV_PATH", runtime_dir / "ambiguous.csv"), \
                mock.patch.object(odoo_pod_compare, "REPORT_MD_PATH", runtime_dir / "report.md"):
                odoo_pod_compare.save_pod_odoo_compare_snapshot(snapshot)
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("summary_csv"))
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("matched_csv"))
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("matched_cancelled_csv"))
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("matched_other_carrier_csv"))
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("snapshot_json"))
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("report_md"))

    def test_refresh_creates_push_ready_excel_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            matched_path = tmp / "matched.json"
            matched_path.write_text(
                json.dumps(
                    [
                        {
                            "source_name": "CMC",
                            "source_type": "onedrive",
                            "file_name": "EU22114.pdf",
                            "source_path": "CMC/2024 POD/EU22114.pdf",
                            "file_url": "https://example.test/cmc/EU22114.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "2024 POD" / "EU22114.pdf").resolve()),
                            "source_modified_at": "2026-04-27T01:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22114",
                        },
                        {
                            "source_name": "EMTC",
                            "source_type": "gdrive",
                            "file_name": "2026-03-19_3SYZXG8293874.png",
                            "source_path": "EMTC/2026-03-19_3SYZXG8293874.png",
                            "file_url": "https://example.test/emtc/3SYZXG8293874.png",
                            "local_path": str((tmp / "storage" / "EMTC" / "2026-03-19_3SYZXG8293874.png").resolve()),
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "3SYZXG8293874",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            run = {
                "run_name": "202604270999",
                "summary": {
                    "generated_at": "2026-04-27T03:30:00Z",
                    "ready_document_count": 2,
                    "review_document_count": 0,
                },
            }
            odoo_snapshot = {
                "generated_at": "2026-04-27T04:00:00Z",
                "totals": {"total_done_outgoing": 10, "without_tracking_number": 2},
                "total_detail_rows": [
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 101,
                        "picking_name": "WCB2B/OUT/22114",
                        "state": "done",
                        "business_datetime": "2026-04-20 10:00:00",
                        "x_studio_pod_link": None,
                        "carrier_tracking_ref": None,
                    },
                    {
                        "carrier_id": 24,
                        "carrier_name": "EMTC",
                        "odoo_picking_id": 301,
                        "picking_name": "WRB2B/OUT/99999",
                        "state": "done",
                        "business_datetime": "2026-04-22 09:00:00",
                        "x_studio_pod_link": "https://already-linked.example/pod.png",
                        "carrier_tracking_ref": "3SYZXG8293874",
                    },
                ],
            }

            runtime_dir = tmp / "runtime"
            push_xlsx_path = runtime_dir / "pod_ready_for_odoo.xlsx"
            with mock.patch.object(odoo_pod_compare, "RUNTIME_DIR", runtime_dir), \
                mock.patch.object(odoo_pod_compare, "SNAPSHOT_PATH", runtime_dir / "latest_snapshot.json"), \
                mock.patch.object(odoo_pod_compare, "SUMMARY_CSV_PATH", runtime_dir / "summary.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_CSV_PATH", runtime_dir / "matched.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_CANCELLED_CSV_PATH", runtime_dir / "matched_cancelled.csv"), \
                mock.patch.object(odoo_pod_compare, "MATCHED_OTHER_CARRIER_CSV_PATH", runtime_dir / "matched_other_carrier.csv"), \
                mock.patch.object(odoo_pod_compare, "NOT_FOUND_CSV_PATH", runtime_dir / "not_found.csv"), \
                mock.patch.object(odoo_pod_compare, "AMBIGUOUS_CSV_PATH", runtime_dir / "ambiguous.csv"), \
                mock.patch.object(odoo_pod_compare, "REPORT_MD_PATH", runtime_dir / "report.md"), \
                mock.patch.object(odoo_pod_compare, "PUSH_READY_XLSX_PATH", push_xlsx_path), \
                mock.patch.object(odoo_pod_compare, "get_latest_phase1a_run", return_value=run), \
                mock.patch.object(odoo_pod_compare, "resolve_artifact_path", return_value=matched_path), \
                mock.patch.object(odoo_pod_compare, "load_odoo_missing_snapshot", return_value=odoo_snapshot), \
                mock.patch.object(odoo_pod_compare, "resolve_storage_dir", return_value=(tmp / "storage")):
                snapshot = odoo_pod_compare.refresh_pod_odoo_compare_snapshot()
                self.assertIsNotNone(odoo_pod_compare.resolve_pod_odoo_compare_artifact("push_ready_xlsx"))

            self.assertEqual(snapshot["totals"]["matched_count"], 2)
            self.assertTrue(push_xlsx_path.exists())
            workbook_bytes = push_xlsx_path.read_bytes()
            self.assertTrue(workbook_bytes.startswith(b"PK"))
            with zipfile.ZipFile(push_xlsx_path) as archive:
                sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("odoo_picking_id", sheet_xml)
            self.assertIn("/storage/files/CMC/2024%20POD/EU22114.pdf", sheet_xml)
            self.assertIn("101", sheet_xml)
            self.assertNotIn("301", sheet_xml)


if __name__ == "__main__":
    unittest.main()
