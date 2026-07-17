import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_reconcile_cache


class FakeModels:
    """Small in-memory XML-RPC model proxy with domain and page handling."""

    def __init__(self):
        self.carriers = [
            {"id": 24, "name": "EMTC", "delivery_type": "spare", "active": True},
            {"id": 25, "name": "CMC", "delivery_type": "cmc", "active": True},
            {"id": 26, "name": "Westerman", "delivery_type": "westman", "active": True},
            {"id": 99, "name": "Other", "delivery_type": "other", "active": True},
        ]
        self.picking_types = [
            {"id": 1, "code": "outgoing"},
            {"id": 2, "code": "incoming"},
        ]
        self.pickings = [
            self._picking(101, "EMTC/OUT/001", 24, "EMTC", 1, "EMTC-001"),
            self._picking(102, "CMC/OUT/001", 25, "CMC", 1, "CMC-001"),
            self._picking(103, "WDB2B/OUT/01391", 26, "Westerman", 1, "WEST-001"),
            self._picking(104, "OTHER/OUT/001", 99, "Other", 1, "OTHER-001"),
            self._picking(105, "CMC/IN/001", 25, "CMC", 2, "CMC-IN-001"),
        ]
        self.tracking = [
            {
                "id": 201,
                "tracking_number": "EMTC-001",
                "pod_link": "https://pod.test/emtc.pdf",
                "picking_id": [101, "EMTC/OUT/001"],
                "write_date": "2026-07-17 09:00:00",
            },
            {
                "id": 202,
                "tracking_number": "WEST-001",
                "pod_link": "https://pod.test/westerman.pdf",
                "picking_id": [103, "WDB2B/OUT/01391"],
                "write_date": "2026-07-17 09:00:00",
            },
            {
                "id": 203,
                "tracking_number": "CMC-001",
                "pod_link": False,
                "picking_id": [102, "CMC/OUT/001"],
                "write_date": "2026-07-17 09:00:00",
            },
        ]
        self.fail_model = None
        self.calls = []

    @staticmethod
    def _picking(record_id, name, carrier_id, carrier_name, picking_type_id, tracking):
        return {
            "id": record_id,
            "name": name,
            "carrier_id": [carrier_id, carrier_name],
            "picking_type_id": [picking_type_id, "Delivery Orders"],
            "state": "done",
            "carrier_tracking_ref": tracking,
            "x_studio_pod_link": False,
            "date_done": "2026-07-16 12:00:00",
            "scheduled_date": "2026-07-16 10:00:00",
            "create_date": "2026-07-15 10:00:00",
            "write_date": "2026-07-17 09:00:00",
        }

    def move_emtc_out_of_scope_and_clear_pod(self):
        emtc = next(row for row in self.pickings if row["id"] == 101)
        emtc["carrier_id"] = [99, "Other"]
        emtc["write_date"] = "2026-07-17 10:05:00"

        tracking = next(row for row in self.tracking if row["id"] == 201)
        tracking["pod_link"] = False
        tracking["write_date"] = "2026-07-17 10:05:00"

    @staticmethod
    def _many2one_id(value):
        if isinstance(value, (list, tuple)):
            return value[0] if value else False
        return value

    def _field_value(self, model, row, field):
        if field == "carrier_id.delivery_type":
            carrier_id = self._many2one_id(row.get("carrier_id"))
            carrier = next(
                (item for item in self.carriers if item["id"] == carrier_id), None
            )
            return carrier.get("delivery_type") if carrier else False
        if field == "picking_type_id.code":
            type_id = self._many2one_id(row.get("picking_type_id"))
            picking_type = next(
                (item for item in self.picking_types if item["id"] == type_id), None
            )
            return picking_type.get("code") if picking_type else False
        return row.get(field, False)

    def _matches(self, model, row, domain):
        for field, operator, expected in domain:
            actual = self._field_value(model, row, field)
            comparable = self._many2one_id(actual)
            if operator == "=" and comparable != expected:
                return False
            if operator == "!=" and comparable == expected:
                return False
            if operator == "in" and comparable not in expected:
                return False
            if operator == ">" and not comparable > expected:
                return False
            if operator == ">=" and not comparable >= expected:
                return False
            if operator == "<=" and not comparable <= expected:
                return False
        return True

    @staticmethod
    def _project(row, fields):
        return {field: row.get(field, False) for field in fields}

    def execute_kw(self, db, uid, api_key, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        if self.fail_model == model:
            raise RuntimeError(f"forced {model} failure")
        self.assert_credentials(db, uid, api_key)
        records = {
            "delivery.carrier": self.carriers,
            "stock.picking.type": self.picking_types,
            "stock.picking": self.pickings,
            "stock.tracking": self.tracking,
        }[model]
        fields = kwargs.get("fields") or []
        if method == "read":
            wanted_ids = set(args[0])
            return [
                self._project(row, fields)
                for row in records
                if row["id"] in wanted_ids
            ]
        if method != "search_read":
            raise AssertionError(f"unexpected method: {method}")
        domain = args[0]
        result = [row for row in records if self._matches(model, row, domain)]
        result.sort(key=lambda row: row["id"])
        limit = kwargs.get("limit")
        if limit:
            result = result[:limit]
        return [self._project(row, fields) for row in result]

    @staticmethod
    def assert_credentials(db, uid, api_key):
        if (db, uid, api_key) != ("test-db", 7, "test-key"):
            raise AssertionError("unexpected fake Odoo credentials")


class OdooReconcileCacheTests(unittest.TestCase):
    @staticmethod
    def _config():
        return {
            "odoo": {
                "db": "test-db",
                "uid": 7,
                "api_key": "test-key",
            },
            "odoo_reconcile": {
                "page_size": 100,
                "incremental_overlap_seconds": 0,
                "full_refresh_interval_hours": 168,
            },
        }

    def test_read_backend_default_valid_and_invalid_values(self):
        self.assertEqual(
            odoo_reconcile_cache.resolve_read_backend({}),
            "querydeluxe",
        )
        self.assertEqual(
            odoo_reconcile_cache.resolve_read_backend({"odoo": {}}),
            "querydeluxe",
        )
        for configured, expected in (
            ("querydeluxe", "querydeluxe"),
            (" SHADOW ", "shadow"),
            ("XMLRPC", "xmlrpc"),
        ):
            with self.subTest(configured=configured):
                self.assertEqual(
                    odoo_reconcile_cache.resolve_read_backend(
                        {"odoo": {"read_backend": configured}}
                    ),
                    expected,
                )
        for configured in ("", "direct", None, 1):
            with self.subTest(configured=configured):
                with self.assertRaisesRegex(ValueError, "odoo.read_backend"):
                    odoo_reconcile_cache.resolve_read_backend(
                        {"odoo": {"read_backend": configured}}
                    )

    def test_full_then_incremental_refresh_and_load_helpers(self):
        fake = FakeModels()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reconcile.sqlite3"
            missing = odoo_reconcile_cache.cache_status(db_path=db_path)
            self.assertFalse(missing["available"])
            self.assertEqual(missing["generation"], 0)
            self.assertEqual(
                odoo_reconcile_cache.load_cached_picking_rows(db_path=db_path), []
            )
            self.assertEqual(
                odoo_reconcile_cache.load_cached_tracking_rows(db_path=db_path), []
            )

            full = odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                force_full=True,
                now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
                db_path=db_path,
            )

            self.assertEqual(full["refresh_kind"], "full")
            self.assertEqual(full["generation"], 1)
            self.assertEqual(
                full["row_counts"],
                {"pickings": 3, "tracking": 2, "tracking_pickings": 2},
            )
            pickings = odoo_reconcile_cache.load_cached_picking_rows(db_path=db_path)
            self.assertEqual(
                [(row["source_slug"], row["picking_name"]) for row in pickings],
                [
                    ("emtc", "EMTC/OUT/001"),
                    ("cmc", "CMC/OUT/001"),
                    ("westerman", "WDB2B/OUT/01391"),
                ],
            )
            self.assertEqual(
                [row["picking_name"] for row in odoo_reconcile_cache.load_cached_picking_rows(
                    source_slug="westerman", db_path=db_path
                )],
                ["WDB2B/OUT/01391"],
            )
            tracking = odoo_reconcile_cache.load_cached_tracking_rows(db_path=db_path)
            self.assertEqual(
                [(row["id"], row["picking_name"]) for row in tracking],
                [(201, "EMTC/OUT/001"), (202, "WDB2B/OUT/01391")],
            )
            self.assertEqual(
                odoo_reconcile_cache.cached_stock_tracking_picking_ids(db_path=db_path),
                [101, 103],
            )
            status = odoo_reconcile_cache.cache_status(db_path=db_path)
            self.assertTrue(status["available"])
            self.assertEqual(status["generation"], 1)
            self.assertEqual(status["last_full_at"], "2026-07-17T10:00:00Z")

            fake.move_emtc_out_of_scope_and_clear_pod()
            incremental = odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                now=datetime(2026, 7, 17, 10, 10, tzinfo=timezone.utc),
                db_path=db_path,
            )

            self.assertEqual(incremental["refresh_kind"], "incremental")
            self.assertEqual(incremental["generation"], 2)
            self.assertEqual(
                incremental["row_counts"],
                {"pickings": 2, "tracking": 1, "tracking_pickings": 2},
            )
            self.assertEqual(
                [row["picking_name"] for row in odoo_reconcile_cache.load_cached_picking_rows(
                    db_path=db_path
                )],
                ["CMC/OUT/001", "WDB2B/OUT/01391"],
            )
            remaining_tracking = odoo_reconcile_cache.load_cached_tracking_rows(
                db_path=db_path
            )
            self.assertEqual([row["id"] for row in remaining_tracking], [202])
            self.assertEqual(
                odoo_reconcile_cache.cached_stock_tracking_picking_ids(db_path=db_path),
                [103],
            )
            pod_state = odoo_reconcile_cache.load_cached_pod_state(db_path=db_path)
            self.assertEqual(pod_state["generation"], 2)
            self.assertEqual(pod_state["picking_ids"], [103])
            self.assertEqual([row["id"] for row in pod_state["rows"]], [202])

            # An older queued refresher must not move the incremental cursor
            # backwards after it eventually obtains the file lock.
            older_refresh = odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                now=datetime(2026, 7, 17, 10, 5, tzinfo=timezone.utc),
                db_path=db_path,
            )
            self.assertEqual(older_refresh["window_end"], "2026-07-17T10:10:00Z")
            self.assertEqual(
                odoo_reconcile_cache.cache_status(db_path=db_path)["last_success_at"],
                "2026-07-17T10:10:00Z",
            )

    def test_refresh_failure_preserves_last_successful_generation_and_rows(self):
        fake = FakeModels()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reconcile.sqlite3"
            odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                force_full=True,
                now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
                db_path=db_path,
            )
            before_status = odoo_reconcile_cache.cache_status(db_path=db_path)
            before_pickings = odoo_reconcile_cache.load_cached_picking_rows(
                db_path=db_path
            )
            before_tracking = odoo_reconcile_cache.load_cached_tracking_rows(
                db_path=db_path
            )

            fake.move_emtc_out_of_scope_and_clear_pod()
            fake.fail_model = "stock.tracking"
            with self.assertRaisesRegex(RuntimeError, "forced stock.tracking failure"):
                odoo_reconcile_cache.refresh_reconcile_cache(
                    full_cfg=self._config(),
                    models=fake,
                    now=datetime(2026, 7, 17, 10, 10, tzinfo=timezone.utc),
                    db_path=db_path,
                )

            after_status = odoo_reconcile_cache.cache_status(db_path=db_path)
            self.assertEqual(after_status["generation"], before_status["generation"])
            self.assertEqual(after_status["row_counts"], before_status["row_counts"])
            self.assertEqual(
                odoo_reconcile_cache.load_cached_picking_rows(db_path=db_path),
                before_pickings,
            )
            self.assertEqual(
                odoo_reconcile_cache.load_cached_tracking_rows(db_path=db_path),
                before_tracking,
            )

    def test_changed_carrier_scope_forces_full_rebuild(self):
        fake = FakeModels()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reconcile.sqlite3"
            odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                force_full=True,
                now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
                db_path=db_path,
            )
            fake.carriers.append(
                {
                    "id": 27,
                    "name": "Westerman Secondary",
                    "delivery_type": "westman",
                    "active": True,
                }
            )

            result = odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                now=datetime(2026, 7, 17, 10, 10, tzinfo=timezone.utc),
                db_path=db_path,
            )

            self.assertEqual(result["refresh_kind"], "full")
            self.assertEqual(result["target_carrier_ids"], [24, 25, 26, 27])

    def test_changed_delivery_type_mapping_forces_full_rebuild(self):
        fake = FakeModels()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reconcile.sqlite3"
            odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                force_full=True,
                now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
                db_path=db_path,
            )
            fake.carriers[0]["delivery_type"] = "cmc"
            fake.carriers[1]["delivery_type"] = "spare"

            result = odoo_reconcile_cache.refresh_reconcile_cache(
                full_cfg=self._config(),
                models=fake,
                now=datetime(2026, 7, 17, 10, 10, tzinfo=timezone.utc),
                db_path=db_path,
            )
            rows = odoo_reconcile_cache.load_cached_picking_rows(db_path=db_path)
            by_id = {row["odoo_picking_id"]: row for row in rows}

        self.assertEqual(result["refresh_kind"], "full")
        self.assertEqual(by_id[101]["source_slug"], "cmc")
        self.assertEqual(by_id[102]["source_slug"], "emtc")


if __name__ == "__main__":
    unittest.main()
