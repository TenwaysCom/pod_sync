import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import reporting


class ReportingTests(unittest.TestCase):
    def test_build_odoo_analysis_context_summarizes_fields_and_states(self):
        snapshot = {
            "endpoints": {"total_detail_url": "https://tenways.odoo.com/api/querydeluxe/64/data"},
            "total_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "picking_name": "WRB2B/OUT/00001",
                    "state": "done",
                    "business_datetime": "2026-04-27 09:42:28",
                    "carrier_tracking_ref": "3SYZXG0000001",
                    "x_studio_pod_link": None,
                },
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "picking_name": "WRB2B/OUT/00002",
                    "state": "cancel",
                    "business_datetime": "2026-04-15 10:00:00",
                    "carrier_tracking_ref": None,
                    "x_studio_pod_link": "http://example.test/pod/2.png",
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "picking_name": "WCB2B/OUT/00003",
                    "state": "done",
                    "business_datetime": "2026-03-01 10:00:00",
                    "tracking_number": "TRACK-3",
                    "x_studio_pod_link": "http://example.test/pod/3.png",
                },
            ],
        }

        analysis = reporting.build_odoo_analysis_context(snapshot)

        self.assertEqual(analysis["overview"]["total_count"], 3)
        self.assertEqual(analysis["overview"]["with_tracking_count"], 2)
        self.assertEqual(analysis["overview"]["with_pod_link_count"], 2)
        self.assertEqual(analysis["overview"]["ready_for_push_count"], 1)
        self.assertEqual(analysis["overview"]["data_source"], "https://tenways.odoo.com/api/querydeluxe/64/data")
        self.assertEqual(analysis["field_rows"][-1]["field_name"], "x_studio_pod_link")
        done_row = next(row for row in analysis["state_rows"] if row["state"] == "done")
        self.assertEqual(done_row["row_count"], 2)
        emtc_cancel = next(row for row in analysis["carrier_state_rows"] if row["carrier_id"] == 24 and row["state"] == "cancel")
        self.assertEqual(emtc_cancel["with_pod_link_count"], 1)

    def test_build_pod_scope_context_limits_to_emtc_and_cmc(self):
        snapshot = {
            "total_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "state": "done",
                    "carrier_tracking_ref": "TRACK-24",
                    "x_studio_pod_link": None,
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "state": "done",
                    "carrier_tracking_ref": None,
                    "x_studio_pod_link": "http://example.test/pod/25.png",
                },
                {
                    "carrier_id": 11,
                    "carrier_name": "Other Carrier",
                    "state": "done",
                    "carrier_tracking_ref": "TRACK-11",
                    "x_studio_pod_link": None,
                },
            ]
        }

        context = reporting.build_pod_scope_context(snapshot)

        self.assertEqual(context["overview"]["total_count"], 2)
        self.assertEqual(context["overview"]["without_tracking_count"], 1)
        self.assertEqual(context["overview"]["with_pod_link_count"], 1)
        self.assertEqual(len(context["carrier_rows"]), 2)
        self.assertEqual(context["carrier_rows"][0]["carrier_id"], 24)

    def test_build_compare_analysis_context_groups_other_carriers(self):
        snapshot = {
            "totals": {
                "unique_identifier_count": 10,
                "matched_count": 4,
                "matched_cancelled_count": 1,
                "matched_other_carrier_count": 2,
                "not_found_count": 2,
                "ambiguous_count": 1,
                "match_rate_percent": 70.0,
                "review_document_count": 3,
            },
            "matched_other_carrier_rows": [
                {
                    "matched_odoo_carrier_names": "Legacy Carrier",
                    "matched_odoo_picking_names": "WCB2B/OUT/16079",
                },
                {
                    "matched_odoo_carrier_names": "Legacy Carrier",
                    "matched_odoo_picking_names": "WCB2B/OUT/16160",
                },
            ],
        }

        context = reporting.build_compare_analysis_context(snapshot)

        self.assertEqual(context["overview"]["matched_any_count"], 7)
        self.assertEqual(context["status_rows"][2]["label"], "命中其他承运商")
        self.assertEqual(context["other_carrier_rows"][0]["carrier_name"], "Legacy Carrier")
        self.assertEqual(context["other_carrier_rows"][0]["count"], 2)

    def test_build_monthly_tracking_rows_groups_by_month_and_carrier(self):
        snapshot = {
            "total_detail_rows": [
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "state": "done",
                    "business_datetime": "2026-04-27 09:42:28",
                    "tracking_status": "with_tracking",
                },
                {
                    "carrier_id": 24,
                    "carrier_name": "EMTC",
                    "state": "cancel",
                    "business_datetime": "2026-04-15 10:00:00",
                    "tracking_status": "missing_tracking",
                },
                {
                    "carrier_id": 25,
                    "carrier_name": "CMC",
                    "state": "done",
                    "business_datetime": "2026-03-01 10:00:00",
                    "tracking_status": "missing_tracking",
                },
            ]
        }

        rows = reporting.build_monthly_tracking_rows(snapshot)

        self.assertEqual(rows[0]["month_key"], "2026-04")
        april_emtc = next(row for row in rows if row["month_key"] == "2026-04" and row["carrier_id"] == 24)
        march_cmc = next(row for row in rows if row["month_key"] == "2026-03" and row["carrier_id"] == 25)
        self.assertEqual(april_emtc["total_done_outgoing"], 2)
        self.assertEqual(april_emtc["done_count"], 1)
        self.assertEqual(april_emtc["cancel_count"], 1)
        self.assertEqual(april_emtc["without_tracking_number"], 1)
        self.assertEqual(april_emtc["missing_rate_percent"], 50.0)
        self.assertEqual(march_cmc["total_done_outgoing"], 1)

    def test_build_monthly_tracking_overview_groups_by_month(self):
        monthly_rows = [
            {
                "month_key": "2026-04",
                "carrier_id": 24,
                "carrier_name": "EMTC",
                "total_done_outgoing": 2,
                "done_count": 1,
                "cancel_count": 1,
                "with_tracking_number": 1,
                "without_tracking_number": 1,
                "missing_rate_percent": 50.0,
            },
            {
                "month_key": "2026-04",
                "carrier_id": 25,
                "carrier_name": "CMC",
                "total_done_outgoing": 3,
                "done_count": 3,
                "cancel_count": 0,
                "with_tracking_number": 2,
                "without_tracking_number": 1,
                "missing_rate_percent": 33.33,
            },
        ]

        rows = reporting.build_monthly_tracking_overview(monthly_rows)

        self.assertEqual(rows[0]["month_key"], "2026-04")
        self.assertEqual(rows[0]["total_done_outgoing"], 5)
        self.assertEqual(rows[0]["without_tracking_number"], 2)
        self.assertEqual(rows[0]["missing_rate_percent"], 40.0)
        self.assertEqual(rows[0]["carrier_names"], "EMTC / CMC")

    def test_build_report_context_loads_both_snapshots(self):
        with mock.patch.object(reporting, "load_odoo_missing_snapshot", return_value={"generated_at": "x"}), \
            mock.patch.object(reporting, "load_pod_odoo_compare_snapshot", return_value={"generated_at": "y"}), \
            mock.patch.object(reporting, "build_monthly_tracking_rows", return_value=[{"month_key": "2026-04"}]), \
            mock.patch.object(reporting, "build_monthly_tracking_overview", return_value=[{"month_key": "2026-04"}]):
            context = reporting.build_report_context()

        self.assertEqual(context["odoo_snapshot"]["generated_at"], "x")
        self.assertEqual(context["compare_snapshot"]["generated_at"], "y")
        self.assertEqual(context["monthly_rows"][0]["month_key"], "2026-04")
        self.assertEqual(context["monthly_overview_rows"][0]["month_key"], "2026-04")
        self.assertIn("pod_scope_analysis", context)
        self.assertIn("compare_analysis", context)


if __name__ == "__main__":
    unittest.main()
