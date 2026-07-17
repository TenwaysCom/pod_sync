import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_pull, odoo_read_backends
from webapp.odoo_pull import ExistingPodIndex


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.raise_for_status_calls = 0

    def raise_for_status(self):
        self.raise_for_status_calls += 1

    def json(self):
        return self.payload


class CoverageModels:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute_kw(self, db, uid, api_key, model, method, args, kwargs):
        self.calls.append((db, uid, api_key, model, method, args, kwargs))
        self.assertEqualCredentials(db, uid, api_key)
        if (model, method) != ("stock.tracking", "search_read"):
            raise AssertionError(f"unexpected XML-RPC call: {model}.{method}")
        cursor_terms = [term for term in args[0] if term[:2] == ("id", ">")]
        if len(cursor_terms) != 1:
            raise AssertionError(f"missing keyset cursor: {args[0]!r}")
        cursor = cursor_terms[0][2]
        page = [row for row in self.rows if row["id"] > cursor]
        page.sort(key=lambda row: row["id"])
        return page[: kwargs["limit"]]

    @staticmethod
    def assertEqualCredentials(db, uid, api_key):
        if (db, uid, api_key) != ("test-db", 7, "test-key"):
            raise AssertionError("unexpected fake Odoo credentials")


class QueryDeluxeReadTests(unittest.TestCase):
    @staticmethod
    def _config(backend="querydeluxe"):
        return {
            "odoo": {
                "url": "https://odoo.test",
                "timeout_seconds": 17,
                "read_backend": backend,
                "db": "test-db",
                "uid": 7,
                "api_key": "test-key",
            },
            "querydeluxe": {"existing_pod_query_id": 65},
        }

    @staticmethod
    def _payload(rows):
        return {
            "success": True,
            "data": {
                "header": ["tracking_number", "picking_name", "pod_link"],
                "data": rows,
            },
        }

    def test_querydeluxe_accepts_dict_and_exact_width_array_rows(self):
        response = FakeResponse(
            self._payload(
                [
                    {
                        "tracking_number": "TN-DICT",
                        "picking_name": "OUT/DICT",
                        "pod_link": "https://pod.test/dict.pdf",
                        "extra": "preserved",
                    },
                    ["TN-ARRAY", "OUT/ARRAY", "https://pod.test/array.pdf"],
                ]
            )
        )

        with mock.patch("webapp.odoo_read_backends.requests.get", return_value=response) as get:
            rows = odoo_read_backends._querydeluxe_rows(self._config())

        self.assertEqual(
            rows,
            [
                {
                    "tracking_number": "TN-DICT",
                    "picking_name": "OUT/DICT",
                    "pod_link": "https://pod.test/dict.pdf",
                    "extra": "preserved",
                },
                {
                    "tracking_number": "TN-ARRAY",
                    "picking_name": "OUT/ARRAY",
                    "pod_link": "https://pod.test/array.pdf",
                },
            ],
        )
        get.assert_called_once_with(
            "https://odoo.test/api/querydeluxe/65/data", timeout=17
        )
        self.assertEqual(response.raise_for_status_calls, 1)

    def test_querydeluxe_rejects_missing_headers_and_malformed_rows(self):
        cases = [
            (
                {
                    "success": True,
                    "data": {
                        "header": ["tracking_number", "picking_name"],
                        "data": [],
                    },
                },
                "missing required columns",
            ),
            (self._payload([["TN", "OUT/ONLY-TWO"]]), "invalid shape"),
            (
                self._payload(
                    [{"tracking_number": "TN", "picking_name": "OUT/NO-POD"}]
                ),
                "missing required fields",
            ),
            (self._payload(["not-a-row"]), "invalid shape"),
        ]

        for payload, message in cases:
            with self.subTest(message=message), mock.patch(
                "webapp.odoo_read_backends.requests.get",
                return_value=FakeResponse(payload),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    odoo_read_backends._querydeluxe_rows(self._config())

    def test_querydeluxe_rejects_non_object_top_level_and_data_shapes(self):
        malformed_payloads = [
            [],
            {"success": True, "data": []},
            {
                "success": True,
                "data": {
                    "header": ["tracking_number", "picking_name", "pod_link"],
                    "data": {"unexpected": "object"},
                },
            },
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload), mock.patch(
                "webapp.odoo_read_backends.requests.get",
                return_value=FakeResponse(payload),
            ):
                with self.assertRaisesRegex(ValueError, "response|shape"):
                    odoo_read_backends._querydeluxe_rows(self._config())

    def test_direct_coverage_uses_uncapped_keyset_pagination(self):
        models = CoverageModels(
            [
                {"id": 1, "picking_id": [301, "OUT/301"]},
                {"id": 2, "picking_id": 302},
                {"id": 3, "picking_id": [301, "OUT/301"]},
                {"id": 4, "picking_id": False},
                {"id": 5, "picking_id": [305, "OUT/305"]},
            ]
        )

        result = odoo_read_backends._direct_coverage_ids(
            self._config(), models=models, page_size=2
        )

        self.assertEqual(result, [301, 302, 305])
        self.assertEqual(len(models.calls), 3)
        self.assertEqual(
            [call[5][0][-1] for call in models.calls],
            [("id", ">", 0), ("id", ">", 2), ("id", ">", 4)],
        )
        for call in models.calls:
            self.assertEqual(call[6]["order"], "id ASC")
            self.assertEqual(call[6]["limit"], 2)

    def test_shadow_comparison_ignores_duplicate_order_and_classifies_only_rows_as_concurrent(self):
        left = [
            {"tracking_number": "TN", "picking_name": "OUT/1", "pod_link": "u1"},
            {"tracking_number": "TN", "picking_name": "OUT/1", "pod_link": "u2"},
        ]
        right = list(reversed(left))
        counts = odoo_read_backends._shadow_counts(left, right)
        self.assertEqual(odoo_read_backends._shadow_status(counts), "match")

        right.append(
            {"tracking_number": "TN-NEW", "picking_name": "OUT/2", "pod_link": "u3"}
        )
        counts = odoo_read_backends._shadow_counts(left, right)
        self.assertEqual(
            odoo_read_backends._shadow_status(counts),
            "inconclusive_concurrent_changes",
        )

        many_new_rows = right + [
            {
                "tracking_number": f"TN-BULK-{number}",
                "picking_name": f"OUT/BULK/{number}",
                "pod_link": f"u-bulk-{number}",
            }
            for number in range(25)
        ]
        counts = odoo_read_backends._shadow_counts(left, many_new_rows)
        self.assertEqual(odoo_read_backends._shadow_status(counts), "differences")

    def test_querydeluxe_coverage_is_live_even_when_xmlrpc_cache_exists(self):
        query_rows = [
            {"tracking_number": "TN-1", "picking_name": "OUT/1", "pod_link": "u1"}
        ]
        with mock.patch(
            "webapp.odoo_read_backends._querydeluxe_rows", return_value=query_rows
        ), mock.patch(
            "webapp.odoo_read_backends._direct_coverage_ids", return_value=[99]
        ) as direct, mock.patch(
            "webapp.odoo_read_backends.load_cached_pod_state",
            return_value={
                "available": True,
                "generation": 7,
                "rows": [],
                "picking_ids": [77],
            },
        ) as load_cache:
            state = odoo_read_backends.fetch_pod_read_state(
                self._config("querydeluxe"), config_path="config.test.json"
            )

        self.assertEqual(state["coverage_ids"], [99])
        self.assertEqual(state["coverage_status"], "complete")
        self.assertEqual(state["cache_generation"], 0)
        direct.assert_called_once()
        load_cache.assert_not_called()

    def test_shadow_refresh_failure_keeps_querydeluxe_authoritative(self):
        query_rows = [
            {
                "tracking_number": "TN-1",
                "picking_name": "OUT/1",
                "pod_link": "https://pod.test/1.pdf",
            }
        ]
        with mock.patch(
            "webapp.odoo_read_backends._querydeluxe_rows", return_value=query_rows
        ) as query, mock.patch(
            "webapp.odoo_read_backends.refresh_reconcile_cache",
            side_effect=RuntimeError("XML-RPC unavailable"),
        ) as refresh, mock.patch(
            "webapp.odoo_read_backends._direct_coverage_ids", return_value=[901]
        ) as coverage, mock.patch(
            "webapp.odoo_read_backends.load_cached_pod_state"
        ) as load_cache:
            state = odoo_read_backends.fetch_pod_read_state(
                self._config("shadow"), config_path="config.test.json"
            )

        self.assertEqual(state["requested_backend"], "shadow")
        self.assertEqual(state["authoritative_source"], "querydeluxe")
        self.assertIs(state["rows"], query_rows)
        self.assertEqual(state["shadow_status"], "unavailable")
        self.assertEqual(
            state["shadow_difference_counts"], {"error_type": "RuntimeError"}
        )
        self.assertEqual(state["coverage_ids"], [901])
        self.assertEqual(state["coverage_status"], "complete")
        query.assert_called_once()
        refresh.assert_called_once()
        coverage.assert_called_once()
        load_cache.assert_not_called()

    def test_querydeluxe_coverage_failure_does_not_replace_authoritative_rows(self):
        query_rows = [
            {
                "tracking_number": "TN-1",
                "picking_name": "OUT/1",
                "pod_link": "https://pod.test/1.pdf",
            }
        ]
        with mock.patch(
            "webapp.odoo_read_backends._querydeluxe_rows", return_value=query_rows
        ), mock.patch(
            "webapp.odoo_read_backends._direct_coverage_ids",
            side_effect=RuntimeError("coverage unavailable"),
        ), mock.patch(
            "webapp.odoo_read_backends.refresh_reconcile_cache"
        ) as refresh, mock.patch(
            "webapp.odoo_read_backends.load_cached_pod_state",
            return_value={"available": False, "generation": 0, "rows": [], "picking_ids": []},
        ):
            state = odoo_read_backends.fetch_pod_read_state(
                self._config("querydeluxe"), config_path="config.test.json"
            )

        self.assertEqual(state["requested_backend"], "querydeluxe")
        self.assertEqual(state["authoritative_source"], "querydeluxe")
        self.assertIs(state["rows"], query_rows)
        self.assertEqual(state["coverage_ids"], [])
        self.assertEqual(state["coverage_status"], "unavailable:RuntimeError")
        self.assertEqual(state["shadow_status"], "")
        refresh.assert_not_called()

    def test_xmlrpc_success_uses_one_refreshed_cache_generation(self):
        cached_rows = [
            {
                "tracking_number": "TN-X",
                "picking_name": "OUT/X",
                "pod_link": "https://pod.test/x.pdf",
            }
        ]
        with mock.patch(
            "webapp.odoo_read_backends.refresh_reconcile_cache",
            return_value={"generation": 3},
        ) as refresh, mock.patch(
            "webapp.odoo_read_backends.load_cached_pod_state",
            return_value={
                "available": True,
                "generation": 3,
                "rows": cached_rows,
                "picking_ids": [811, 812],
            },
        ) as load_cache, mock.patch(
            "webapp.odoo_read_backends._querydeluxe_rows"
        ) as query:
            state = odoo_read_backends.fetch_pod_read_state(
                self._config("xmlrpc"), config_path="config.test.json"
            )

        self.assertEqual(state["requested_backend"], "xmlrpc")
        self.assertEqual(state["authoritative_source"], "xmlrpc")
        self.assertIs(state["rows"], cached_rows)
        self.assertEqual(state["coverage_ids"], [811, 812])
        self.assertEqual(state["coverage_status"], "complete")
        self.assertEqual(state["cache_generation"], 3)
        refresh.assert_called_once()
        load_cache.assert_called_once()
        query.assert_not_called()

    def test_xmlrpc_refresh_failure_propagates_without_querydeluxe_fallback(self):
        with mock.patch(
            "webapp.odoo_read_backends.refresh_reconcile_cache",
            side_effect=RuntimeError("XML-RPC unavailable"),
        ) as refresh, mock.patch(
            "webapp.odoo_read_backends._querydeluxe_rows"
        ) as query, mock.patch(
            "webapp.odoo_read_backends.load_cached_pod_state"
        ) as load_cache:
            with self.assertRaisesRegex(RuntimeError, "XML-RPC unavailable"):
                odoo_read_backends.fetch_pod_read_state(
                    self._config("xmlrpc"), config_path="config.test.json"
                )

        refresh.assert_called_once()
        query.assert_not_called()
        load_cache.assert_not_called()


class OdooPullModeTests(unittest.TestCase):
    @staticmethod
    def _saved_index(backend=""):
        index = ExistingPodIndex()
        index.requested_backend = backend
        return index

    def test_get_index_refreshes_when_saved_backend_does_not_match(self):
        saved = self._saved_index("shadow")
        refreshed = self._saved_index("xmlrpc")
        with mock.patch(
            "webapp.odoo_pull.load_config",
            return_value={"odoo": {"read_backend": "xmlrpc"}},
        ), mock.patch(
            "webapp.odoo_pull.load_saved_index", return_value=saved
        ), mock.patch(
            "webapp.odoo_reconcile_cache.cache_status",
            return_value={"available": False, "generation": 0},
        ), mock.patch(
            "webapp.odoo_pull.fetch_and_save_index", return_value=refreshed
        ) as fetch:
            result = odoo_pull.get_index("config.test.json")

        self.assertIs(result, refreshed)
        fetch.assert_called_once_with("config.test.json")

    def test_get_index_accepts_legacy_metadata_only_for_querydeluxe(self):
        legacy = self._saved_index("")
        with mock.patch(
            "webapp.odoo_pull.load_config", return_value={"odoo": {}}
        ), mock.patch(
            "webapp.odoo_pull.load_saved_index", return_value=legacy
        ), mock.patch("webapp.odoo_pull.fetch_and_save_index") as fetch:
            result = odoo_pull.get_index("config.test.json")

        self.assertIs(result, legacy)
        fetch.assert_not_called()

    def test_get_index_refreshes_legacy_metadata_for_non_querydeluxe_modes(self):
        for backend in ("shadow", "xmlrpc"):
            legacy = self._saved_index("")
            refreshed = self._saved_index(backend)
            with self.subTest(backend=backend), mock.patch(
                "webapp.odoo_pull.load_config",
                return_value={"odoo": {"read_backend": backend}},
            ), mock.patch(
                "webapp.odoo_pull.load_saved_index", return_value=legacy
            ), mock.patch(
                "webapp.odoo_reconcile_cache.cache_status",
                return_value={"available": False, "generation": 0},
            ), mock.patch(
                "webapp.odoo_pull.fetch_and_save_index", return_value=refreshed
            ) as fetch:
                result = odoo_pull.get_index("config.test.json")

            self.assertIs(result, refreshed)
            fetch.assert_called_once_with("config.test.json")

    def test_fetch_saves_index_and_coverage_with_same_generation(self):
        state = {
            "requested_backend": "shadow",
            "authoritative_source": "querydeluxe",
            "rows": [
                {
                    "tracking_number": "TN-1",
                    "picking_name": "OUT/1",
                    "pod_link": "https://pod.test/1.pdf",
                }
            ],
            "coverage_ids": [103, 101, 103],
            "coverage_status": "complete",
            "shadow_status": "match",
            "shadow_difference_counts": {},
            "cache_generation": 3,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "odoo_push" / "odoo_pod_index.json"
            coverage_path = Path(tmpdir) / "stock_tracking_pod_picking_ids.json"
            with mock.patch.object(odoo_pull, "INDEX_FILE", index_path), mock.patch.object(
                odoo_pull, "COVERAGE_FILE", coverage_path
            ), mock.patch.object(
                odoo_pull, "INDEX_LOCK_FILE", Path(tmpdir) / "odoo_push" / ".lock"
            ), mock.patch(
                "webapp.odoo_pull.load_config", return_value={"odoo": {}}
            ), mock.patch(
                "webapp.odoo_read_backends.fetch_pod_read_state", return_value=state
            ) as fetch_state:
                index = odoo_pull.fetch_and_save_index("config.test.json")

            saved_index = json.loads(index_path.read_text(encoding="utf-8"))
            saved_coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

        self.assertTrue(index.generation_id)
        self.assertEqual(saved_index["generation_id"], index.generation_id)
        self.assertEqual(saved_coverage["generation_id"], index.generation_id)
        self.assertEqual(saved_coverage["picking_ids"], [101, 103])
        self.assertEqual(saved_coverage["count"], 2)
        self.assertEqual(saved_index["requested_backend"], "shadow")
        self.assertEqual(saved_index["authoritative_source"], "querydeluxe")
        self.assertEqual(saved_index["shadow_status"], "match")
        self.assertFalse(index_path.with_suffix(".json.tmp").exists())
        self.assertFalse(coverage_path.with_suffix(".json.tmp").exists())
        fetch_state.assert_called_once_with(
            {"odoo": {}}, config_path="config.test.json", refresh_xmlrpc_cache=True
        )

    def test_fetch_preserves_last_good_coverage_when_new_coverage_is_unavailable(self):
        state = {
            "requested_backend": "querydeluxe",
            "authoritative_source": "querydeluxe",
            "rows": [],
            "coverage_ids": [],
            "coverage_status": "unavailable:TimeoutError",
            "shadow_status": "",
            "shadow_difference_counts": {},
            "cache_generation": 0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path = root / "odoo_push" / "odoo_pod_index.json"
            coverage_path = root / "stock_tracking_pod_picking_ids.json"
            coverage_path.write_text(
                json.dumps({"picking_ids": [77], "count": 1, "generation_id": "old"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", root / "odoo_push" / ".lock"),
                mock.patch("webapp.odoo_pull.load_config", return_value={"odoo": {}}),
                mock.patch(
                    "webapp.odoo_read_backends.fetch_pod_read_state",
                    return_value=state,
                ),
            ):
                index = odoo_pull.fetch_and_save_index("config.test.json")

            saved_coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

        self.assertEqual(saved_coverage["picking_ids"], [77])
        self.assertEqual(saved_coverage["generation_id"], "old")
        self.assertEqual(index.coverage_status, "unavailable:TimeoutError")

    def test_get_index_rebuilds_from_newer_cache_without_network_refresh(self):
        saved = self._saved_index("xmlrpc")
        saved.cache_generation = 2
        rebuilt = self._saved_index("xmlrpc")
        rebuilt.cache_generation = 3
        with (
            mock.patch(
                "webapp.odoo_pull.load_config",
                return_value={"odoo": {"read_backend": "xmlrpc"}},
            ),
            mock.patch("webapp.odoo_pull.load_saved_index", return_value=saved),
            mock.patch(
                "webapp.odoo_reconcile_cache.cache_status",
                return_value={"available": True, "generation": 3},
            ),
            mock.patch(
                "webapp.odoo_pull.fetch_and_save_index", return_value=rebuilt
            ) as fetch,
        ):
            result = odoo_pull.get_index("config.test.json")

        self.assertIs(result, rebuilt)
        fetch.assert_called_once_with(
            "config.test.json", refresh_xmlrpc_cache=False
        )


if __name__ == "__main__":
    unittest.main()
