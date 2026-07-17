import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clients.westerman_api.pool import WestermanPodPool
from phase1a.phase1a_pod.westerman_gate import evaluate_westerman_pool_gate


T0 = "2026-07-17T01:00:00Z"


class WestermanPodPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "runtime" / "westerman" / "pool.sqlite3"
        self.pool = WestermanPodPool(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_candidate_refresh_status_is_persisted_for_fail_closed_gates(self):
        self.assertEqual(
            self.pool.candidate_refresh_status(),
            {"status": "unknown", "updated_at": None},
        )
        self.pool.set_candidate_refresh_status("running", now=T0)
        self.assertEqual(
            self.pool.candidate_refresh_status(),
            {"status": "running", "updated_at": T0},
        )
        self.pool.set_candidate_refresh_status(
            "complete", now="2026-07-17T01:05:00Z"
        )
        self.assertEqual(
            self.pool.summary(now=T0)["candidate_refresh"],
            {"status": "complete", "updated_at": "2026-07-17T01:05:00Z"},
        )

    def test_v3_pool_migration_adds_failure_counter_and_cools_legacy_failures(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(lease_owner="worker-a", now=T0)
        self.pool.mark_failed(
            reference,
            lease_owner="worker-a",
            error_code="INVALID_DOCUMENT",
            now=T0,
        )

        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE pod_pull_pool SET next_check_at = NULL WHERE shipment_reference = ?",
                (reference,),
            )
            connection.execute(
                "ALTER TABLE pod_pull_pool DROP COLUMN consecutive_failure_count"
            )
            connection.execute(
                "ALTER TABLE pod_pull_pool DROP COLUMN deferred_terminal_state"
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        finally:
            connection.close()

        migrated = WestermanPodPool(self.db_path)
        row = migrated.get_item(reference)
        self.assertEqual(row["pull_state"], "failed")
        self.assertEqual(row["consecutive_failure_count"], 0)
        self.assertEqual(row["next_check_at"], "2026-07-18T01:00:00Z")
        self.assertIsNone(row["deferred_terminal_state"])

        migrated.upsert_candidates(
            [reference],
            reactivate_failed=True,
            now="2026-07-18T00:59:59Z",
        )
        self.assertEqual(migrated.get_item(reference)["pull_state"], "failed")
        migrated.upsert_candidates(
            [reference],
            reactivate_failed=True,
            now="2026-07-18T01:00:00Z",
        )
        self.assertEqual(migrated.get_item(reference)["pull_state"], "pending")

    def test_upsert_is_idempotent_and_preserves_processing_state(self):
        count = self.pool.upsert_candidates(
            [
                {
                    "shipment_reference": "WDB2B/OUT/01391",
                    "odoo_picking_id": 391,
                    "tracking_number": "U260203465",
                    "picking_state": "assigned",
                    "wms_status": "ref_back",
                },
                {
                    "shipment_reference": "WDB2B/OUT/01391",
                    "tracking_number": "U260203465",
                    "picking_state": "done",
                },
            ],
            now=T0,
        )
        self.assertEqual(count, 1)

        claimed = self.pool.claim_due(
            lease_owner="worker-a",
            limit=1,
            lease_seconds=60,
            now=T0,
        )
        self.assertEqual([item["shipment_reference"] for item in claimed], ["WDB2B/OUT/01391"])
        self.assertEqual(claimed[0]["attempt_count"], 1)
        self.assertTrue(
            self.pool.mark_waiting(
                "WDB2B/OUT/01391",
                lease_owner="worker-a",
                next_check_at="2026-07-18T01:00:00+00:00",
                http_status=200,
                now=T0,
            )
        )

        count = self.pool.upsert_candidates(
            [
                {
                    "shipment_reference": "WDB2B/OUT/01391",
                    "odoo_picking_id": 391,
                    "picking_state": "done",
                    "wms_status": "pod",
                }
            ],
            now="2026-07-17T02:00:00Z",
        )
        self.assertEqual(count, 1)
        item = self.pool.get_item("WDB2B/OUT/01391")
        self.assertEqual(item["first_seen_at"], T0)
        self.assertEqual(item["pull_state"], "waiting_pod")
        self.assertEqual(item["attempt_count"], 1)
        self.assertEqual(item["next_check_at"], "2026-07-18T01:00:00Z")
        self.assertEqual(item["picking_state"], "done")
        self.assertEqual(item["wms_status"], "pod")
        self.assertEqual(item["tracking_number"], "U260203465")

    def test_claim_due_is_atomic_and_never_returns_same_item_twice(self):
        self.pool.upsert_candidates(["WDB2B/OUT/01391"], now=T0)
        barrier = threading.Barrier(2)

        def claim(owner):
            barrier.wait()
            return self.pool.claim_due(
                lease_owner=owner,
                limit=1,
                lease_seconds=60,
                now=T0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b")))

        claimed = [item for result in results for item in result]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["shipment_reference"], "WDB2B/OUT/01391")
        self.assertEqual(claimed[0]["attempt_count"], 1)
        self.assertEqual(self.pool.summary(now=T0)["leased_count"], 1)

    def test_stale_lease_recovers_and_old_worker_cannot_overwrite_new_lease(self):
        self.pool.upsert_candidates(["WDB2B/OUT/01391"], now=T0)
        first = self.pool.claim_due(
            lease_owner="worker-old",
            lease_seconds=60,
            now=T0,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(
            self.pool.claim_due(
                lease_owner="worker-new",
                lease_seconds=60,
                now="2026-07-17T01:00:59Z",
            ),
            [],
        )

        self.assertEqual(
            self.pool.recover_stale_leases(now="2026-07-17T01:01:00Z"),
            1,
        )
        second = self.pool.claim_due(
            lease_owner="worker-new",
            lease_seconds=60,
            now="2026-07-17T01:01:00Z",
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["attempt_count"], 2)
        self.assertEqual(second[0]["consecutive_failure_count"], 1)

        self.assertFalse(
            self.pool.mark_failed(
                "WDB2B/OUT/01391",
                lease_owner="worker-old",
                error_code="STALE_WORKER",
                now="2026-07-17T01:01:01Z",
            )
        )
        self.assertTrue(
            self.pool.mark_waiting(
                "WDB2B/OUT/01391",
                lease_owner="worker-new",
                wait_seconds=3600,
                http_status=200,
                now="2026-07-17T01:01:01Z",
            )
        )
        item = self.pool.get_item("WDB2B/OUT/01391")
        self.assertEqual(item["pull_state"], "waiting_pod")
        self.assertEqual(item["next_check_at"], "2026-07-17T02:01:01Z")
        self.assertEqual(item["consecutive_failure_count"], 0)
        self.assertIsNone(item["lease_owner"])

    def test_inflight_download_cannot_revive_candidate_removed_by_complete_refresh(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(
            lease_owner="worker-a",
            lease_seconds=3600,
            now=T0,
        )

        # A concurrent complete refresh no longer sees this picking. Preserve
        # the lease, but record the terminal decision for worker finalization.
        self.assertEqual(
            self.pool.expire_missing_candidates(
                [],
                now="2026-07-17T01:01:00Z",
            ),
            0,
        )
        pending = self.pool.get_item(reference)
        self.assertEqual(pending["pull_state"], "checking")
        self.assertEqual(pending["lease_owner"], "worker-a")
        self.assertEqual(pending["deferred_terminal_state"], "expired")

        self.assertTrue(
            self.pool.mark_downloaded(
                reference,
                lease_owner="worker-a",
                document_count=1,
                http_status=200,
                now="2026-07-17T01:02:00Z",
            )
        )
        row = self.pool.get_item(reference)
        self.assertEqual(row["pull_state"], "expired")
        self.assertEqual(row["push_state"], "needs_review")
        self.assertEqual(row["last_error_code"], "ODOO_CANDIDATE_EXPIRED")
        self.assertIsNone(row["deferred_terminal_state"])
        self.assertIsNone(row["lease_owner"])

        self.pool.set_candidate_refresh_status(
            "complete",
            now="2026-07-17T01:01:00Z",
        )
        gate = evaluate_westerman_pool_gate(
            self.db_path,
            now="2026-07-17T01:02:00Z",
        )
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["eligible_references"], set())

    def test_inflight_download_cannot_revive_candidate_completed_in_odoo(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(
            lease_owner="worker-a",
            lease_seconds=3600,
            now=T0,
        )

        self.assertTrue(
            self.pool.mark_completed(
                reference,
                now="2026-07-17T01:01:00Z",
            )
        )
        pending = self.pool.get_item(reference)
        self.assertEqual(pending["pull_state"], "checking")
        self.assertEqual(pending["lease_owner"], "worker-a")
        self.assertEqual(pending["deferred_terminal_state"], "completed")

        self.assertTrue(
            self.pool.mark_downloaded(
                reference,
                lease_owner="worker-a",
                document_count=1,
                http_status=200,
                now="2026-07-17T01:02:00Z",
            )
        )
        row = self.pool.get_item(reference)
        self.assertEqual(row["pull_state"], "completed")
        self.assertEqual(row["push_state"], "completed")
        self.assertEqual(row["completed_at"], "2026-07-17T01:02:00Z")
        self.assertIsNone(row["deferred_terminal_state"])
        self.assertIsNone(row["lease_owner"])

    def test_stale_recovery_applies_deferred_terminal_state(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(
            lease_owner="dead-worker",
            lease_seconds=60,
            now=T0,
        )
        self.pool.expire_missing_candidates(
            [],
            now="2026-07-17T01:00:30Z",
        )

        self.assertEqual(
            self.pool.recover_stale_leases(now="2026-07-17T01:01:00Z"),
            1,
        )
        row = self.pool.get_item(reference)
        self.assertEqual(row["pull_state"], "expired")
        self.assertEqual(row["push_state"], "not_ready")
        self.assertEqual(row["last_error_code"], "ODOO_CANDIDATE_EXPIRED")
        self.assertIsNone(row["deferred_terminal_state"])
        self.assertIsNone(row["lease_owner"])

    def test_stale_recovery_applies_deferred_completed_state(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(
            lease_owner="dead-worker",
            lease_seconds=60,
            now=T0,
        )
        self.pool.mark_completed(
            reference,
            now="2026-07-17T01:00:30Z",
        )

        self.assertEqual(
            self.pool.recover_stale_leases(now="2026-07-17T01:01:00Z"),
            1,
        )
        row = self.pool.get_item(reference)
        self.assertEqual(row["pull_state"], "completed")
        self.assertEqual(row["push_state"], "completed")
        self.assertEqual(row["completed_at"], "2026-07-17T01:01:00Z")
        self.assertIsNone(row["deferred_terminal_state"])
        self.assertIsNone(row["lease_owner"])

    def test_wait_retry_download_fail_and_complete_transitions(self):
        self.pool.upsert_candidates(
            ["WAIT", "DOWNLOAD", "FAIL"],
            now=T0,
        )

        claimed = self.pool.claim_due(
            lease_owner="worker-a",
            limit=3,
            lease_seconds=300,
            now=T0,
        )
        self.assertEqual({item["shipment_reference"] for item in claimed}, {"WAIT", "DOWNLOAD", "FAIL"})

        self.assertTrue(
            self.pool.mark_waiting(
                "WAIT",
                lease_owner="worker-a",
                next_check_at="2026-07-17T02:00:00Z",
                document_count=0,
                http_status=200,
                now=T0,
            )
        )
        self.assertTrue(
            self.pool.mark_downloaded(
                "DOWNLOAD",
                lease_owner="worker-a",
                document_count=1,
                http_status=200,
                now=T0,
            )
        )
        self.assertTrue(
            self.pool.mark_failed(
                "FAIL",
                lease_owner="worker-a",
                error_code="INVALID_DOCUMENT",
                http_status=422,
                now=T0,
            )
        )

        self.assertTrue(self.pool.mark_completed("DOWNLOAD", now="2026-07-17T01:05:00Z"))
        completed = self.pool.get_item("DOWNLOAD")
        self.assertEqual(completed["pull_state"], "completed")
        self.assertEqual(completed["push_state"], "completed")
        self.assertEqual(completed["document_count"], 1)
        self.assertEqual(completed["last_http_status"], 200)
        self.assertEqual(completed["downloaded_at"], T0)
        self.assertEqual(completed["completed_at"], "2026-07-17T01:05:00Z")

        waiting = self.pool.claim_due(
            lease_owner="worker-b",
            limit=1,
            lease_seconds=300,
            now="2026-07-17T02:00:00Z",
        )
        self.assertEqual([item["shipment_reference"] for item in waiting], ["WAIT"])
        self.assertTrue(
            self.pool.mark_retry(
                "WAIT",
                lease_owner="worker-b",
                error_code="HTTP_503",
                next_check_at="2026-07-17T02:30:00+00:00",
                http_status=503,
                now="2026-07-17T02:00:00Z",
            )
        )
        retried = self.pool.get_item("WAIT")
        self.assertEqual(retried["pull_state"], "retry")
        self.assertEqual(retried["next_check_at"], "2026-07-17T02:30:00Z")
        self.assertEqual(retried["last_error_code"], "HTTP_503")
        self.assertEqual(retried["attempt_count"], 2)
        self.assertEqual(retried["consecutive_failure_count"], 1)
        self.assertEqual(self.pool.get_item("FAIL")["consecutive_failure_count"], 1)

        summary = self.pool.summary(now="2026-07-17T03:00:00Z")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["pull_states"], {"completed": 1, "failed": 1, "retry": 1})
        self.assertEqual(summary["due_count"], 1)
        self.assertEqual(
            [item["shipment_reference"] for item in self.pool.list_items(pull_state="retry")],
            ["WAIT"],
        )

    def test_failed_candidate_reactivates_only_after_cooldown_on_complete_refresh(self):
        reference = "WDB2B/OUT/01391"
        self.pool.upsert_candidates([reference], now=T0)
        self.pool.claim_due(lease_owner="worker-a", now=T0)
        self.assertTrue(
            self.pool.mark_failed(
                reference,
                lease_owner="worker-a",
                error_code="INVALID_DOCUMENT",
                retry_seconds=3600,
                now=T0,
            )
        )
        failed = self.pool.get_item(reference)
        self.assertEqual(failed["pull_state"], "failed")
        self.assertEqual(failed["next_check_at"], "2026-07-17T02:00:00Z")
        self.assertEqual(failed["consecutive_failure_count"], 1)
        self.assertEqual(
            self.pool.claim_due(
                lease_owner="worker-b",
                now="2026-07-18T00:00:00Z",
            ),
            [],
        )

        # Even a complete refresh cannot reactivate the row before cooldown.
        self.pool.upsert_candidates(
            [reference],
            reactivate_failed=True,
            now="2026-07-17T01:59:59Z",
        )
        self.assertEqual(self.pool.get_item(reference)["pull_state"], "failed")

        # A truncated/incomplete refresh never reactivates a failed row,
        # including after the timestamp has elapsed.
        self.pool.upsert_candidates(
            [reference],
            reactivate_failed=False,
            now="2026-07-17T02:00:00Z",
        )
        self.assertEqual(self.pool.get_item(reference)["pull_state"], "failed")

        self.pool.upsert_candidates(
            [reference],
            reactivate_failed=True,
            now="2026-07-17T02:00:01Z",
        )
        reactivated = self.pool.get_item(reference)
        self.assertEqual(reactivated["pull_state"], "pending")
        self.assertEqual(reactivated["push_state"], "not_ready")
        self.assertEqual(reactivated["next_check_at"], "2026-07-17T02:00:01Z")
        self.assertEqual(reactivated["consecutive_failure_count"], 1)
        self.assertIsNone(reactivated["last_error_code"])

        claimed = self.pool.claim_due(
            lease_owner="worker-c",
            now="2026-07-17T02:00:01Z",
        )
        self.assertEqual(len(claimed), 1)
        self.assertTrue(
            self.pool.mark_waiting(
                reference,
                lease_owner="worker-c",
                now="2026-07-17T02:00:01Z",
            )
        )
        self.assertEqual(
            self.pool.get_item(reference)["consecutive_failure_count"],
            0,
        )

    def test_odoo_completion_is_terminal_even_when_pool_was_still_waiting(self):
        self.pool.upsert_candidates(["WDB2B/OUT/01391"], now=T0)
        self.pool.claim_due(lease_owner="worker-a", now=T0)
        self.pool.mark_waiting(
            "WDB2B/OUT/01391",
            lease_owner="worker-a",
            wait_seconds=3600,
            now=T0,
        )

        self.assertTrue(
            self.pool.mark_completed("WDB2B/OUT/01391", now="2026-07-17T01:05:00Z")
        )
        item = self.pool.get_item("WDB2B/OUT/01391")
        self.assertEqual(item["pull_state"], "completed")
        self.assertEqual(item["push_state"], "completed")
        self.assertIsNone(item["next_check_at"])

    def test_complete_candidate_refresh_expires_absent_rows_without_touching_leases(self):
        self.pool.upsert_candidates(["ACTIVE", "STALE", "DOWNLOADED", "LEASED"], now=T0)
        claimed = self.pool.claim_due(
            lease_owner="worker-a",
            limit=4,
            lease_seconds=3600,
            now=T0,
        )
        self.assertEqual(len(claimed), 4)
        self.pool.mark_waiting("ACTIVE", lease_owner="worker-a", now=T0)
        self.pool.mark_waiting("STALE", lease_owner="worker-a", now=T0)
        self.pool.mark_downloaded("DOWNLOADED", lease_owner="worker-a", now=T0)

        expired = self.pool.expire_missing_candidates(["ACTIVE"], now=T0)

        self.assertEqual(expired, 2)
        self.assertEqual(self.pool.get_item("ACTIVE")["pull_state"], "waiting_pod")
        self.assertEqual(self.pool.get_item("STALE")["pull_state"], "expired")
        downloaded = self.pool.get_item("DOWNLOADED")
        self.assertEqual(downloaded["pull_state"], "expired")
        self.assertEqual(downloaded["push_state"], "needs_review")
        leased = self.pool.get_item("LEASED")
        self.assertEqual(leased["pull_state"], "checking")
        self.assertEqual(leased["deferred_terminal_state"], "expired")

        self.pool.upsert_candidates(["STALE"], now="2026-07-18T00:00:00Z")
        reactivated = self.pool.get_item("STALE")
        self.assertEqual(reactivated["pull_state"], "pending")
        self.assertEqual(reactivated["push_state"], "not_ready")
        self.assertEqual(reactivated["next_check_at"], "2026-07-18T00:00:00Z")
        self.assertIsNone(reactivated["last_error_code"])

    def test_error_field_accepts_codes_but_rejects_free_form_messages(self):
        self.pool.upsert_candidates(["WDB2B/OUT/01391"], now=T0)
        self.pool.claim_due(lease_owner="worker-a", now=T0)

        with self.assertRaisesRegex(ValueError, "operational code"):
            self.pool.mark_retry(
                "WDB2B/OUT/01391",
                lease_owner="worker-a",
                error_code="request failed for customer@example.test",
                now=T0,
            )
        item = self.pool.get_item("WDB2B/OUT/01391")
        self.assertEqual(item["pull_state"], "checking")
        self.assertIsNone(item["last_error_code"])


if __name__ == "__main__":
    unittest.main()
