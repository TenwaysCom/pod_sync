import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_pull
from webapp.odoo_pull import ExistingPodIndex


class ExistingPodIndexTests(unittest.TestCase):
    def test_tracking_key_accumulates_unique_urls_and_keeps_first_winner(self):
        index = ExistingPodIndex()

        index.add_row("TN1", "PICK-1", "https://files.test/first.pdf", "first")
        index.add_row("TN1", "PICK-2", "https://files.test/second.pdf", "second")
        index.add_row("TN1", "PICK-3", "https://files.test/second.pdf", "third")

        info = index.get_pod_info(tracking_number="TN1")
        self.assertEqual(info["picking_name"], "PICK-1")
        self.assertEqual(info["pod_link"], "https://files.test/first.pdf")
        self.assertEqual(info["source_model"], "first")
        self.assertEqual(
            info["pod_links"],
            [
                "https://files.test/first.pdf",
                "https://files.test/second.pdf",
            ],
        )
        self.assertTrue(
            index.has_exact_pod_url(
                "https://files.test/second.pdf", tracking_number="TN1"
            )
        )
        self.assertFalse(
            index.has_exact_pod_url(
                "https://files.test/missing.pdf", tracking_number="TN1"
            )
        )


class IndexGenerationTests(unittest.TestCase):
    def _paths(self, root: Path):
        return (
            root / "odoo_push" / "odoo_pod_index.json",
            root / "stock_tracking_pod_picking_ids.json",
            root / "odoo_push" / ".lock",
        )

    def test_late_older_cache_generation_cannot_replace_newer_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                newer = ExistingPodIndex()
                newer.requested_backend = "shadow"
                newer.cache_generation = 3
                newer.generation_id = "newer"
                newer.coverage_status = "complete"
                newer.add_row("TN-NEW", "OUT/NEW", "https://pod.test/new.pdf")
                odoo_pull.save_index(newer)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps(
                        {
                            "picking_ids": [3],
                            "generation_id": "newer",
                            "cache_generation": 3,
                        }
                    ),
                    encoding="utf-8",
                )

                older = ExistingPodIndex()
                older.requested_backend = "shadow"
                older.cache_generation = 2
                older.generation_id = "older"
                older.coverage_status = "complete"
                older.add_row("TN-OLD", "OUT/OLD", "https://pod.test/old.pdf")
                winner = odoo_pull._commit_index_generation(
                    older,
                    {"coverage_status": "complete", "coverage_ids": [2]},
                )

                saved = odoo_pull.load_saved_index()
                saved_coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

        self.assertEqual(winner.generation_id, "newer")
        self.assertEqual(saved.generation_id, "newer")
        self.assertIn("TN-NEW", saved.by_tracking_number)
        self.assertNotIn("TN-OLD", saved.by_tracking_number)
        self.assertEqual(saved_coverage["generation_id"], "newer")

    def test_rebuilt_cache_epoch_can_replace_higher_legacy_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                legacy = ExistingPodIndex()
                legacy.requested_backend = "xmlrpc"
                legacy.cache_generation = 40
                legacy.generation_id = "legacy-40"
                legacy.coverage_status = "complete"
                legacy.add_row("TN-OLD", "OUT/OLD", "https://pod.test/old.pdf")
                odoo_pull.save_index(legacy)

                rebuilt = ExistingPodIndex()
                rebuilt.requested_backend = "xmlrpc"
                rebuilt.cache_epoch = "20260807T010000000000Z-new"
                rebuilt.cache_generation = 1
                rebuilt.generation_id = "rebuilt-1"
                rebuilt.coverage_status = "complete"
                rebuilt.add_row("TN-NEW", "OUT/NEW", "https://pod.test/new.pdf")
                winner = odoo_pull._commit_index_generation(
                    rebuilt,
                    {"coverage_status": "complete", "coverage_ids": [1]},
                )
                saved = odoo_pull.load_saved_index()

        self.assertEqual(winner.generation_id, "rebuilt-1")
        self.assertEqual(saved.cache_epoch, rebuilt.cache_epoch)
        self.assertIn("TN-NEW", saved.by_tracking_number)
        self.assertNotIn("TN-OLD", saved.by_tracking_number)

    def test_late_older_cache_epoch_cannot_replace_newer_epoch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                newer = ExistingPodIndex()
                newer.requested_backend = "xmlrpc"
                newer.cache_epoch = "20260807T020000000000Z-newer"
                newer.cache_generation = 1
                newer.generation_id = "newer-1"
                newer.coverage_status = "complete"
                odoo_pull.save_index(newer)

                older = ExistingPodIndex()
                older.requested_backend = "xmlrpc"
                older.cache_epoch = "20260807T010000000000Z-older"
                older.cache_generation = 99
                older.generation_id = "older-99"
                older.coverage_status = "complete"
                winner = odoo_pull._commit_index_generation(
                    older,
                    {"coverage_status": "complete", "coverage_ids": [99]},
                )
                saved = odoo_pull.load_saved_index()

        self.assertEqual(winner.generation_id, "newer-1")
        self.assertEqual(saved.cache_epoch, newer.cache_epoch)

    def test_local_marker_clears_after_authority_confirms_exact_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                previous = ExistingPodIndex()
                previous.add_row(
                    "TN-1", "OUT/1", "https://pod.test/old.pdf", "local_push"
                )
                odoo_pull.save_index(previous)

                authoritative = ExistingPodIndex()
                authoritative.requested_backend = "shadow"
                authoritative.cache_generation = 1
                authoritative.generation_id = "authority"
                authoritative.coverage_status = "complete"
                authoritative.add_row(
                    "TN-1", "OUT/1", "https://pod.test/old.pdf", "querydeluxe"
                )
                odoo_pull._commit_index_generation(
                    authoritative,
                    {"coverage_status": "complete", "coverage_ids": [1]},
                )
                saved = odoo_pull.load_saved_index()

        self.assertTrue(
            saved.has_exact_pod_url(
                "https://pod.test/old.pdf", tracking_number="TN-1"
            )
        )
        self.assertEqual(odoo_pull._local_push_markers(saved), set())

    def test_local_push_created_during_fetch_is_merged_before_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                concurrent = ExistingPodIndex()
                concurrent.add_row(
                    "TN-1", "OUT/1", "https://pod.test/concurrent.pdf", "local_push"
                )
                odoo_pull.save_index(concurrent)

                authoritative = ExistingPodIndex()
                authoritative.requested_backend = "shadow"
                authoritative.cache_generation = 1
                authoritative.generation_id = "authority"
                authoritative.coverage_status = "complete"
                authoritative.add_row(
                    "TN-1", "OUT/1", "https://pod.test/current.pdf", "querydeluxe"
                )
                odoo_pull._commit_index_generation(
                    authoritative,
                    {"coverage_status": "complete", "coverage_ids": [1]},
                )
                saved = odoo_pull.load_saved_index()

        self.assertTrue(
            saved.has_exact_pod_url(
                "https://pod.test/concurrent.pdf", tracking_number="TN-1"
            )
        )
        self.assertTrue(
            saved.has_exact_pod_url(
                "https://pod.test/current.pdf", tracking_number="TN-1"
            )
        )

    def test_expected_coverage_generation_rejects_mismatch_and_degraded_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                index = ExistingPodIndex()
                index.generation_id = "current"
                index.coverage_status = "complete"
                odoo_pull.save_index(index)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps({"picking_ids": [1], "generation_id": "other"}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    odoo_pull.load_coverage_picking_ids(
                        expected_generation_id="current"
                    )

                index.coverage_status = "unavailable:TimeoutError"
                odoo_pull.save_index(index)
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    odoo_pull.load_coverage_picking_ids(
                        expected_generation_id="current"
                    )
                self.assertEqual(odoo_pull.load_coverage_picking_ids(), {1})

    def test_local_push_updates_coverage_and_advances_generation_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                index = ExistingPodIndex()
                index.generation_id = "before"
                index.coverage_status = "complete"
                index.cache_generation = 4
                odoo_pull.save_index(index)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps(
                        {
                            "picking_ids": [1],
                            "generation_id": "before",
                            "cache_generation": 4,
                        }
                    ),
                    encoding="utf-8",
                )

                updated = odoo_pull.update_saved_index(
                    lambda value: value.add_row(
                        "TN-2", "OUT/2", "https://pod.test/2.pdf", "local_push"
                    ),
                    coverage_picking_id=2,
                )
                coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

                self.assertNotEqual(updated.generation_id, "before")
                self.assertEqual(updated.coverage_status, "complete")
                self.assertEqual(coverage["picking_ids"], [1, 2])
                self.assertEqual(coverage["generation_id"], updated.generation_id)
                self.assertEqual(
                    odoo_pull.load_coverage_picking_ids(
                        expected_generation_id=updated.generation_id
                    ),
                    {1, 2},
                )

    def test_local_push_without_picking_id_marks_coverage_incomplete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                index = ExistingPodIndex()
                index.generation_id = "before"
                index.coverage_status = "complete"
                odoo_pull.save_index(index)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps({"picking_ids": [1], "generation_id": "before"}),
                    encoding="utf-8",
                )

                updated = odoo_pull.update_saved_index(lambda value: None)

        self.assertNotEqual(updated.generation_id, "before")
        self.assertEqual(updated.coverage_status, "local_push_pending_refresh")

    def test_refresh_commit_preserves_coverage_added_by_concurrent_local_push(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                initial = ExistingPodIndex()
                initial.requested_backend = "shadow"
                initial.cache_generation = 4
                initial.generation_id = "cache-4"
                initial.coverage_status = "complete"
                odoo_pull.save_index(initial)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps(
                        {
                            "picking_ids": [1],
                            "generation_id": "cache-4",
                            "cache_generation": 4,
                        }
                    ),
                    encoding="utf-8",
                )
                odoo_pull.update_saved_index(
                    lambda value: value.add_row(
                        "TN-2", "OUT/2", "https://pod.test/2.pdf", "local_push"
                    ),
                    coverage_picking_id=2,
                )

                fetched = ExistingPodIndex()
                fetched.requested_backend = "shadow"
                fetched.cache_generation = 5
                fetched.generation_id = "cache-5"
                fetched.coverage_status = "complete"
                saved = odoo_pull._commit_index_generation(
                    fetched,
                    {"coverage_status": "complete", "coverage_ids": [1]},
                )
                coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

        self.assertTrue(
            saved.has_exact_pod_url(
                "https://pod.test/2.pdf", tracking_number="TN-2"
            )
        )
        self.assertEqual(saved.local_coverage_picking_ids, [2])
        self.assertEqual(coverage["picking_ids"], [1, 2])
        self.assertEqual(coverage["generation_id"], "cache-5")

    def test_authority_confirmation_clears_pending_url_and_coverage_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path, coverage_path, lock_path = self._paths(root)
            with (
                mock.patch.object(odoo_pull, "INDEX_FILE", index_path),
                mock.patch.object(odoo_pull, "COVERAGE_FILE", coverage_path),
                mock.patch.object(odoo_pull, "INDEX_LOCK_FILE", lock_path),
            ):
                pending = ExistingPodIndex()
                pending.requested_backend = "xmlrpc"
                pending.cache_generation = 5
                pending.generation_id = "local"
                pending.coverage_status = "complete"
                pending.local_coverage_picking_ids = [2]
                pending.add_row(
                    "TN-2", "OUT/2", "https://pod.test/2.pdf", "local_push"
                )
                odoo_pull.save_index(pending)
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                coverage_path.write_text(
                    json.dumps({"picking_ids": [1, 2], "generation_id": "local"}),
                    encoding="utf-8",
                )

                confirmed = ExistingPodIndex()
                confirmed.requested_backend = "xmlrpc"
                confirmed.cache_generation = 6
                confirmed.generation_id = "cache-6"
                confirmed.coverage_status = "complete"
                confirmed.add_row(
                    "TN-2", "OUT/2", "https://pod.test/2.pdf", "xmlrpc"
                )
                saved = odoo_pull._commit_index_generation(
                    confirmed,
                    {"coverage_status": "complete", "coverage_ids": [1, 2]},
                )

        self.assertEqual(saved.local_coverage_picking_ids, [])
        self.assertEqual(odoo_pull._local_push_markers(saved), set())


