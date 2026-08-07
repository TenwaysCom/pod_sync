import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_missing_report


class OdooMissingReportTests(unittest.TestCase):
    def test_xmlrpc_mode_builds_snapshot_from_target_cache_without_querydeluxe(self):
        rows = [
            {
                "carrier_id": 91,
                "carrier_name": "Westerman",
                "source_slug": "westerman",
                "odoo_picking_id": 5001,
                "picking_name": "WDB2B/OUT/01391",
                "state": "done",
                "business_datetime": "2026-07-17 09:00:00",
                "tracking_number": "10094961",
            }
        ]
        full_cfg = {"odoo": {"read_backend": "xmlrpc"}}
        with (
            mock.patch("webapp.odoo_reconcile_cache.load_config", return_value=full_cfg),
            mock.patch(
                "webapp.odoo_reconcile_cache.refresh_reconcile_cache",
                return_value={"generation": 3, "cache_epoch": "epoch-3"},
            ),
            mock.patch(
                "webapp.odoo_reconcile_cache.load_cached_picking_state",
                return_value={
                    "available": True,
                    "generation": 3,
                    "cache_epoch": "epoch-3",
                    "last_success_at": "2026-07-17T09:00:00Z",
                    "target_carrier_ids": [91],
                    "rows": rows,
                },
            ),
            mock.patch.object(odoo_missing_report, "_fetch_querydeluxe_rows") as query_fetch,
            mock.patch.object(odoo_missing_report, "save_odoo_missing_snapshot"),
        ):
            snapshot = odoo_missing_report.refresh_odoo_missing_snapshot(config_path="test.json")

        query_fetch.assert_not_called()
        self.assertEqual(snapshot["read_backend"], "xmlrpc")
        self.assertEqual(snapshot["authoritative_source"], "xmlrpc_cache")
        self.assertEqual(snapshot["xmlrpc_cache"]["cache_epoch"], "epoch-3")
        self.assertEqual(snapshot["totals"]["total_done_outgoing"], 1)
        self.assertEqual(snapshot["total_detail_rows"][0]["picking_name"], "WDB2B/OUT/01391")

    def test_shadow_cache_failure_keeps_querydeluxe_authoritative(self):
        full_cfg = {
            "odoo": {"read_backend": "shadow"},
            "querydeluxe": {"missing_tracking_query_id": 64},
        }
        query_rows = [
            {
                "carrier_id": 24,
                "carrier_name": "EMTC",
                "odoo_picking_id": 1,
                "picking_name": "WH/OUT/1",
                "state": "done",
                "business_datetime": "2026-07-17 09:00:00",
                "tracking_number": "TRACK-1",
            }
        ]
        with (
            mock.patch("webapp.odoo_reconcile_cache.load_config", return_value=full_cfg),
            mock.patch("webapp.odoo_reconcile_cache.refresh_reconcile_cache", side_effect=TimeoutError("down")),
            mock.patch(
                "webapp.odoo_reconcile_cache.load_cached_picking_state",
                return_value={
                    "available": False,
                    "generation": 0,
                    "last_success_at": None,
                    "target_carrier_ids": [],
                    "rows": [],
                },
            ),
            mock.patch.object(odoo_missing_report, "_get_default_detail_url", return_value="https://example.test/64"),
            mock.patch.object(odoo_missing_report, "_fetch_querydeluxe_rows", return_value={"rows": query_rows}),
            mock.patch.object(odoo_missing_report, "save_odoo_missing_snapshot"),
        ):
            snapshot = odoo_missing_report.refresh_odoo_missing_snapshot(config_path="test.json")

        self.assertEqual(snapshot["authoritative_source"], "querydeluxe")
        self.assertEqual(snapshot["totals"]["total_done_outgoing"], 1)
        self.assertEqual(snapshot["warnings"], ["XML-RPC cache refresh failed: TimeoutError"])

    def test_build_snapshot_enriches_summary_with_detail_counts(self):
        snapshot = odoo_missing_report.build_odoo_missing_snapshot(
            total_detail_rows=[
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1001,
                    "picking_name": "WRB2B/OUT/00001",
                    "state": "done",
                    "business_datetime": "2024-02-12 16:00:24",
                    "carrier_tracking_ref": None,
                },
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1000,
                    "picking_name": "WRB2C/OUT/00003",
                    "state": "done",
                    "business_datetime": "2024-02-10 02:35:13",
                    "carrier_tracking_ref": "3SYZXG4370467",
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "odoo_picking_id": 2001,
                    "picking_name": "WCB2B/OUT/10001",
                    "state": "cancel",
                    "business_datetime": "2024-05-29 21:04:52",
                    "carrier_tracking_ref": None,
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "odoo_picking_id": 2000,
                    "picking_name": "WCB2B/OUT/09999",
                    "state": "done",
                    "business_datetime": "2024-05-25 11:00:00",
                    "carrier_tracking_ref": "TRACK-1",
                },
            ],
            detail_url="https://example.test/detail",
        )

        self.assertEqual(snapshot["totals"]["without_tracking_number"], 2)
        self.assertEqual(snapshot["totals"]["detail_row_count"], 2)
        self.assertTrue(snapshot["totals"]["detail_matches_summary"])
        self.assertEqual(snapshot["summary_rows"][0]["detail_row_count"], 1)
        self.assertEqual(snapshot["summary_rows"][0]["missing_rate_percent"], 50.0)
        self.assertEqual(snapshot["totals"]["done_count"], 3)
        self.assertEqual(snapshot["totals"]["cancel_count"], 1)
        self.assertEqual(len(snapshot["total_detail_rows"]), 4)
        self.assertEqual(len(snapshot["missing_detail_rows"]), 2)

    def test_build_listing_supports_filter_and_pagination(self):
        snapshot = {
            "generated_at": "2026-04-27T12:00:00Z",
            "totals": {"without_tracking_number": 1},
            "total_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1002,
                    "picking_name": "WRB2B/OUT/00002",
                    "state": "cancel",
                    "business_datetime": "2024-02-12 16:10:22",
                    "carrier_tracking_ref": None,
                    "x_studio_pod_link": None,
                    "tracking_status": "missing_tracking",
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "odoo_picking_id": 2002,
                    "picking_name": "WCB2B/OUT/10002",
                    "state": "done",
                    "business_datetime": "2024-05-29 21:05:52",
                    "carrier_tracking_ref": "TRACK-2",
                    "x_studio_pod_link": "http://example.test/pod.png",
                    "tracking_status": "with_tracking",
                },
            ],
        }

        payload = odoo_missing_report.build_odoo_missing_listing(
            query="wcb2b",
            carrier="25",
            tracking_status="with_tracking",
            state="done",
            page=1,
            page_size=1,
            snapshot=snapshot,
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["carrier_name"], "CMC")
        self.assertEqual(payload["carrier_options"][1]["label"], "EMTC (24)")
        self.assertEqual(payload["tracking_status_options"][1]["label"], "缺失 tracking number")
        self.assertEqual(payload["state_options"][1]["label"], "cancel")

        pod_link_payload = odoo_missing_report.build_odoo_missing_listing(
            tracking_status="",
            pod_link_status="with_pod_link",
            snapshot=snapshot,
        )
        self.assertEqual(pod_link_payload["total"], 1)
        self.assertEqual(pod_link_payload["items"][0]["carrier_name"], "CMC")
        self.assertEqual(pod_link_payload["pod_link_status_options"][1]["label"], "已有 POD 链接")

    def test_save_and_resolve_artifacts(self):
        snapshot = {
            "generated_at": "2026-04-27T12:00:00Z",
            "endpoints": {
                "total_detail_url": "https://example.test/detail",
            },
            "totals": {
                "carrier_count": 1,
                "total_done_outgoing": 10,
                "done_count": 9,
                "cancel_count": 1,
                "with_tracking_number": 9,
                "without_tracking_number": 1,
                "detail_row_count": 1,
                "missing_rate_percent": 10.0,
                "detail_matches_summary": True,
            },
            "summary_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "counting_start_datetime": "2024-02-10 02:35:13",
                    "counting_end_datetime": "2026-04-27 09:42:28",
                    "total_done_outgoing": 10,
                    "done_count": 9,
                    "cancel_count": 1,
                    "with_tracking_number": 9,
                    "without_tracking_number": 1,
                    "missing_rate_percent": 10.0,
                    "detail_row_count": 1,
                    "detail_matches_summary": True,
                    "oldest_missing_datetime": "2024-02-12 16:00:24",
                    "latest_missing_datetime": "2024-02-12 16:00:24",
                }
            ],
            "total_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1000,
                    "picking_name": "WRB2B/OUT/00000",
                    "state": "done",
                    "business_datetime": "2024-02-10 02:35:13",
                    "carrier_tracking_ref": "TRACK-1",
                    "tracking_status": "with_tracking",
                },
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1001,
                    "picking_name": "WRB2B/OUT/00001",
                    "state": "cancel",
                    "business_datetime": "2024-02-12 16:00:24",
                    "carrier_tracking_ref": None,
                    "tracking_status": "missing_tracking",
                }
            ],
            "missing_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "odoo_picking_id": 1001,
                    "picking_name": "WRB2B/OUT/00001",
                    "state": "cancel",
                    "business_datetime": "2024-02-12 16:00:24",
                    "carrier_tracking_ref": None,
                    "tracking_status": "missing_tracking",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            with mock.patch.object(odoo_missing_report, "RUNTIME_DIR", runtime_dir), \
                mock.patch.object(odoo_missing_report, "SNAPSHOT_PATH", runtime_dir / "latest_snapshot.json"), \
                mock.patch.object(odoo_missing_report, "SUMMARY_CSV_PATH", runtime_dir / "summary.csv"), \
                mock.patch.object(odoo_missing_report, "TOTAL_DETAIL_CSV_PATH", runtime_dir / "total_detail.csv"), \
                mock.patch.object(odoo_missing_report, "MISSING_DETAIL_CSV_PATH", runtime_dir / "missing_detail.csv"), \
                mock.patch.object(odoo_missing_report, "REPORT_MD_PATH", runtime_dir / "report.md"):
                odoo_missing_report.save_odoo_missing_snapshot(snapshot)
                self.assertIsNotNone(odoo_missing_report.resolve_odoo_missing_artifact("summary_csv"))
                self.assertIsNotNone(odoo_missing_report.resolve_odoo_missing_artifact("total_detail_csv"))
                self.assertIsNotNone(odoo_missing_report.resolve_odoo_missing_artifact("missing_detail_csv"))
                self.assertIsNotNone(odoo_missing_report.resolve_odoo_missing_artifact("snapshot_json"))
                self.assertIsNotNone(odoo_missing_report.resolve_odoo_missing_artifact("report_md"))

    def test_older_cache_generation_cannot_overwrite_newer_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            paths = {
                "RUNTIME_DIR": runtime_dir,
                "SNAPSHOT_PATH": runtime_dir / "latest_snapshot.json",
                "SUMMARY_CSV_PATH": runtime_dir / "summary.csv",
                "TOTAL_DETAIL_CSV_PATH": runtime_dir / "total_detail.csv",
                "MISSING_DETAIL_CSV_PATH": runtime_dir / "missing_detail.csv",
                "REPORT_MD_PATH": runtime_dir / "report.md",
            }
            newer = {
                "read_backend": "xmlrpc",
                "xmlrpc_cache": {
                    "generation": 2,
                    "last_success_at": "2026-07-17T11:00:00Z",
                },
                "summary_rows": [],
                "total_detail_rows": [],
                "missing_detail_rows": [],
            }
            older = {
                **newer,
                "xmlrpc_cache": {
                    "generation": 1,
                    "last_success_at": "2026-07-17T10:00:00Z",
                },
            }
            with mock.patch.multiple(odoo_missing_report, **paths):
                odoo_missing_report.save_odoo_missing_snapshot(newer)
                result = odoo_missing_report.save_odoo_missing_snapshot(older)
                saved = json.loads(paths["SNAPSHOT_PATH"].read_text(encoding="utf-8"))

        self.assertTrue(result["stale_generation_rejected"])
        self.assertEqual(saved["xmlrpc_cache"]["generation"], 2)


if __name__ == "__main__":
    unittest.main()
