import unittest
from datetime import datetime, timezone
from unittest import mock
from xmlrpc.client import ProtocolError

from ops import validate_odoo_xmlrpc_migration as validation


class FakeModels:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def execute_kw(self, db, uid, password, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs or {}))
        if method == "search_read":
            cursor = next(
                value
                for field, operator, value in args[0]
                if field == "id" and operator == ">"
            )
            limit = (kwargs or {}).get("limit", 100)
            return [row for row in self.rows if row["id"] > cursor][:limit]
        if method == "read":
            ids = set(args[0])
            return [row for row in self.rows if row["id"] in ids]
        raise AssertionError(method)


ODOO_CFG = {"db": "db", "uid": 1, "api_key": "secret"}


class ValidateOdooXmlrpcMigrationTests(unittest.TestCase):
    def test_normalize_query_rows_accepts_dicts_and_arrays(self):
        self.assertEqual(
            validation._normalize_query_rows(
                {"header": ["id", "name"], "data": [[1, "A"], {"id": 2, "name": "B"}]}
            ),
            [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )

    def test_normalize_query_rows_fails_closed_on_contract_drift(self):
        with self.assertRaisesRegex(ValueError, "missing required headers"):
            validation._normalize_query_rows(
                {"header": ["id"], "data": [[1]]},
                required_headers={"id", "pod_link"},
            )
        with self.assertRaisesRegex(ValueError, "values for"):
            validation._normalize_query_rows(
                {"header": ["id", "pod_link"], "data": [[1]]},
                required_headers={"id", "pod_link"},
            )

    def test_xmlrpc_wrapper_refuses_write(self):
        with self.assertRaisesRegex(AssertionError, "non-read-only"):
            validation._execute_read_only(
                FakeModels(), ODOO_CFG, "stock.tracking", "write", [[1], {"pod_link": "x"}], {}
            )

    def test_xmlrpc_wrapper_retries_transient_503_with_backoff(self):
        class FlakyModels:
            calls = 0

            def execute_kw(self, *args, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise ProtocolError("https://odoo/xmlrpc", 503, "busy", {})
                return [{"id": 1}]

        models = FlakyModels()
        with mock.patch.object(validation.time, "sleep") as sleep_mock:
            result = validation._execute_read_only(
                models,
                ODOO_CFG,
                "stock.tracking",
                "read",
                [[1]],
                {"fields": ["id"]},
            )

        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(models.calls, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [1, 2])

    def test_search_read_pages_uses_monotonic_id_cursor(self):
        models = FakeModels([{"id": 1}, {"id": 2}, {"id": 3}])

        rows, metadata = validation._search_read_pages(
            models,
            ODOO_CFG,
            model="stock.tracking",
            domain=[("pod_link", "!=", False)],
            fields=["id"],
            page_size=2,
        )

        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertEqual(metadata["calls"], 2)
        self.assertEqual({call[1] for call in models.calls}, {"search_read"})

    def test_compare_separates_recent_from_stable_mismatches(self):
        cutoff = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
        left = [
            {"id": 1, "state": "done"},
            {"id": 2, "state": "done"},
        ]
        right = [
            {"id": 1, "state": "cancel", "write_date": "2026-07-17 09:01:00"},
            {"id": 2, "state": "cancel", "write_date": "2026-07-16 09:01:00"},
            {"id": 3, "state": "done", "write_date": "2026-07-17 09:02:00"},
            {"id": 4, "state": "done", "write_date": "2026-07-16 09:02:00"},
        ]

        result = validation.compare_canonical_rows(
            left,
            right,
            fields=("state",),
            recent_cutoff=cutoff,
            sample_limit=10,
        )

        self.assertEqual(result["field_mismatch_counts"], {"state": 2})
        self.assertEqual(result["stable_field_mismatch_counts"], {"state": 1})
        self.assertEqual(result["right_only_recent_count"], 1)
        self.assertEqual(result["right_only_stable_count"], 1)

    def test_invalid_and_duplicate_ids_are_visible_as_stable_differences(self):
        cutoff = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
        result = validation.compare_canonical_rows(
            [{"id": None, "state": "done"}, {"id": 1}, {"id": 1}],
            [{"id": 1}],
            fields=("state",),
            recent_cutoff=cutoff,
            sample_limit=10,
        )

        self.assertEqual(result["left_invalid_id_count"], 1)
        self.assertEqual(result["left_duplicate_id_count"], 1)
        self.assertTrue(
            validation._has_stable_difference(
                result,
                {"stable_still_in_scope_count": 0},
            )
        )

    def test_deterministic_index_comparison_ignores_transport_order(self):
        rows = [
            {
                "id": 1,
                "tracking_number": "TN",
                "picking_name": "OUT/1",
                "pod_link": "https://pod/one",
            },
            {
                "id": 2,
                "tracking_number": "TN",
                "picking_name": "OUT/1",
                "pod_link": "https://pod/two",
            },
        ]

        result = validation.compare_index_projections(rows, list(reversed(rows)), sample_limit=10)

        self.assertEqual(result["tracking"]["mapping_mismatch_count"], 0)
        self.assertEqual(result["picking"]["mapping_mismatch_count"], 0)
        self.assertEqual(result["tracking"]["left_only_key_count"], 0)
        self.assertEqual(result["picking"]["right_only_key_count"], 0)

        current_order_result = validation.compare_index_projections(
            rows,
            list(reversed(rows)),
            sample_limit=10,
            sort_by_id=False,
        )
        self.assertEqual(current_order_result["tracking"]["mapping_mismatch_count"], 1)
        self.assertEqual(current_order_result["picking"]["mapping_mismatch_count"], 1)

    def test_duplicate_summary_reports_conflicting_urls_without_persisting_keys(self):
        result = validation._duplicate_key_summary(
            [
                {"id": 1, "tracking_number": "TN", "picking_name": "OUT/1", "pod_link": "one"},
                {"id": 2, "tracking_number": "TN", "picking_name": "OUT/1", "pod_link": "two"},
            ]
        )

        self.assertEqual(result["tracking"]["duplicate_key_count"], 1)
        self.assertEqual(result["tracking"]["different_pod_url_key_count"], 1)
        self.assertNotIn("TN", str(result))


if __name__ == "__main__":
    unittest.main()
