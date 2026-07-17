import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.westerman_api import candidates


class FakeModels:
    def __init__(self, picking_rows, tracking_rows=None):
        self.picking_rows = list(picking_rows)
        self.tracking_rows = list(tracking_rows or [])
        self.calls = []

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        if model == "stock.picking":
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", len(self.picking_rows))
            return self.picking_rows[offset : offset + limit]
        if model == "stock.tracking":
            return self.tracking_rows
        raise AssertionError(model)


class WestermanCandidateTests(unittest.TestCase):
    def _config(self):
        return {
            "westerman_api": {
                "enabled": True,
                "candidate_lookback_days": 180,
                "candidate_page_size": 100,
                "candidate_scan_limit": 5000,
            },
            "odoo": {"url": "https://odoo.example.test", "db": "db", "uid": 9, "api_key": "secret"},
        }

    def test_domain_uses_stable_delivery_type_and_configured_lifecycle_states(self):
        now = datetime(2026, 7, 17, tzinfo=timezone.utc)
        domain = candidates.build_candidate_domain(self._config()["westerman_api"], now=now)

        self.assertIn(("carrier_id.delivery_type", "=", "westman"), domain)
        self.assertIn(("picking_type_id.code", "=", "outgoing"), domain)
        self.assertIn(("state", "in", ["waiting", "assigned", "done"]), domain)
        self.assertNotIn(("carrier_id", "=", 38), domain)
        self.assertIn(("create_date", ">=", "2026-01-18 00:00:00"), domain)

    def test_fetch_candidates_excludes_pickings_with_existing_stock_tracking_pod(self):
        models = FakeModels(
            [
                {
                    "id": 1391,
                    "name": "WDB2B/OUT/01391",
                    "state": "done",
                    "wms_status": "ref_back",
                    "carrier_tracking_ref": "U260203465",
                },
                {
                    "id": 1436,
                    "name": "WDB2B/OUT/01436",
                    "state": "assigned",
                    "wms_status": "sent",
                    "carrier_tracking_ref": False,
                },
            ],
            tracking_rows=[{"picking_id": [1391, "WDB2B/OUT/01391"]}],
        )

        result = candidates.fetch_candidates(self._config(), models=models)

        self.assertEqual(result["scanned_count"], 2)
        self.assertEqual(result["already_completed_count"], 1)
        self.assertEqual(result["completed_references"], ["WDB2B/OUT/01391"])
        self.assertEqual(
            result["candidates"],
            [
                {
                    "shipment_reference": "WDB2B/OUT/01436",
                    "odoo_picking_id": 1436,
                    "tracking_number": None,
                    "picking_state": "assigned",
                    "wms_status": "sent",
                }
            ],
        )
        picking_call = next(call for call in models.calls if call[0] == "stock.picking")
        self.assertIn(("carrier_id.delivery_type", "=", "westman"), picking_call[2][0])
        self.assertEqual(picking_call[3]["order"], "create_date desc, id desc")

    def test_disabled_source_does_not_connect_to_odoo(self):
        result = candidates.fetch_candidates({"westerman_api": {"enabled": False}})
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["scan_truncated"])

    def test_missing_source_or_enabled_flag_does_not_scan_odoo(self):
        models = FakeModels(
            [
                {
                    "id": 1,
                    "name": "WDB2B/OUT/00001",
                    "state": "done",
                    "wms_status": "ref_back",
                }
            ]
        )

        for config in ({}, {"westerman_api": {}}, {"westerman_api": {"enabled": 1}}):
            with self.subTest(config=config):
                result = candidates.fetch_candidates(config, models=models)
                self.assertEqual(result["candidate_count"], 0)
                self.assertEqual(result["candidates"], [])

        self.assertEqual(models.calls, [])

    def test_scan_limit_is_reported_as_truncated_for_safe_pool_reconciliation(self):
        config = self._config()
        config["westerman_api"]["candidate_scan_limit"] = 1
        models = FakeModels(
            [
                {
                    "id": 1,
                    "name": "WDB2B/OUT/00001",
                    "state": "done",
                    "wms_status": "ref_back",
                },
                {
                    "id": 2,
                    "name": "WDB2B/OUT/00002",
                    "state": "done",
                    "wms_status": "ref_back",
                },
            ]
        )

        result = candidates.fetch_candidates(config, models=models)

        self.assertEqual(result["scanned_count"], 1)
        self.assertTrue(result["scan_truncated"])

    def test_exact_scan_limit_without_sentinel_row_is_not_truncated(self):
        config = self._config()
        config["westerman_api"]["candidate_scan_limit"] = 1
        models = FakeModels(
            [
                {
                    "id": 1,
                    "name": "WDB2B/OUT/00001",
                    "state": "done",
                    "wms_status": "ref_back",
                }
            ]
        )

        result = candidates.fetch_candidates(config, models=models)

        self.assertEqual(result["scanned_count"], 1)
        self.assertFalse(result["scan_truncated"])


if __name__ == "__main__":
    unittest.main()
