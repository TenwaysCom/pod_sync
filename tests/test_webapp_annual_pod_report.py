import sys
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import annual_pod_report


class AnnualPodReportTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "total_detail_rows": [
                {
                    "odoo_picking_id": 2401,
                    "carrier_id": 24,
                    "business_datetime": "2026-01-10 10:00:00",
                    "x_studio_pod_link": "https://example.test/emtc.pdf",
                },
                {
                    "odoo_picking_id": 2501,
                    "carrier_id": 25,
                    "business_datetime": "2026-01-11 10:00:00",
                    "x_studio_pod_link": None,
                },
                {
                    "odoo_picking_id": 3801,
                    "carrier_id": 38,
                    "business_datetime": "2026-02-10 10:00:00",
                    "x_studio_pod_link": None,
                },
                {
                    "odoo_picking_id": 3802,
                    "carrier_id": 38,
                    "business_datetime": "2026-02-11 10:00:00",
                    "x_studio_pod_link": "https://example.test/westerman.pdf",
                },
                {
                    "odoo_picking_id": 9901,
                    "carrier_id": 99,
                    "business_datetime": "2026-02-12 10:00:00",
                    "x_studio_pod_link": "https://example.test/other.pdf",
                },
            ]
        }

    def _patch_snapshot(self):
        return mock.patch.object(
            annual_pod_report,
            "load_odoo_missing_snapshot",
            return_value=self.snapshot,
        )

    def test_annual_summary_covers_all_three_sources_and_excludes_non_target(self):
        with self._patch_snapshot(), mock.patch.object(
            annual_pod_report,
            "_load_stock_tracking_pod_picking_ids",
            return_value={2501},
        ):
            data = annual_pod_report.build_annual_pod_report_data()

        self.assertEqual(data["years"], ["2026"])
        self.assertEqual(data["sources"], ["CMC", "EMTC", "Westerman"])
        rows = {row["source"]: row for row in data["rows"]}
        self.assertEqual(rows["CMC"]["odoo_orders"], 1)
        self.assertEqual(rows["CMC"]["with_pod"], 1)
        self.assertEqual(rows["EMTC"]["odoo_orders"], 1)
        self.assertEqual(rows["EMTC"]["with_pod"], 1)
        self.assertEqual(rows["Westerman"]["odoo_orders"], 2)
        self.assertEqual(rows["Westerman"]["with_pod"], 1)
        self.assertEqual(rows["Westerman"]["coverage_rate"], 50.0)
        self.assertNotIn("Other", rows)

    def test_monthly_summary_covers_westerman(self):
        with self._patch_snapshot(), mock.patch.object(
            annual_pod_report,
            "_load_stock_tracking_pod_picking_ids",
            return_value={2501},
        ):
            data = annual_pod_report.build_monthly_pod_report_data()

        self.assertEqual(data["months"], ["2026-02", "2026-01"])
        self.assertEqual(data["carriers"], ["CMC", "EMTC", "Westerman"])
        feb_westerman = next(
            row for row in data["rows"]
            if row["month_key"] == "2026-02" and row["carrier"] == "Westerman"
        )
        self.assertEqual(feb_westerman["total_orders"], 2)
        self.assertEqual(feb_westerman["with_pod"], 1)
        self.assertEqual(feb_westerman["without_pod"], 1)
        self.assertEqual(feb_westerman["coverage_rate"], 50.0)
        self.assertEqual(sum(row["total_orders"] for row in data["rows"]), 4)

    def test_workbook_contains_westerman_in_annual_and_monthly_sheets(self):
        with self._patch_snapshot(), mock.patch.object(
            annual_pod_report,
            "_load_stock_tracking_pod_picking_ids",
            return_value={2501},
        ):
            payload = annual_pod_report.build_annual_pod_workbook()

        self.assertIsNotNone(payload)
        with zipfile.ZipFile(BytesIO(payload["content"])) as archive:
            annual_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            monthly_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn("Westerman", annual_xml)
        self.assertIn("Westerman", monthly_xml)


if __name__ == "__main__":
    unittest.main()
