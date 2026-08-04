import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.odoo_push_pool import (
    OdooPushPool,
    build_candidate_fingerprint,
    build_candidate_key,
    resolve_push_pool_settings,
)


UTC = timezone.utc


class OdooPushPoolTests(unittest.TestCase):
    @staticmethod
    def _candidate(*, target_url="https://pod.test/a.pdf", document_uid="doc-1"):
        item = {
            "payload_item_id": "item-1",
            "document_uid": document_uid,
            "identifier_status": "ready",
            "preferred_key_type": "tracking_number",
            "preferred_key": "TRACK-1",
            "source_name": "CMC",
            "source_type": "onedrive",
            "local_path": "/srv/pod/a.pdf",
            # This source URL must never be persisted by the pool.
            "file_url": "https://source.test/a.pdf?token=secret",
        }
        key = build_candidate_key("cmc", "tracking_number", "TRACK-1")
        return {
            "candidate_key": key,
            "provider_identity": "cmc",
            "key_type": "tracking_number",
            "key_value": "TRACK-1",
            "fingerprint": build_candidate_fingerprint(
                provider="cmc",
                key_type="tracking_number",
                key_value="TRACK-1",
                item=item,
                target_url=target_url,
            ),
            "item": item,
        }

    def test_refresh_keeps_completed_candidate_and_url_change_requeues(self):
        now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir:
            pool = OdooPushPool(Path(tmpdir) / "push.sqlite3")
            candidate = self._candidate()
            first = pool.refresh_candidates(
                [candidate], generation="generation-1", now=now
            )
            pool.set_evaluation(
                candidate["candidate_key"],
                state="completed",
                status="cache_exact_complete",
                delay_seconds=3600,
                now=now,
            )

            unchanged = pool.refresh_candidates(
                [candidate], generation="generation-2", now=now + timedelta(minutes=5)
            )
            unchanged_summary = pool.summary(
                generation="generation-2", now=now + timedelta(minutes=5)
            )

            changed_candidate = self._candidate(
                target_url="https://pod.test/replaced.pdf", document_uid="doc-2"
            )
            changed = pool.refresh_candidates(
                [changed_candidate],
                generation="generation-3",
                now=now + timedelta(minutes=10),
            )
            due = pool.due_candidates(
                generation="generation-3", now=now + timedelta(minutes=10)
            )

            self.assertEqual(first["inserted_count"], 1)
            self.assertEqual(unchanged["unchanged_count"], 1)
            self.assertEqual(unchanged_summary["states"], {"completed": 1})
            self.assertEqual(changed["changed_count"], 1)
            self.assertEqual([row["state"] for row in due], ["pending"])
            raw_db = (Path(tmpdir) / "push.sqlite3").read_bytes()
            self.assertNotIn(b"token=secret", raw_db)
            self.assertNotIn(b"https://pod.test/replaced.pdf", raw_db)

    def test_claim_finish_retry_backoff_and_expired_lease_recovery(self):
        now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
        settings = {
            "completed_audit_seconds": 3600,
            "not_found_retry_seconds": 7200,
            "review_retry_seconds": 10800,
            "retry_base_seconds": 60,
            "retry_max_seconds": 600,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            pool = OdooPushPool(Path(tmpdir) / "push.sqlite3")
            candidate = self._candidate()
            pool.refresh_candidates([candidate], generation="g", now=now)
            owner, claimed = pool.claim_pending(
                generation="g", limit=1, lease_seconds=60, now=now
            )
            self.assertEqual(len(claimed), 1)
            self.assertEqual(pool.summary(generation="g", now=now)["states"], {"leased": 1})

            recovered = pool.recover_expired_leases(now=now + timedelta(seconds=61))
            self.assertEqual(recovered, 1)
            owner, claimed = pool.claim_pending(
                generation="g",
                limit=1,
                lease_seconds=60,
                now=now + timedelta(seconds=61),
            )
            pool.finish_claim(
                candidate["candidate_key"],
                lease_owner=owner,
                status="not_found",
                retryable=True,
                settings=settings,
                now=now + timedelta(seconds=61),
            )
            summary = pool.summary(generation="g", now=now + timedelta(seconds=61))
            self.assertEqual(summary["states"], {"retry": 1})
            self.assertEqual(summary["due_count"], 0)
            self.assertEqual(
                len(
                    pool.due_candidates(
                        generation="g",
                        now=now + timedelta(seconds=61, hours=2),
                    )
                ),
                1,
            )

    def test_settings_resolve_relative_path_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.local.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            settings = resolve_push_pool_settings(
                {
                    "odoo_push": {
                        "pool_path": "runtime/custom.sqlite3",
                        "batch_limit": 25,
                    }
                },
                config_path=str(config_path),
            )
            self.assertEqual(
                settings["pool_path"],
                (Path(tmpdir) / "runtime/custom.sqlite3").resolve(),
            )
            self.assertEqual(settings["batch_limit"], 25)
            with self.assertRaisesRegex(ValueError, "odoo_push.batch_limit"):
                resolve_push_pool_settings(
                    {"odoo_push": {"batch_limit": 0}},
                    config_path=str(config_path),
                )

    def test_candidate_fingerprint_changes_with_odoo_target_scope(self):
        item = self._candidate()["item"]
        first = build_candidate_fingerprint(
            provider="cmc",
            key_type="tracking_number",
            key_value="TRACK-1",
            item=item,
            target_url="https://pod.test/a.pdf",
            scope_identity="https://odoo.test|db-a|cmc",
        )
        second = build_candidate_fingerprint(
            provider="cmc",
            key_type="tracking_number",
            key_value="TRACK-1",
            item=item,
            target_url="https://pod.test/a.pdf",
            scope_identity="https://odoo.test|db-b|cmc",
        )
        self.assertNotEqual(first, second)

    def test_consecutive_live_failures_keep_exponential_backoff_after_recheck(self):
        now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
        settings = {
            "completed_audit_seconds": 3600,
            "not_found_retry_seconds": 7200,
            "review_retry_seconds": 10800,
            "retry_base_seconds": 60,
            "retry_max_seconds": 600,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            pool = OdooPushPool(Path(tmpdir) / "push.sqlite3")
            candidate = self._candidate()
            pool.refresh_candidates([candidate], generation="g", now=now)
            owner, _ = pool.claim_pending(
                generation="g", limit=1, lease_seconds=60, now=now
            )
            pool.finish_claim(
                candidate["candidate_key"],
                lease_owner=owner,
                status="failed",
                retryable=True,
                settings=settings,
                now=now,
            )
            first_due = now + timedelta(seconds=60)
            row = pool.due_candidates(generation="g", now=first_due)[0]
            pool.set_evaluation(
                row["candidate_key"],
                state="pending",
                status="tracking_write_required",
                delay_seconds=0,
                now=first_due,
            )
            owner, _ = pool.claim_pending(
                generation="g", limit=1, lease_seconds=60, now=first_due
            )
            pool.finish_claim(
                candidate["candidate_key"],
                lease_owner=owner,
                status="failed",
                retryable=True,
                settings=settings,
                now=first_due,
            )

            self.assertEqual(
                pool.due_candidates(
                    generation="g", now=now + timedelta(seconds=179)
                ),
                [],
            )
            self.assertEqual(
                len(
                    pool.due_candidates(
                        generation="g", now=now + timedelta(seconds=180)
                    )
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