class ExistingPodIndexAdditionalTests(unittest.TestCase):

    def test_picking_key_accumulates_unique_urls_and_keeps_last_winner(self):
        index = ExistingPodIndex()

        index.add_row("TN1", "PICK-1", "https://files.test/first.pdf", "first")
        index.add_row("TN2", "PICK-1", "https://files.test/second.pdf", "second")
        index.add_row("TN3", "PICK-1", "https://files.test/second.pdf", "third")

        info = index.get_pod_info(picking_name="PICK-1")
        self.assertEqual(info["tracking_number"], "TN3")
        self.assertEqual(info["pod_link"], "https://files.test/second.pdf")
        self.assertEqual(info["source_model"], "third")
        self.assertEqual(
            info["pod_links"],
            [
                "https://files.test/first.pdf",
                "https://files.test/second.pdf",
            ],
        )
        self.assertTrue(
            index.has_exact_pod_url(
                "https://files.test/first.pdf", picking_name="PICK-1"
            )
        )

    def test_from_dict_backfills_url_lists_for_legacy_index(self):
        legacy = {
            "by_tracking_number": {
                "TN1": {
                    "picking_name": "PICK-1",
                    "pod_link": "https://files.test/tracking.pdf",
                    "source_model": "querydeluxe",
                }
            },
            "by_picking_name": {
                "PICK-1": {
                    "tracking_number": "TN1",
                    "pod_link": "https://files.test/picking.pdf",
                    "source_model": "querydeluxe",
                }
            },
            "total_loaded": 1,
            "fetched_at": "2026-07-17T00:00:00Z",
            "updated_at": "",
        }

        index = ExistingPodIndex.from_dict(legacy)

        self.assertEqual(
            index.get_pod_info(tracking_number="TN1")["pod_links"],
            ["https://files.test/tracking.pdf"],
        )
        self.assertEqual(
            index.get_pod_info(picking_name="PICK-1")["pod_links"],
            ["https://files.test/picking.pdf"],
        )
        self.assertTrue(
            index.has_exact_pod_url(
                "https://files.test/tracking.pdf", tracking_number="TN1"
            )
        )
        self.assertEqual(index.total_loaded, 1)
        self.assertEqual(index.fetched_at, "2026-07-17T00:00:00Z")

    def test_from_dict_preserves_accumulated_unique_urls(self):
        data = {
            "by_tracking_number": {
                "TN1": {
                    "picking_name": "PICK-1",
                    "pod_link": "https://files.test/first.pdf",
                    "pod_links": [
                        "https://files.test/first.pdf",
                        "https://files.test/second.pdf",
                        "https://files.test/second.pdf",
                    ],
                    "source_model": "querydeluxe",
                }
            }
        }

        index = ExistingPodIndex.from_dict(data)

        self.assertEqual(
            index.get_pod_info(tracking_number="TN1")["pod_links"],
            [
                "https://files.test/first.pdf",
                "https://files.test/second.pdf",
            ],
        )
        self.assertTrue(
            index.has_exact_pod_url(
                "https://files.test/second.pdf", tracking_number="TN1"
            )
        )


if __name__ == "__main__":
    unittest.main()
