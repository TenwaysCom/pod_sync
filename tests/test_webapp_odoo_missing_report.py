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


if __name__ == "__main__":
    unittest.main()
