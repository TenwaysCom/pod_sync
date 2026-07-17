import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.westerman_api import probe, sync
from clients.westerman_api.pool import WestermanPodPool
from phase1a.phase1a_pod.westerman_gate import evaluate_westerman_pool_gate


class WestermanBatchSyncTests(unittest.TestCase):
    def _config(self, root):
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "download": {"base_dir": str(root / "pod")},
                    "westerman_api": {
                        "enabled": True,
                        "api_mode": "indicium",
                        "base_url": "https://westerman.example.test/indicium/api",
                        "api_key": "test-token",
                        "warehouse": "Westerman",
                        "pool_path": str(root / "pool.sqlite3"),
                        "pool_batch_limit": 100,
                        "pool_workers": 3,
                    },
                    "odoo": {
                        "url": "https://odoo.example.test",
                        "db": "db",
                        "uid": 9,
                        "api_key": "odoo-token",
                    },
                }
            ),
            encoding="utf-8",
        )
        return config_path

    @staticmethod
    def _refresh(*references):
        return {
            "scanned_count": len(references),
            "candidate_count": len(references),
            "already_completed_count": 0,
            "completed_references": [],
            "candidates": [
                {
                    "shipment_reference": reference,
                    "odoo_picking_id": index,
                    "picking_state": "done",
                    "wms_status": "ref_back",
                }
                for index, reference in enumerate(references, start=1)
            ],
        }

    def test_downloaded_and_no_pod_shipments_reach_distinct_terminal_states(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            calls = []

            def candidate_fetcher(_full_cfg):
                refresh = self._refresh("WDB2B/OUT/01391", "WDB2B/OUT/01436")
                refresh["candidates"][0]["tracking_number"] = "U260203465"
                return refresh

            def probe_runner(_args, _source, _download, reference, **kwargs):
                calls.append(reference)
                if reference.endswith("01391"):
                    self.assertEqual(kwargs["fallback_tracking_number"], "U260203465")
                    return {
                        "http_status": 200,
                        "document_count": 1,
                        "pod_document_count": 1,
                        "download_candidate_count": 1,
                        "downloaded_count": 1,
                        "already_downloaded_count": 0,
                        "failed_documents": [],
                    }
                return {
                    "http_status": 200,
                    "document_count": 0,
                    "pod_document_count": 0,
                    "download_candidate_count": 0,
                    "downloaded_count": 0,
                    "already_downloaded_count": 0,
                    "failed_documents": [],
                }

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=2,
                candidate_fetcher=candidate_fetcher,
                probe_runner=probe_runner,
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["candidate_refresh_status"], "complete")
            self.assertEqual(result["candidate_count"], 2)
            self.assertEqual(result["checked_count"], 2)
            self.assertEqual(result["downloaded_shipment_count"], 1)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual(result["waiting_pod_count"], 1)
            self.assertEqual(set(calls), {"WDB2B/OUT/01391", "WDB2B/OUT/01436"})

            pool = WestermanPodPool(root / "pool.sqlite3")
            self.assertEqual(pool.get_item("WDB2B/OUT/01391")["pull_state"], "downloaded")
            waiting = pool.get_item("WDB2B/OUT/01436")
            self.assertEqual(waiting["pull_state"], "waiting_pod")
            self.assertIsNotNone(waiting["next_check_at"])

            second, second_exit = sync.run_sync(
                config_path=config_path,
                skip_candidate_refresh=True,
                probe_runner=lambda *_args, **_kwargs: self.fail("no item should be due"),
                session_factory=lambda _cfg: object(),
            )
            self.assertEqual(second_exit, 0)
            self.assertEqual(second["due_count"], 0)
            self.assertEqual(second["checked_count"], 0)
            self.assertEqual(pool.candidate_refresh_status()["status"], "skipped")

    def test_transient_lookup_failure_is_retried_with_safe_error_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)

            def probe_runner(*_args, **_kwargs):
                raise probe.WestermanError(
                    "upstream unavailable",
                    error_code="get_order_info_http_error",
                    http_status=503,
                    retryable=True,
                )

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("WDB2B/OUT/01436"),
                probe_runner=probe_runner,
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["retry_count"], 1)
            row = WestermanPodPool(root / "pool.sqlite3").get_item("WDB2B/OUT/01436")
            self.assertEqual(row["pull_state"], "retry")
            self.assertEqual(row["last_http_status"], 503)
            self.assertEqual(row["last_error_code"], "LOOKUP_RETRY_GET_ORDER_INFO_HTTP_ERROR")
            self.assertEqual(row["consecutive_failure_count"], 1)
            self.assertNotIn("upstream unavailable", json.dumps(row))

    def test_retry_backoff_uses_consecutive_failures_not_historical_attempts(self):
        settings = {
            "retry_base_seconds": 900,
            "retry_max_seconds": 86400,
        }
        pool = mock.Mock()
        pool.mark_retry.return_value = True

        def fail_probe(*_args, **_kwargs):
            raise probe.WestermanError(
                "temporary",
                error_code="temporary",
                retryable=True,
            )

        outcome = sync._process_claim(
            {
                "shipment_reference": "WDB2B/OUT/01391",
                "attempt_count": 99,
                "consecutive_failure_count": 0,
                "tracking_number": None,
            },
            pool=pool,
            lease_owner="worker-a",
            source_cfg={},
            download_cfg={},
            settings=settings,
            auth_event=threading.Event(),
            session_getter=lambda: object(),
            probe_runner=fail_probe,
        )

        self.assertEqual(outcome["outcome"], "retry")
        self.assertEqual(pool.mark_retry.call_args.kwargs["retry_seconds"], 900)

    def test_permanent_partial_document_failure_allows_valid_pod_in_current_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("WDB2B/OUT/01391"),
                probe_runner=lambda *_args, **_kwargs: {
                    "http_status": 200,
                    "document_count": 2,
                    "pod_document_count": 2,
                    "download_candidate_count": 2,
                    "downloaded_count": 1,
                    "already_downloaded_count": 0,
                    "failed_documents": [
                        {
                            "document_id": "invalid-doc",
                            "retryable": False,
                            "auth_error": False,
                        }
                    ],
                },
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["partial_document_failure_count"], 1)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual(result["failed_count"], 0)
            row = WestermanPodPool(root / "pool.sqlite3").get_item("WDB2B/OUT/01391")
            self.assertEqual(row["pull_state"], "downloaded")
            self.assertEqual(row["push_state"], "pending")
            self.assertEqual(row["last_error_code"], "PARTIAL_DOCUMENT_FAILURE")
            self.assertEqual(row["consecutive_failure_count"], 0)

    def test_retryable_partial_document_failure_allows_valid_pod_in_current_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("WDB2B/OUT/01391"),
                probe_runner=lambda *_args, **_kwargs: {
                    "http_status": 200,
                    "document_count": 2,
                    "pod_document_count": 2,
                    "download_candidate_count": 2,
                    "downloaded_count": 1,
                    "already_downloaded_count": 0,
                    "failed_documents": [
                        {
                            "document_id": "temporary-doc",
                            "http_status": 503,
                            "retryable": True,
                            "auth_error": False,
                        }
                    ],
                },
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["partial_document_failure_count"], 1)
            self.assertEqual(result["downloaded_shipment_count"], 1)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual(result["retry_count"], 0)
            row = WestermanPodPool(root / "pool.sqlite3").get_item(
                "WDB2B/OUT/01391"
            )
            self.assertEqual(row["pull_state"], "downloaded")
            self.assertEqual(row["push_state"], "pending")
            self.assertEqual(row["last_http_status"], 503)
            self.assertEqual(row["last_error_code"], "PARTIAL_DOCUMENT_FAILURE")

    def test_auth_partial_download_is_ready_while_later_items_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            calls = []

            def probe_runner(_args, _source, _download, reference, **_kwargs):
                calls.append(reference)
                self.assertEqual(reference, "A")
                return {
                    "http_status": 200,
                    "document_count": 2,
                    "pod_document_count": 2,
                    "download_candidate_count": 2,
                    "downloaded_count": 1,
                    "already_downloaded_count": 0,
                    "failed_documents": [
                        {
                            "document_id": "unauthorized-doc",
                            "http_status": 401,
                            "retryable": False,
                            "auth_error": True,
                        }
                    ],
                }

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("A", "B"),
                probe_runner=probe_runner,
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 3)
            self.assertEqual(result["status"], "degraded")
            self.assertTrue(result["provider_auth_failed"])
            self.assertEqual(result["partial_document_failure_count"], 1)
            self.assertEqual(result["downloaded_shipment_count"], 1)
            self.assertEqual(result["auth_blocked_count"], 1)
            self.assertEqual(result["retry_count"], 1)
            self.assertEqual(calls, ["A"])

            pool = WestermanPodPool(root / "pool.sqlite3")
            first = pool.get_item("A")
            self.assertEqual(first["pull_state"], "downloaded")
            self.assertEqual(first["push_state"], "pending")
            self.assertEqual(first["last_error_code"], "PARTIAL_DOCUMENT_FAILURE")
            second = pool.get_item("B")
            self.assertEqual(second["pull_state"], "retry")
            self.assertEqual(second["push_state"], "not_ready")
            self.assertEqual(second["last_error_code"], "AUTH_BLOCKED")

    def test_complete_refresh_requeues_cooled_failed_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            pool = WestermanPodPool(root / "pool.sqlite3")
            pool.upsert_candidates(
                ["WDB2B/OUT/01391"],
                now="2020-01-01T00:00:00Z",
            )
            pool.claim_due(
                lease_owner="seed-worker",
                now="2020-01-01T00:00:00Z",
            )
            pool.mark_failed(
                "WDB2B/OUT/01391",
                lease_owner="seed-worker",
                error_code="INVALID_DOCUMENT",
                retry_seconds=60,
                now="2020-01-01T00:00:00Z",
            )

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("WDB2B/OUT/01391"),
                probe_runner=lambda *_args, **_kwargs: {
                    "http_status": 200,
                    "document_count": 0,
                    "downloaded_count": 0,
                    "already_downloaded_count": 0,
                    "failed_documents": [],
                },
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["candidate_refresh_status"], "complete")
            self.assertEqual(result["checked_count"], 1)
            row = pool.get_item("WDB2B/OUT/01391")
            self.assertEqual(row["pull_state"], "waiting_pod")
            self.assertEqual(row["consecutive_failure_count"], 0)

    def test_truncated_refresh_does_not_requeue_cooled_failed_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            pool = WestermanPodPool(root / "pool.sqlite3")
            pool.upsert_candidates(
                ["WDB2B/OUT/01391"],
                now="2020-01-01T00:00:00Z",
            )
            pool.claim_due(
                lease_owner="seed-worker",
                now="2020-01-01T00:00:00Z",
            )
            pool.mark_failed(
                "WDB2B/OUT/01391",
                lease_owner="seed-worker",
                error_code="INVALID_DOCUMENT",
                retry_seconds=60,
                now="2020-01-01T00:00:00Z",
            )

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: {
                    **self._refresh("WDB2B/OUT/01391"),
                    "scan_truncated": True,
                },
                probe_runner=lambda *_args, **_kwargs: self.fail(
                    "truncated refresh must not reactivate failed work"
                ),
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["candidate_refresh_status"], "truncated")
            self.assertEqual(result["due_count"], 0)
            row = pool.get_item("WDB2B/OUT/01391")
            self.assertEqual(row["pull_state"], "failed")
            self.assertEqual(row["consecutive_failure_count"], 1)

    def test_missing_enabled_flag_keeps_sync_disabled_without_pool_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["westerman_api"].pop("enabled")
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            result, exit_code = sync.run_sync(
                config_path=config_path,
                candidate_fetcher=lambda _cfg: self.fail(
                    "implicit enablement must not read Odoo"
                ),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(result["candidate_refresh_status"], "disabled")
            self.assertFalse((root / "pool.sqlite3").exists())

    def test_invalid_document_timezone_is_a_configuration_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["westerman_api"]["document_timezone"] = "/etc/passwd"
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            result, exit_code = sync.run_sync(
                config_path=config_path,
                candidate_fetcher=lambda _cfg: self._refresh(),
                probe_runner=lambda *_args, **_kwargs: self.fail(
                    "invalid configuration must fail before workers"
                ),
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "CONFIG_INVALID_SYSTEMEXIT")

    def test_auth_failure_stops_later_requests_and_returns_degraded_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            calls = []

            def probe_runner(_args, _source, _download, reference, **_kwargs):
                calls.append(reference)
                raise probe.WestermanError(
                    "authentication failed",
                    error_code="get_order_info_http_error",
                    http_status=401,
                    auth_error=True,
                )

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: self._refresh("A", "B"),
                probe_runner=probe_runner,
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 3)
            self.assertEqual(result["status"], "degraded")
            self.assertTrue(result["provider_auth_failed"])
            self.assertEqual(result["checked_count"], 1)
            self.assertEqual(result["auth_blocked_count"], 1)
            self.assertEqual(result["retry_count"], 2)
            self.assertEqual(calls, ["A"])
            pool = WestermanPodPool(root / "pool.sqlite3")
            self.assertEqual(pool.get_item("A")["last_error_code"], "PROVIDER_AUTH_ERROR")
            self.assertEqual(pool.get_item("B")["last_error_code"], "AUTH_BLOCKED")

    def test_complete_candidate_refresh_expires_stale_pool_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            pool = WestermanPodPool(root / "pool.sqlite3")
            pool.upsert_candidates(["STALE"])

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: {
                    **self._refresh("ACTIVE"),
                    "scan_truncated": False,
                },
                probe_runner=lambda *_args, **_kwargs: {
                    "http_status": 200,
                    "document_count": 0,
                    "downloaded_count": 0,
                    "already_downloaded_count": 0,
                    "failed_documents": [],
                },
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["expired_count"], 1)
            self.assertEqual(pool.get_item("STALE")["pull_state"], "expired")
            self.assertEqual(pool.get_item("ACTIVE")["pull_state"], "waiting_pod")

    def test_truncated_candidate_refresh_is_degraded_and_preserves_old_gate_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            pool = WestermanPodPool(root / "pool.sqlite3")
            pool.upsert_candidates(["STALE"])
            pool.claim_due(lease_owner="seed-worker")
            pool.mark_downloaded("STALE", lease_owner="seed-worker", document_count=1)

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: {
                    **self._refresh("ACTIVE"),
                    "scan_truncated": True,
                },
                probe_runner=lambda *_args, **_kwargs: {
                    "http_status": 200,
                    "document_count": 0,
                    "downloaded_count": 0,
                    "already_downloaded_count": 0,
                    "failed_documents": [],
                },
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["candidate_refresh_status"], "truncated")
            self.assertEqual(result["error_code"], "CANDIDATE_SCAN_TRUNCATED")
            self.assertEqual(result["expired_count"], 0)
            stale = pool.get_item("STALE")
            self.assertEqual(stale["pull_state"], "downloaded")
            self.assertEqual(stale["push_state"], "pending")
            self.assertEqual(
                pool.candidate_refresh_status()["status"],
                "truncated",
            )

    def test_candidate_refresh_failure_persists_fail_closed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)

            def fail_refresh(_cfg):
                raise OSError("odoo unavailable")

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=fail_refresh,
                probe_runner=lambda *_args, **_kwargs: self.fail("pool is empty"),
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(result["candidate_refresh_status"], "failed")
            pool = WestermanPodPool(root / "pool.sqlite3")
            self.assertEqual(pool.candidate_refresh_status()["status"], "failed")

    def test_stale_lease_is_recovered_before_absent_candidate_is_expired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            pool = WestermanPodPool(root / "pool.sqlite3")
            pool.upsert_candidates(["STALE"], now="2020-01-01T00:00:00Z")
            pool.claim_due(
                lease_owner="dead-worker",
                lease_seconds=60,
                now="2020-01-01T00:00:00Z",
            )

            result, exit_code = sync.run_sync(
                config_path=config_path,
                workers=1,
                candidate_fetcher=lambda _cfg: {
                    **self._refresh(),
                    "scan_truncated": False,
                },
                probe_runner=lambda *_args, **_kwargs: self.fail("expired row must not be polled"),
                session_factory=lambda _cfg: object(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["recovered_lease_count"], 1)
            self.assertEqual(result["expired_count"], 1)
            self.assertEqual(result["due_count"], 0)
            self.assertEqual(pool.get_item("STALE")["pull_state"], "expired")

    def test_concurrent_complete_refresh_cannot_let_inflight_removed_item_become_eligible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._config(root)
            reference = "WDB2B/OUT/01391"
            worker_started = threading.Event()
            release_worker = threading.Event()
            first_result = {}

            def blocking_probe(*_args, **_kwargs):
                worker_started.set()
                if not release_worker.wait(timeout=5):
                    raise AssertionError("test worker was not released")
                return {
                    "http_status": 200,
                    "document_count": 1,
                    "pod_document_count": 1,
                    "download_candidate_count": 1,
                    "downloaded_count": 1,
                    "already_downloaded_count": 0,
                    "failed_documents": [],
                }

            def run_first_sync():
                first_result["value"] = sync.run_sync(
                    config_path=config_path,
                    workers=1,
                    candidate_fetcher=lambda _cfg: self._refresh(reference),
                    probe_runner=blocking_probe,
                    session_factory=lambda _cfg: object(),
                )

            thread = threading.Thread(target=run_first_sync, daemon=True)
            thread.start()
            self.assertTrue(worker_started.wait(timeout=5))
            try:
                second, second_exit = sync.run_sync(
                    config_path=config_path,
                    workers=1,
                    candidate_fetcher=lambda _cfg: self._refresh(),
                    probe_runner=lambda *_args, **_kwargs: self.fail(
                        "leased item must not be claimed by the second run"
                    ),
                    session_factory=lambda _cfg: object(),
                )
                self.assertEqual(second_exit, 0)
                self.assertEqual(second["candidate_refresh_status"], "complete")
                self.assertEqual(second["due_count"], 0)
            finally:
                release_worker.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first_result["value"][1], 0)

            pool = WestermanPodPool(root / "pool.sqlite3")
            row = pool.get_item(reference)
            self.assertEqual(row["pull_state"], "expired")
            self.assertEqual(row["push_state"], "needs_review")
            self.assertIsNone(row["deferred_terminal_state"])
            gate = evaluate_westerman_pool_gate(pool.db_path)
            self.assertEqual(gate["status"], "ok")
            self.assertEqual(gate["eligible_references"], set())


if __name__ == "__main__":
    unittest.main()
