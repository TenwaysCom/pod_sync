import json
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import emtc_reconcile


class EmtcReconcileTests(unittest.TestCase):
    def setUp(self):
        emtc_reconcile._load_reconcile_dataset_cached.cache_clear()

    def test_load_dataset_builds_bidirectional_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed_path = tmp / "parsed_documents.json"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "document_uid": "pod-1",
                            "source_name": "EMTC",
                            "file_name": "2026-03-19_3SYZXG8293874.png",
                            "source_path": "EMTC/2026-03-19_3SYZXG8293874.png",
                            "file_url": "https://example.test/emtc/1.png",
                            "local_path": str((tmp / "storage" / "EMTC" / "2026-03-19_3SYZXG8293874.png").resolve()),
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "3SYZXG8293874",
                            "primary_tracking_number": "3SYZXG8293874",
                        },
                        {
                            "document_uid": "pod-2",
                            "source_name": "EMTC",
                            "file_name": "2026-03-20_3SYZXG0000001.png",
                            "source_path": "EMTC/2026-03-20_3SYZXG0000001.png",
                            "file_url": "https://example.test/emtc/2.png",
                            "local_path": str((tmp / "storage" / "EMTC" / "2026-03-20_3SYZXG0000001.png").resolve()),
                            "source_modified_at": "2026-04-27T03:00:00Z",
                            "preferred_key_type": "tracking_number",
                            "preferred_key": "3SYZXG0000001",
                            "primary_tracking_number": "3SYZXG0000001",
                        },
                        {
                            "document_uid": "pod-3",
                            "source_name": "EMTC",
                            "file_name": "bad_name.png",
                            "source_path": "EMTC/bad_name.png",
                            "file_url": "https://example.test/emtc/3.png",
                            "local_path": str((tmp / "storage" / "EMTC" / "bad_name.png").resolve()),
                            "source_modified_at": "2026-04-27T04:00:00Z",
                            "preferred_key_type": None,
                            "preferred_key": None,
                            "primary_tracking_number": None,
                        },
                        {
                            "document_uid": "pod-4",
                            "source_name": "CMC",
                            "file_name": "EU22114.pdf",
                            "source_path": "CMC/EU22114.pdf",
                            "file_url": "https://example.test/cmc/1.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "EU22114.pdf").resolve()),
                            "source_modified_at": "2026-04-27T05:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22114",
                            "primary_tracking_number": None,
                        },
                    ]
                ),
                encoding="utf-8",
            )

            run = {
                "run_name": "202604280001",
                "summary": {"generated_at": "2026-04-28T01:00:00Z"},
            }
            odoo_snapshot = {
                "generated_at": "2026-04-28T02:00:00Z",
                "total_detail_rows": [
                    {
                        "carrier_id": 24,
                        "carrier_name": "EMTC",
                        "odoo_picking_id": 101,
                        "picking_name": "WH/OUT/1",
                        "state": "done",
                        "business_datetime": "2026-04-20 10:00:00",
                        "tracking_number": "3SYZXG8293874",
                    },
                    {
                        "carrier_id": 24,
                        "carrier_name": "EMTC",
                        "odoo_picking_id": 102,
                        "picking_name": "WH/OUT/2",
                        "state": "done",
                        "business_datetime": "2026-04-20 11:00:00",
                        "tracking_number": "3SYZXG9999999",
                    },
                    {
                        "carrier_id": 24,
                        "carrier_name": "EMTC",
                        "odoo_picking_id": 103,
                        "picking_name": "WH/OUT/3",
                        "state": "cancel",
                        "business_datetime": "2026-04-20 12:00:00",
                        "tracking_number": None,
                    },
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 201,
                        "picking_name": "WCB2B/OUT/22114",
                        "state": "done",
                        "business_datetime": "2026-04-20 13:00:00",
                        "tracking_number": None,
                    },
                ],
            }

            with mock.patch.object(emtc_reconcile, "get_latest_phase1a_run", return_value=run), \
                mock.patch.object(emtc_reconcile, "resolve_artifact_path", return_value=parsed_path), \
                mock.patch.object(emtc_reconcile, "load_odoo_missing_snapshot", return_value=odoo_snapshot), \
                mock.patch.object(emtc_reconcile, "resolve_storage_dir", return_value=(tmp / "storage")):
                dataset = emtc_reconcile.load_reconcile_dataset("emtc")

            self.assertEqual(dataset["odoo_summary"]["total_count"], 3)
            self.assertEqual(dataset["odoo_summary"]["with_match_key_count"], 2)
            self.assertEqual(dataset["odoo_summary"]["matched_pod_count"], 1)
            self.assertEqual(dataset["odoo_summary"]["matched_pod_share_text"], "占Odoo 有匹配键单数 50.0%")
            self.assertEqual(dataset["pod_summary"]["total_count"], 3)
            self.assertEqual(dataset["pod_summary"]["parseable_count"], 2)
            self.assertEqual(dataset["pod_summary"]["found_in_odoo_count"], 1)
            self.assertEqual(dataset["pod_summary"]["found_in_odoo_share_text"], "占可解析 POD 文件 50.0%")
            found_row = next(row for row in dataset["pod_to_odoo_rows"] if row["compare_status"] == "found_in_odoo")
            self.assertEqual(found_row["sample_document"]["local_file_href"], "/storage/files/EMTC/2026-03-19_3SYZXG8293874.png")

    def test_load_dataset_supports_cmc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed_path = tmp / "parsed_documents.json"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "document_uid": "cmc-1",
                            "source_name": "CMC",
                            "file_name": "EU22114.pdf",
                            "source_path": "CMC/EU22114.pdf",
                            "file_url": "https://example.test/cmc/1.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "EU22114.pdf").resolve()),
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT22114",
                            "primary_picking_name": "WCB2BOUT22114",
                        },
                        {
                            "document_uid": "cmc-2",
                            "source_name": "CMC",
                            "file_name": "bad.pdf",
                            "source_path": "CMC/bad.pdf",
                            "file_url": "https://example.test/cmc/2.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "bad.pdf").resolve()),
                            "source_modified_at": "2026-04-27T03:00:00Z",
                            "preferred_key_type": None,
                            "preferred_key": None,
                            "primary_picking_name": None,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            run = {"run_name": "202604280001", "summary": {"generated_at": "2026-04-28T01:00:00Z"}}
            odoo_snapshot = {
                "generated_at": "2026-04-28T02:00:00Z",
                "total_detail_rows": [
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 201,
                        "picking_name": "WCB2B/OUT/22114",
                        "state": "done",
                        "business_datetime": "2026-04-20 13:00:00",
                    },
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 202,
                        "picking_name": "WCB2B/OUT/22115",
                        "state": "done",
                        "business_datetime": "2026-04-20 14:00:00",
                    },
                ],
            }

            with mock.patch.object(emtc_reconcile, "get_latest_phase1a_run", return_value=run), \
                mock.patch.object(emtc_reconcile, "resolve_artifact_path", return_value=parsed_path), \
                mock.patch.object(emtc_reconcile, "load_odoo_missing_snapshot", return_value=odoo_snapshot), \
                mock.patch.object(emtc_reconcile, "resolve_storage_dir", return_value=(tmp / "storage")):
                dataset = emtc_reconcile.load_reconcile_dataset("cmc")

            self.assertEqual(dataset["source_config"]["slug"], "cmc")
            self.assertEqual(dataset["odoo_summary"]["total_count"], 2)
            self.assertEqual(dataset["odoo_summary"]["with_match_key_count"], 2)
            self.assertEqual(dataset["odoo_summary"]["matched_pod_count"], 1)
            self.assertEqual(dataset["pod_summary"]["total_count"], 2)
            self.assertEqual(dataset["pod_summary"]["parseable_count"], 1)
            self.assertEqual(dataset["pod_summary"]["found_in_odoo_count"], 1)

    def test_cmc_pod_to_odoo_uses_pod_index_and_other_carriers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            parsed_path = tmp / "parsed_documents.json"
            pod_index_path = tmp / "odoo_pod_index.json"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "document_uid": "direct",
                            "source_name": "CMC",
                            "file_name": "direct.pdf",
                            "source_path": "CMC/direct.pdf",
                            "file_url": "https://example.test/cmc/direct.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "direct.pdf").resolve()),
                            "source_modified_at": "2026-04-27T02:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2BOUT111",
                            "primary_picking_name": "WCB2BOUT111",
                        },
                        {
                            "document_uid": "index",
                            "source_name": "CMC",
                            "file_name": "index.pdf",
                            "source_path": "CMC/index.pdf",
                            "file_url": "https://example.test/cmc/index.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "index.pdf").resolve()),
                            "source_modified_at": "2026-04-27T03:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2COUT333",
                            "primary_picking_name": "WCB2COUT333",
                        },
                        {
                            "document_uid": "other",
                            "source_name": "CMC",
                            "file_name": "other.pdf",
                            "source_path": "CMC/other.pdf",
                            "file_url": "https://example.test/cmc/other.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "other.pdf").resolve()),
                            "source_modified_at": "2026-04-27T04:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2COUT222",
                            "primary_picking_name": "WCB2COUT222",
                        },
                        {
                            "document_uid": "missing",
                            "source_name": "CMC",
                            "file_name": "missing.pdf",
                            "source_path": "CMC/missing.pdf",
                            "file_url": "https://example.test/cmc/missing.pdf",
                            "local_path": str((tmp / "storage" / "CMC" / "missing.pdf").resolve()),
                            "source_modified_at": "2026-04-27T05:00:00Z",
                            "preferred_key_type": "picking_name",
                            "preferred_key": "WCB2COUT444",
                            "primary_picking_name": "WCB2COUT444",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            pod_index_path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-04-28T02:00:00Z",
                        "by_picking_name": {
                            "WCB2C/OUT/333": {
                                "tracking_number": "WCB2C/OUT/333",
                                "pod_link": "https://pod.example/WCB2COUT333.pdf",
                                "source_model": "querydeluxe",
                            }
                        },
                        "by_tracking_number": {},
                    }
                ),
                encoding="utf-8",
            )
            run = {"run_name": "202604280001", "summary": {"generated_at": "2026-04-28T01:00:00Z"}}
            odoo_snapshot = {
                "generated_at": "2026-04-28T02:00:00Z",
                "total_detail_rows": [
                    {
                        "carrier_id": 25,
                        "carrier_name": "CMC",
                        "odoo_picking_id": 201,
                        "picking_name": "WCB2B/OUT/111",
                        "state": "done",
                        "business_datetime": "2026-04-20 13:00:00",
                    },
                    {
                        "carrier_id": 15,
                        "carrier_name": "DHL",
                        "odoo_picking_id": 301,
                        "picking_name": "WCB2C/OUT/222",
                        "state": "done",
                        "business_datetime": "2026-04-20 14:00:00",
                    },
                ],
            }

            with mock.patch.object(emtc_reconcile, "get_latest_phase1a_run", return_value=run), \
                mock.patch.object(emtc_reconcile, "resolve_artifact_path", return_value=parsed_path), \
                mock.patch.object(emtc_reconcile, "load_odoo_missing_snapshot", return_value=odoo_snapshot), \
                mock.patch.object(emtc_reconcile, "resolve_storage_dir", return_value=(tmp / "storage")), \
                mock.patch.object(emtc_reconcile, "POD_INDEX_PATH", pod_index_path):
                dataset = emtc_reconcile.load_reconcile_dataset("cmc")

            rows = {row["document_uid"]: row for row in dataset["pod_to_odoo_rows"]}
            self.assertEqual(rows["direct"]["compare_status"], "found_in_odoo")
            self.assertEqual(rows["index"]["compare_status"], "found_in_odoo_pod_index")
            self.assertEqual(rows["index"]["odoo_pod_index_count"], 1)
            self.assertEqual(rows["other"]["compare_status"], "found_other_carrier")
            self.assertEqual(rows["missing"]["compare_status"], "not_found_in_odoo")
            self.assertEqual(dataset["pod_summary"]["found_in_odoo_count"], 3)
            self.assertEqual(dataset["pod_summary"]["not_found_in_odoo_count"], 1)

    def test_build_listings_filter_status(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["emtc"],
            "odoo_to_pod_rows": [
                {"picking_name": "A", "compare_status": "matched_pod", "compare_status_label": "命中 POD", "search_blob": "a", "pod_file_count": 1},
                {"picking_name": "B", "compare_status": "pod_not_found", "compare_status_label": "未命中 POD", "search_blob": "b", "pod_file_count": 0},
            ],
            "pod_to_odoo_rows": [
                {"file_name": "a.png", "compare_status": "found_in_odoo", "compare_status_label": "在 Odoo 中找到", "search_blob": "a", "matched_odoo_count": 1},
                {"file_name": "b.png", "compare_status": "no_tracking_number", "compare_status_label": "无法解析 tracking number", "search_blob": "b", "matched_odoo_count": 0},
            ],
        }

        with mock.patch.object(emtc_reconcile, "load_filtered_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_odoo_to_pod_listing(kind="emtc", status="pod_not_found", page=1, page_size=10)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["picking_name"], "B")
            pod_payload = emtc_reconcile.build_reconcile_pod_to_odoo_listing(kind="emtc", status="found_in_odoo", page=1, page_size=10)
            self.assertEqual(pod_payload["total"], 1)
            self.assertEqual(pod_payload["items"][0]["file_name"], "a.png")

    def test_apply_start_month_filter_updates_both_sides(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["emtc"],
            "_odoo_rows": [
                {"business_datetime": "2024-05-20 10:00:00", "match_key": "3SYZXG1", "normalized_match_key": "3SYZXG1", "odoo_picking_id": 1, "picking_name": "WH/OUT/1", "state": "done"},
                {"business_datetime": "2024-06-20 10:00:00", "match_key": "3SYZXG2", "normalized_match_key": "3SYZXG2", "odoo_picking_id": 2, "picking_name": "WH/OUT/2", "state": "done"},
                {"business_datetime": "2024-06-21 10:00:00", "match_key": None, "normalized_match_key": None, "odoo_picking_id": 3, "picking_name": "WH/OUT/3", "state": "cancel"},
            ],
            "_pod_docs": [
                {"source_modified_at": "2024-05-21T10:00:00Z", "match_key": "3SYZXG1", "normalized_match_key": "3SYZXG1", "document_uid": "pod-1", "file_name": "a.png", "source_path": "EMTC/a.png", "sample_document": {}, "reason_code": None},
                {"source_modified_at": "2024-06-21T10:00:00Z", "match_key": "3SYZXG9", "normalized_match_key": "3SYZXG9", "document_uid": "pod-2", "file_name": "b.png", "source_path": "EMTC/b.png", "sample_document": {}, "reason_code": None},
                {"source_modified_at": "2024-06-22T10:00:00Z", "match_key": None, "normalized_match_key": None, "document_uid": "pod-3", "file_name": "c.png", "source_path": "EMTC/c.png", "sample_document": {}, "reason_code": "NO_IDENTIFIER_FOUND"},
            ],
            "odoo_to_pod_rows": [
                {"business_datetime": "2024-05-20 10:00:00", "compare_status": "matched_pod"},
                {"business_datetime": "2024-06-20 10:00:00", "compare_status": "pod_not_found"},
                {"business_datetime": "2024-06-21 10:00:00", "compare_status": "no_match_key"},
            ],
            "pod_to_odoo_rows": [
                {"source_modified_at": "2024-05-21T10:00:00Z", "compare_status": "found_in_odoo"},
                {"source_modified_at": "2024-06-21T10:00:00Z", "compare_status": "not_found_in_odoo"},
                {"source_modified_at": "2024-06-22T10:00:00Z", "compare_status": "no_match_key"},
            ],
        }

        filtered = emtc_reconcile._apply_start_month_filter(dataset, "2024-06")

        self.assertEqual(filtered["selected_start_month"], "2024-06")
        self.assertEqual(filtered["odoo_summary"]["total_count"], 2)
        self.assertEqual(filtered["odoo_summary"]["without_pod_count"], 1)
        self.assertEqual(filtered["pod_summary"]["total_count"], 3)
        self.assertEqual(filtered["pod_summary"]["not_found_in_odoo_count"], 2)
        self.assertEqual(filtered["pod_summary"]["found_in_odoo_count"], 0)
        self.assertEqual([item["value"] for item in filtered["start_month_options"]], ["2024-05", "2024-06"])

    def test_build_reconcile_export_workbook_contains_six_sheets(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["emtc"],
            "selected_start_month": "",
            "odoo_to_pod_rows": [
                {
                    "odoo_picking_id": 1,
                    "picking_name": "WH/OUT/1",
                    "state": "done",
                    "business_datetime": "2026-04-20 10:00:00",
                    "match_key": "3SYZXG1",
                    "compare_status": "matched_pod",
                    "pod_file_count": 1,
                    "sample_pod_files": "a.png",
                    "sample_pod_paths": "EMTC/a.png",
                    "sample_documents": [{"file_url": "https://example.test/a.png", "local_file_href": "/storage/files/EMTC/a.png"}],
                },
                {
                    "odoo_picking_id": 2,
                    "picking_name": "WH/OUT/2",
                    "state": "done",
                    "business_datetime": "2026-04-20 11:00:00",
                    "match_key": "3SYZXG2",
                    "compare_status": "pod_not_found",
                    "pod_file_count": 0,
                    "sample_pod_files": "",
                    "sample_pod_paths": "",
                    "sample_documents": [],
                },
                {
                    "odoo_picking_id": 3,
                    "picking_name": "WH/OUT/3",
                    "state": "cancel",
                    "business_datetime": "2026-04-20 12:00:00",
                    "match_key": None,
                    "compare_status": "no_match_key",
                    "pod_file_count": 0,
                    "sample_pod_files": "",
                    "sample_pod_paths": "",
                    "sample_documents": [],
                },
            ],
            "pod_to_odoo_rows": [
                {
                    "document_uid": "pod-1",
                    "file_name": "a.png",
                    "source_path": "EMTC/a.png",
                    "source_modified_at": "2026-04-27T02:00:00Z",
                    "match_key": "3SYZXG1",
                    "compare_status": "found_in_odoo",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_names": "WH/OUT/1",
                    "matched_odoo_states": "done",
                    "sample_document": {"file_url": "https://example.test/a.png", "local_file_href": "/storage/files/EMTC/a.png"},
                    "reason_code": None,
                },
                {
                    "document_uid": "pod-2",
                    "file_name": "b.png",
                    "source_path": "EMTC/b.png",
                    "source_modified_at": "2026-04-27T03:00:00Z",
                    "match_key": "3SYZXG9",
                    "compare_status": "not_found_in_odoo",
                    "matched_odoo_count": 0,
                    "matched_odoo_picking_names": "",
                    "matched_odoo_states": "",
                    "sample_document": {"file_url": "https://example.test/b.png", "local_file_href": "/storage/files/EMTC/b.png"},
                    "reason_code": None,
                },
                {
                    "document_uid": "pod-3",
                    "file_name": "c.png",
                    "source_path": "EMTC/c.png",
                    "source_modified_at": "2026-04-27T04:00:00Z",
                    "match_key": None,
                    "compare_status": "no_match_key",
                    "matched_odoo_count": 0,
                    "matched_odoo_picking_names": "",
                    "matched_odoo_states": "",
                    "sample_document": {"file_url": "https://example.test/c.png", "local_file_href": "/storage/files/EMTC/c.png"},
                    "reason_code": "NO_IDENTIFIER_FOUND",
                },
            ],
        }

        with mock.patch.object(emtc_reconcile, "load_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_export_workbook("emtc")

        self.assertTrue(payload["filename"].endswith(".xlsx"))
        self.assertEqual(len(payload["sheet_names"]), 6)
        self.assertEqual(
            payload["sheet_names"],
            [
                "Odoo 命中 POD",
                "Odoo 未命中 POD",
                "Odoo 没有 tracking number",
                "POD 可在 Odoo 找到",
                "POD 未命中 Odoo",
                "POD 解析不了匹配键",
            ],
        )
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("Odoo 命中 POD", workbook_xml)
            self.assertIn("POD 解析不了匹配键", workbook_xml)

    def test_build_reconcile_odoo_export_workbook_contains_first_three_sheets(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["emtc"],
            "selected_start_month": "2024-05",
            "odoo_to_pod_rows": [
                {
                    "odoo_picking_id": 1,
                    "picking_name": "WH/OUT/1",
                    "state": "done",
                    "business_datetime": "2026-04-20 10:00:00",
                    "match_key": "3SYZXG1",
                    "compare_status": "matched_pod",
                    "pod_file_count": 1,
                    "sample_pod_files": "a.png",
                    "sample_pod_paths": "EMTC/a.png",
                    "sample_documents": [{"file_url": "https://example.test/a.png", "local_file_href": "/storage/files/EMTC/a.png"}],
                },
            ],
            "pod_to_odoo_rows": [
                {
                    "document_uid": "pod-1",
                    "file_name": "a.png",
                    "source_path": "EMTC/a.png",
                    "source_modified_at": "2026-04-27T02:00:00Z",
                    "match_key": "3SYZXG1",
                    "compare_status": "found_in_odoo",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_names": "WH/OUT/1",
                    "matched_odoo_states": "done",
                    "sample_document": {"file_url": "https://example.test/a.png", "local_file_href": "/storage/files/EMTC/a.png"},
                    "reason_code": None,
                },
            ],
        }

        with mock.patch.object(emtc_reconcile, "load_filtered_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_odoo_export_workbook("emtc", "2024-05")

        self.assertIn("_odoo_to_pod_202405_", payload["filename"])
        self.assertEqual(payload["sheet_names"], ["Odoo 命中 POD", "Odoo 未命中 POD", "Odoo 没有 tracking number"])
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("Odoo 命中 POD", workbook_xml)
            self.assertIn("Odoo 没有 tracking number", workbook_xml)
            self.assertNotIn("POD 可在 Odoo 找到", workbook_xml)

    def test_build_reconcile_pod_export_workbook_contains_last_three_sheets(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["cmc"],
            "selected_start_month": "",
            "odoo_to_pod_rows": [],
            "pod_to_odoo_rows": [
                {
                    "document_uid": "pod-1",
                    "file_name": "a.png",
                    "source_path": "CMC/a.png",
                    "source_modified_at": "2026-04-27T02:00:00Z",
                    "match_key": "WCB2B/OUT/1",
                    "compare_status": "found_in_odoo",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_names": "WCB2B/OUT/1",
                    "matched_odoo_states": "done",
                    "sample_document": {"file_url": "https://example.test/a.png", "local_file_href": "/storage/files/CMC/a.png"},
                    "reason_code": None,
                },
            ],
        }

        with mock.patch.object(emtc_reconcile, "load_filtered_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_pod_export_workbook("cmc")

        self.assertIn("_pod_to_odoo_all_", payload["filename"])
        self.assertEqual(payload["sheet_names"], ["POD 可在 Odoo 找到", "POD 未命中 Odoo", "POD 解析不了匹配键"])
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("POD 可在 Odoo 找到", workbook_xml)
            self.assertIn("POD 解析不了匹配键", workbook_xml)
            self.assertNotIn("Odoo 命中 POD", workbook_xml)

    def test_build_reconcile_metric_export_workbook_for_odoo_with_key(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["emtc"],
            "selected_start_month": "2024-06",
            "odoo_to_pod_rows": [
                {
                    "odoo_picking_id": 1,
                    "picking_name": "WH/OUT/1",
                    "state": "done",
                    "business_datetime": "2026-04-20 10:00:00",
                    "match_key": "3SYZXG1",
                    "compare_status": "matched_pod",
                    "pod_file_count": 1,
                    "sample_pod_files": "a.png",
                    "sample_pod_paths": "EMTC/a.png",
                    "sample_documents": [],
                },
                {
                    "odoo_picking_id": 2,
                    "picking_name": "WH/OUT/2",
                    "state": "cancel",
                    "business_datetime": "2026-04-20 11:00:00",
                    "match_key": None,
                    "compare_status": "no_match_key",
                    "pod_file_count": 0,
                    "sample_pod_files": "",
                    "sample_pod_paths": "",
                    "sample_documents": [],
                },
            ],
            "pod_to_odoo_rows": [],
        }

        with mock.patch.object(emtc_reconcile, "load_filtered_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_metric_export_workbook("emtc", "2024-06", scope="odoo", metric="with_key")

        self.assertIn("_odoo_with_key_202406_", payload["filename"])
        self.assertEqual(payload["sheet_names"], ["Odoo 有 tracking number"])
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("Odoo 有 tracking number", workbook_xml)
            self.assertIn("WH/OUT/1", sheet_xml)
            self.assertNotIn("WH/OUT/2", sheet_xml)

    def test_build_reconcile_metric_export_workbook_for_pod_unparseable(self):
        dataset = {
            "generated_at": "2026-04-28T02:00:00Z",
            "source_config": emtc_reconcile.RECONCILE_SOURCE_CONFIGS["cmc"],
            "selected_start_month": "",
            "odoo_to_pod_rows": [],
            "pod_to_odoo_rows": [
                {
                    "document_uid": "pod-1",
                    "file_name": "bad.pdf",
                    "source_path": "CMC/bad.pdf",
                    "source_modified_at": "2026-04-27T02:00:00Z",
                    "match_key": None,
                    "compare_status": "no_match_key",
                    "matched_odoo_count": 0,
                    "matched_odoo_picking_names": "",
                    "matched_odoo_states": "",
                    "sample_document": {"file_url": "https://example.test/bad.pdf", "local_file_href": "/storage/files/CMC/bad.pdf"},
                    "reason_code": "NO_IDENTIFIER_FOUND",
                },
                {
                    "document_uid": "pod-2",
                    "file_name": "good.pdf",
                    "source_path": "CMC/good.pdf",
                    "source_modified_at": "2026-04-27T03:00:00Z",
                    "match_key": "WCB2B/OUT/1",
                    "compare_status": "found_in_odoo",
                    "matched_odoo_count": 1,
                    "matched_odoo_picking_names": "WCB2B/OUT/1",
                    "matched_odoo_states": "done",
                    "sample_document": {"file_url": "https://example.test/good.pdf", "local_file_href": "/storage/files/CMC/good.pdf"},
                    "reason_code": None,
                },
            ],
        }

        with mock.patch.object(emtc_reconcile, "load_filtered_reconcile_dataset", return_value=dataset):
            payload = emtc_reconcile.build_reconcile_metric_export_workbook("cmc", scope="pod", metric="unparseable")

        self.assertIn("_pod_unparseable_all_", payload["filename"])
        self.assertEqual(payload["sheet_names"], ["POD 解析不了匹配键"])
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("POD 解析不了匹配键", workbook_xml)
            self.assertIn("bad.pdf", sheet_xml)
            self.assertNotIn("good.pdf", sheet_xml)


if __name__ == "__main__":
    unittest.main()
