import asyncio
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_push
from webapp import odoo_push_history
from clients.westerman_api.pool import WestermanPodPool


class FakeCommon:
    def version(self):
        return {"server_version": "test"}


class EmptyExistingIndex:
    def summary(self):
        return {"total_loaded": 0}

    def has_pod(self, *, tracking_number=None, picking_name=None):
        return False

    def get_pod_info(self, *, tracking_number=None, picking_name=None):
        return None


def complete_tracking_pod(pod_link, **values):
    record = {
        "pod_link": pod_link,
        "pod_time": "2026-07-18 08:00:00",
        "pod_state": "pod",
        "api_state": "completed",
    }
    record.update(values)
    return record


class OdooPushTests(unittest.TestCase):
    def _config(self):
        return {"odoo": {"enabled": True, "url": "https://odoo.test", "db": "db", "uid": 1, "api_key": "key"}}

    def test_provider_delivery_type_resolution_uses_defaults_override_and_unknown_compatibility(self):
        self.assertEqual(
            odoo_push._payload_expected_delivery_type(
                {}, {"source_name": "EMTC", "source_type": "gdrive"}
            ),
            "spare",
        )
        self.assertEqual(
            odoo_push._payload_expected_delivery_type(
                {
                    "odoo_reconcile": {
                        "source_delivery_types": {"cmc": "cmc_custom"}
                    }
                },
                {"source_name": "CMC", "source_type": "onedrive"},
            ),
            "cmc_custom",
        )
        self.assertIsNone(
            odoo_push._payload_expected_delivery_type(
                {}, {"source_name": "Legacy custom source"}
            )
        )

    def test_bulk_push_rejects_cross_carrier_same_key_and_writes_correct_carrier(self):
        class CarrierAwareModels:
            def __init__(self, carrier_type):
                self.carrier_type = carrier_type
                self.writes = []
                self.domains = []

            def execute_kw(self, _db, _uid, _password, model, method, args, options=None):
                if method == "web_search_read":
                    domain = (options or {}).get("domain", [])
                    self.domains.append((model, domain))
                    delivery_type = next(
                        (
                            term[2]
                            for term in domain
                            if isinstance(term, tuple)
                            and term[0].endswith("delivery_type")
                        ),
                        None,
                    )
                    if delivery_type != self.carrier_type:
                        return {"records": []}
                    if model == "stock.tracking":
                        return {
                            "records": [
                                {
                                    "id": 10,
                                    "tracking_number": "SHARED-KEY",
                                    "picking_id": [100, "WCB2B/OUT/100"],
                                    "pod_link": None,
                                }
                            ]
                        }
                    return {"records": [{"id": 100, "name": "WCB2B/OUT/100"}]}
                if model == "stock.tracking" and method == "write":
                    self.writes.append(args)
                    return True
                raise AssertionError((model, method))

        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "SHARED-KEY",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            def run(carrier_type):
                models = CarrierAwareModels(carrier_type)
                with mock.patch.object(
                    odoo_push, "load_config", return_value=self._config()
                ), mock.patch.object(
                    odoo_push,
                    "resolve_push_file_url",
                    return_value=("https://files.test/proof.pdf", None, "local"),
                ), mock.patch.object(
                    odoo_push,
                    "connect_odoo",
                    return_value=(FakeCommon(), models),
                ):
                    result = odoo_push._push_payload_unlocked(
                        config_path="config.local.json",
                        payload_path=str(payload_path),
                    )
                return result, models

            wrong_result, wrong_models = run("spare")
            correct_result, correct_models = run("cmc")

        self.assertEqual(wrong_result["details"][0]["status"], "not_found")
        self.assertEqual(wrong_models.writes, [])
        self.assertEqual(correct_result["details"][0]["status"], "pushed_tracking")
        self.assertEqual(len(correct_models.writes), 1)
        self.assertIn(
            ("picking_id.carrier_id.delivery_type", "=", "cmc"),
            wrong_models.domains[0][1],
        )
        self.assertIn(
            ("carrier_id.delivery_type", "=", "cmc"),
            wrong_models.domains[1][1],
        )

    def test_single_push_passes_provider_delivery_type_to_live_lookup(self):
        with mock.patch.object(
            odoo_push, "load_config", return_value=self._config()
        ), mock.patch.object(
            odoo_push,
            "resolve_push_file_url",
            return_value=("https://files.test/proof.pdf", None, "local"),
        ), mock.patch.object(
            odoo_push,
            "connect_odoo",
            return_value=(FakeCommon(), object()),
        ), mock.patch.object(
            odoo_push,
            "search_stock_tracking",
            return_value=[{"id": 10, "tracking_number": "TN1", "pod_link": None}],
        ) as search_mock, mock.patch.object(
            odoo_push, "write_tracking_pod"
        ) as write_mock, mock.patch.object(
            odoo_push, "_record_successful_single_push_in_index"
        ):
            result = odoo_push._push_single_item_unlocked(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://source.test/proof.pdf",
                source_name="CMC",
            )

        self.assertEqual(result["status"], "pushed_tracking")
        search_mock.assert_called_once_with(
            mock.ANY,
            self._config()["odoo"],
            "TN1",
            expected_delivery_type="cmc",
        )
        write_mock.assert_called_once()

    def test_push_payload_serializes_concurrent_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active = 0
            max_active = 0
            counter_lock = threading.Lock()

            def fake_push(**_kwargs):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1
                return {"ok": True}

            with mock.patch.object(
                odoo_push,
                "ODOO_PUSH_LOCK_PATH",
                Path(tmpdir) / "push.lock",
            ), mock.patch.object(
                odoo_push,
                "_push_payload_unlocked",
                side_effect=fake_push,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(
                            lambda _: odoo_push.push_payload(
                                config_path="config.local.json",
                                payload_path="payload.json",
                            ),
                            range(2),
                        )
                    )

            self.assertEqual(results, [{"ok": True}, {"ok": True}])
            self.assertEqual(max_active, 1)

    def test_push_single_item_serializes_concurrent_creates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active = 0
            max_active = 0
            create_count = 0
            tracking_record = None
            state_lock = threading.Lock()
            original_unlocked = odoo_push._push_single_item_unlocked

            def tracked_unlocked(**kwargs):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.03)
                    return original_unlocked(**kwargs)
                finally:
                    with state_lock:
                        active -= 1

            def search_tracking(*_args, **_kwargs):
                with state_lock:
                    return [dict(tracking_record)] if tracking_record else []

            def search_picking_tracking(*_args, **_kwargs):
                with state_lock:
                    return [dict(tracking_record)] if tracking_record else []

            def create_tracking(*_args, **_kwargs):
                nonlocal create_count, tracking_record
                with state_lock:
                    create_count += 1
                    tracking_record = {
                        "id": 10,
                        "tracking_number": "TN1",
                        **complete_tracking_pod("https://files.test/pod.pdf"),
                    }
                return 10

            with mock.patch.object(
                odoo_push,
                "ODOO_PUSH_LOCK_PATH",
                Path(tmpdir) / "push.lock",
            ), mock.patch.object(
                odoo_push,
                "_push_single_item_unlocked",
                side_effect=tracked_unlocked,
            ), mock.patch.object(
                odoo_push,
                "load_config",
                return_value=self._config(),
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/pod.pdf", None, "local"),
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                side_effect=search_tracking,
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 100, "name": "WCB2B/OUT/100"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                side_effect=search_picking_tracking,
            ), mock.patch.object(
                odoo_push,
                "create_stock_tracking",
                side_effect=create_tracking,
            ), mock.patch.object(
                odoo_push,
                "_record_successful_single_push_in_index",
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(
                            lambda _: odoo_push.push_single_item(
                                config_path="config.local.json",
                                match_key="TN1",
                                match_key_type="tracking_number",
                                file_url="https://files.test/pod.pdf",
                            ),
                            range(2),
                        )
                    )

            self.assertEqual(max_active, 1)
            self.assertEqual(create_count, 1)
            self.assertEqual(
                {result["status"] for result in results},
                {"created_tracking", "skipped_same"},
            )

    def test_single_and_batch_push_share_the_same_serialization_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active = 0
            max_active = 0
            counter_lock = threading.Lock()
            start = threading.Barrier(2)

            def critical_section(kind):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1
                return {"ok": True, "kind": kind}

            def run_single():
                start.wait()
                return odoo_push.push_single_item(
                    config_path="config.local.json",
                    match_key="TN1",
                    match_key_type="tracking_number",
                    file_url="https://files.test/pod.pdf",
                )

            def run_batch():
                start.wait()
                return odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path="payload.json",
                )

            with mock.patch.object(
                odoo_push,
                "ODOO_PUSH_LOCK_PATH",
                Path(tmpdir) / "push.lock",
            ), mock.patch.object(
                odoo_push,
                "_push_single_item_unlocked",
                side_effect=lambda **_kwargs: critical_section("single"),
            ), mock.patch.object(
                odoo_push,
                "_push_payload_unlocked",
                side_effect=lambda **_kwargs: critical_section("batch"),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = [
                        executor.submit(run_single),
                        executor.submit(run_batch),
                    ]
                    results = [future.result() for future in results]

            self.assertEqual(max_active, 1)
            self.assertEqual(
                {result["kind"] for result in results},
                {"single", "batch"},
            )

    def test_push_single_item_rejects_multiple_tracking_matches(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{"id": 10, "tracking_number": "TN1"}, {"id": 11, "tracking_number": "TN1"}],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock, \
            mock.patch.object(odoo_push, "_record_successful_single_push_in_index"):
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "multiple_tracking_matches")
        write_mock.assert_not_called()

    def test_push_single_item_does_not_create_tracking_from_picking_name(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(odoo_push, "search_picking", return_value=[{"id": 100, "name": "WCB2B/OUT/100"}]), \
            mock.patch.object(odoo_push, "search_stock_tracking_by_picking_id", return_value=[]), \
            mock.patch.object(odoo_push, "create_stock_tracking") as create_mock:
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="WCB2B/OUT/100",
                match_key_type="picking_name",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "needs_review")
        create_mock.assert_not_called()

    def test_push_single_item_does_not_choose_among_multiple_picking_trackings(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/new.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 100, "name": "WCB2B/OUT/100"}],
            ), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                return_value=[
                    {"id": 10, "tracking_number": "TN1", "pod_link": "old-1"},
                    {"id": 11, "tracking_number": "TN2", "pod_link": "old-2"},
                ],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock, \
            mock.patch.object(odoo_push, "_record_successful_single_push_in_index"):
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="WCB2B/OUT/100",
                match_key_type="picking_name",
                file_url="https://files.test/new.pdf",
            )

        self.assertEqual(result["status"], "multiple_tracking_records")
        self.assertEqual(result["target_id"], [10, 11])
        write_mock.assert_not_called()

    def test_push_single_item_rejects_westerman_pool_bypass(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="WDB2B/OUT/01391",
                match_key_type="picking_name",
                file_url="https://files.test/pod.pdf",
                source_name="Westerman",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "WESTERMAN_SINGLE_PUSH_REQUIRES_POOL_PAYLOAD",
        )
        connect_mock.assert_not_called()

    def test_push_single_item_updates_local_pod_index_after_success(self):
        from webapp.odoo_pull import ExistingPodIndex

        index = ExistingPodIndex()
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[
                    {
                        "id": 10,
                        "tracking_number": "TN1",
                        "picking_id": [501, "OUT/501"],
                        "pod_link": None,
                    }
                ],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod"), \
            mock.patch(
                "webapp.odoo_pull.update_saved_index",
                side_effect=lambda mutator, **kwargs: (mutator(index), index)[1],
            ) as update_mock:
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "pushed_tracking")
        self.assertEqual(index.by_tracking_number["TN1"]["pod_link"], "https://files.test/pod.pdf")
        self.assertEqual(index.by_tracking_number["TN1"]["source_model"], "local_push")
        self.assertTrue(index.updated_at)
        update_mock.assert_called_once()
        self.assertEqual(update_mock.call_args.kwargs["coverage_picking_id"], 501)
        self.assertNotIn("_index_picking_id", result)

    def test_local_file_url_uses_unsigned_url_when_secret_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            local_file = tmp_path / "CMC" / "proof.pdf"
            local_file.parent.mkdir()
            local_file.write_text("pod", encoding="utf-8")
            cfg = {
                "pod_sync": {
                    "base_url": "https://pod.example.test",
                    "file_url_secret": "CHANGE_ME_RANDOM_32_BYTES",
                },
                "download": {"base_dir": str(tmp_path)},
            }

            self.assertEqual(
                odoo_push.get_local_file_url(cfg, str(local_file)),
                "https://pod.example.test/storage/files/CMC/proof.pdf",
            )

    def test_local_file_url_uses_environment_secret_for_signed_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            local_file = tmp_path / "Westerman" / "proof.pdf"
            local_file.parent.mkdir()
            local_file.write_text("pod", encoding="utf-8")
            cfg = {
                "pod_sync": {"base_url": "https://pod.example.test"},
                "download": {"base_dir": str(tmp_path)},
            }

            with mock.patch.dict(
                "os.environ", {"POD_SYNC_FILE_URL_SECRET": "x" * 32}
            ):
                file_url = odoo_push.get_local_file_url(cfg, str(local_file))

            self.assertTrue(file_url.startswith(
                "https://pod.example.test/storage/files/Westerman/proof.pdf?"
            ))
            self.assertIn("token=", file_url)

    def test_signed_url_reaches_xmlrpc_but_is_redacted_from_public_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            local_file = root / "storage" / "CMC" / "proof.pdf"
            local_file.parent.mkdir(parents=True)
            local_file.write_bytes(b"pod")
            payload_path = root / "odoo_backfill_payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "identifier_status": "ready",
                                "preferred_key_type": "tracking_number",
                                "preferred_key": "TN-SIGNED",
                                "local_path": str(local_file),
                                "source_name": "CMC",
                                "source_type": "onedrive",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg.update(
                {
                    "download": {"base_dir": str(root / "storage")},
                    "pod_sync": {
                        "base_url": "https://pod.example.test",
                        "file_url_secret": "s" * 32,
                    },
                }
            )
            models = object()
            with mock.patch.object(
                odoo_push, "load_config", return_value=full_cfg
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), models),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{
                    "id": 99,
                    "tracking_number": "TN-SIGNED",
                    "pod_link": None,
                }],
            ), mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        written_url = write_mock.call_args.args[3]
        token = written_url.rsplit("token=", 1)[1]
        self.assertIn("?token=", written_url)
        self.assertNotIn(token, json.dumps(result))
        self.assertIn(
            "token=[REDACTED]",
            result["details"][0]["file_url"],
        )

    def test_resolve_push_file_url_refuses_source_link_without_local_path(self):
        cfg = {
            "pod_sync": {"base_url": "https://pod.example.test"},
            "download": {"base_dir": "/tmp/pod"},
        }

        file_url, error, source = odoo_push.resolve_push_file_url(
            cfg,
            local_path="",
            source_file_url="https://drive.google.com/uc?id=source&export=download",
        )

        self.assertEqual(file_url, "")
        self.assertEqual(source, "missing_local")
        self.assertIn("refusing to push source POD link", error)

    def test_resolve_push_file_url_refuses_missing_local_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {
                "pod_sync": {"base_url": "https://pod.example.test"},
                "download": {"base_dir": tmpdir},
            }
            missing_file = Path(tmpdir) / "CMC" / "missing.pdf"

            file_url, error, source = odoo_push.resolve_push_file_url(
                cfg,
                local_path=str(missing_file),
                source_file_url="https://drive.google.com/uc?id=source&export=download",
            )

        self.assertEqual(file_url, "")
        self.assertEqual(source, "missing_local")
        self.assertIn("does not exist", error)

    def test_push_single_item_updates_different_existing_pod(self):
        models = object()
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/new.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), models)), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{
                    "id": 10,
                    "tracking_number": "TN1",
                    "pod_link": "https://files.test/old.pdf?token=do-not-log",
                }],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock, \
            mock.patch.object(odoo_push, "_record_successful_single_push_in_index"):
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/new.pdf",
            )

        self.assertEqual(result["status"], "pushed_tracking")
        self.assertTrue(result["pod_link_replaced"])
        self.assertNotIn("do-not-log", json.dumps(result))
        write_mock.assert_called_once_with(
            models,
            self._config()["odoo"],
            10,
            "https://files.test/new.pdf",
            mock.ANY,
        )

    def test_push_single_item_skips_exact_same_existing_pod(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[complete_tracking_pod(
                    "https://files.test/pod.pdf",
                    id=10,
                    tracking_number="TN1",
                )],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock, \
            mock.patch.object(odoo_push, "_record_successful_single_push_in_index"):
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "skipped_same")
        write_mock.assert_not_called()

    def test_push_single_item_repairs_same_url_with_incomplete_pod_state(self):
        models = object()
        with mock.patch.object(
            odoo_push, "load_config", return_value=self._config()
        ), mock.patch.object(
            odoo_push,
            "resolve_push_file_url",
            return_value=("https://files.test/pod.pdf", None, "local"),
        ), mock.patch.object(
            odoo_push, "connect_odoo", return_value=(FakeCommon(), models)
        ), mock.patch.object(
            odoo_push,
            "search_stock_tracking",
            return_value=[{
                "id": 10,
                "tracking_number": "TN1",
                "pod_link": "https://files.test/pod.pdf",
                "pod_time": False,
                "pod_state": "progress",
                "api_state": "sent",
            }],
        ), mock.patch.object(
            odoo_push, "write_tracking_pod"
        ) as write_mock, mock.patch.object(
            odoo_push, "_record_successful_single_push_in_index"
        ):
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "pushed_tracking")
        self.assertTrue(result["pod_state_repaired"])
        self.assertNotIn("pod_link_replaced", result)
        write_mock.assert_called_once_with(
            models,
            self._config()["odoo"],
            10,
            "https://files.test/pod.pdf",
            mock.ANY,
        )

    def test_push_payload_updates_different_existing_pod(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/old-index.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "file_url": "https://drive.google.com/uc?id=source&export=download",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                    "source_name": "EMTC",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/new.pdf", None, "local")), \
                mock.patch("webapp.odoo_pull.get_index", return_value=existing_index), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(
                    odoo_push,
                    "search_stock_tracking",
                    return_value=[{
                        "id": 10,
                        "tracking_number": "TN1",
                        "pod_link": "https://files.test/old.pdf?token=do-not-log",
                    }],
                ), \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["skipped_existing"], 0)
        self.assertEqual(result["needs_review"], 0)
        self.assertEqual(result["tracking_pushed"], 1)
        self.assertEqual(result["details"][0]["status"], "pushed_tracking")
        self.assertTrue(result["details"][0]["pod_link_replaced"])
        self.assertNotIn("do-not-log", json.dumps(result))
        write_mock.assert_called_once()

    def test_missing_only_skips_only_exact_target_url(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/target.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ) as connect_mock, mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[complete_tracking_pod(
                    "https://files.test/target.pdf",
                    id=10,
                    tracking_number="TN1",
                )],
            ) as tracking_search:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["details"], [])
        connect_mock.assert_called_once()
        tracking_search.assert_called_once_with(
            mock.ANY,
            self._config()["odoo"],
            "TN1",
            expected_delivery_type="cmc",
        )

    def test_missing_only_checks_all_urls_for_tracking_key(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/first.pdf",
        )
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/target.pdf",
        )
        self.assertEqual(
            existing_index.get_pod_info(tracking_number="TN1")["pod_link"],
            "https://files.test/first.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ) as connect_mock, mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[complete_tracking_pod(
                    "https://files.test/target.pdf",
                    id=10,
                    tracking_number="TN1",
                )],
            ) as tracking_search:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["skipped_existing"], 1)
        connect_mock.assert_called_once()
        tracking_search.assert_called_once()

    def test_missing_only_does_not_skip_same_url_with_missing_pod_time(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/target.pdf",
        )
        payload = {
            "items": [{
                "identifier_status": "ready",
                "preferred_key_type": "tracking_number",
                "preferred_key": "TN1",
                "local_path": "/tmp/pod/CMC/proof.pdf",
                "source_name": "CMC",
                "source_type": "onedrive",
            }]
        }
        incomplete = {
            "id": 10,
            "tracking_number": "TN1",
            "pod_link": "https://files.test/target.pdf",
            "pod_time": False,
            "pod_state": "pod",
            "api_state": "completed",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[incomplete],
            ), mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_existing"], 0)
        self.assertEqual(result["tracking_pushed"], 1)
        self.assertTrue(result["details"][0]["pod_state_repaired"])
        write_mock.assert_called_once()

    def test_missing_only_exact_index_hit_does_not_hide_multi_tracking_picking(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="PKG-1",
            picking_name="WCB2B/OUT/12345",
            pod_link="https://files.test/target.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WCB2BOUT12345",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 12345, "name": "WCB2B/OUT/12345"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                return_value=[
                    {"id": 11, "pod_link": "https://files.test/target.pdf"},
                    {"id": 12, "pod_link": None},
                ],
            ), mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["business_status"], "attention")
        self.assertEqual(result["business_pending"], 1)
        self.assertEqual(result["technical_failure_count"], 0)
        self.assertEqual(result["skipped_existing"], 0)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(result["details"][0]["status"], "multiple_tracking_records")
        write_mock.assert_not_called()

    def test_push_payload_uses_current_tracking_record_for_same_url_check(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/pod.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
                mock.patch("webapp.odoo_pull.get_index", return_value=existing_index), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(
                    odoo_push,
                    "search_stock_tracking",
                    return_value=[complete_tracking_pod(
                        "https://files.test/pod.pdf",
                        id=10,
                        tracking_number="TN1",
                    )],
                ) as search_mock, \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["skipped_existing"], 0)
        self.assertEqual(result["tracking_skipped_same"], 1)
        self.assertEqual(result["details"][0]["status"], "skipped_same")
        search_mock.assert_called_once()
        write_mock.assert_not_called()

    def test_push_payload_reports_ok_when_every_item_completes(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN-OK",
                    "local_path": "/tmp/pod/CMC/ok.pdf",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/ok.pdf", None, "local")), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(odoo_push, "search_stock_tracking", return_value=[{"id": 1, "tracking_number": "TN-OK", "pod_link": None}]), \
                mock.patch.object(odoo_push, "write_tracking_pod"):
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["incomplete"], 0)
        self.assertEqual(result["retryable"], 0)

    def test_push_payload_reports_business_attention_and_continues_after_review_items(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN-OK",
                    "local_path": "/tmp/pod/CMC/ok.pdf",
                    "source_name": "CMC",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN-AMBIGUOUS",
                    "local_path": "/tmp/pod/CMC/ambiguous.pdf",
                    "source_name": "CMC",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "",
                    "preferred_key": "",
                    "local_path": "/tmp/pod/CMC/missing-key.pdf",
                    "source_name": "CMC",
                },
            ]
        }

        def search_tracking(_models, _cfg, key, **_kwargs):
            if key == "TN-OK":
                return [{"id": 1, "tracking_number": key, "pod_link": None}]
            return [
                {"id": 2, "tracking_number": key, "pod_link": None},
                {"id": 3, "tracking_number": key, "pod_link": None},
            ]

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(odoo_push, "search_stock_tracking", side_effect=search_tracking), \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["business_status"], "attention")
        self.assertEqual(result["business_pending"], 2)
        self.assertEqual(result["technical_failure_count"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["incomplete"], 2)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(result["missing_required"], 1)
        self.assertEqual(result["manual_review"], 2)
        self.assertEqual(result["retryable"], 0)
        write_mock.assert_called_once()

    def test_push_payload_reports_failed_when_all_items_fail_but_attempts_each(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": key,
                    "local_path": f"/tmp/pod/CMC/{key}.pdf",
                    "source_name": "CMC",
                }
                for key in ("TN-1", "TN-2")
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(odoo_push, "search_stock_tracking", side_effect=RuntimeError("temporary XML-RPC failure")) as search_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["business_status"], "complete")
        self.assertEqual(result["business_pending"], 0)
        self.assertEqual(result["technical_failure_count"], 2)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["incomplete"], 2)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["retryable"], 2)
        self.assertEqual(search_mock.call_count, 2)

    def test_push_payload_returns_structured_failure_for_invalid_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text("not-json", encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ):
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["business_status"], "unknown")
        self.assertEqual(result["business_pending"], 0)
        self.assertEqual(result["technical_failure_count"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["incomplete"], 1)
        self.assertIn("JSONDecodeError", result["error"])

    def test_rerun_skips_exact_success_and_retries_previous_failure(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": key,
                    "local_path": f"/tmp/pod/CMC/{key}.pdf",
                    "source_name": "CMC",
                }
                for key in ("TN-OK", "TN-RETRY")
            ]
        }
        links = {"TN-OK": None, "TN-RETRY": None}
        failed_once = False

        def search_tracking(_models, _cfg, key, **_kwargs):
            tracking_id = 1 if key == "TN-OK" else 2
            if links[key]:
                return [complete_tracking_pod(
                    links[key],
                    id=tracking_id,
                    tracking_number=key,
                )]
            return [{"id": tracking_id, "tracking_number": key, "pod_link": None}]

        def write_tracking(_models, _cfg, tracking_id, file_url, _pod_time):
            nonlocal failed_once
            key = "TN-OK" if tracking_id == 1 else "TN-RETRY"
            if key == "TN-RETRY" and not failed_once:
                failed_once = True
                raise RuntimeError("temporary write failure")
            links[key] = file_url
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(odoo_push, "search_stock_tracking", side_effect=search_tracking), \
                mock.patch.object(odoo_push, "write_tracking_pod", side_effect=write_tracking) as write_mock:
                first = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )
                second = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["completed"], 1)
        self.assertEqual(first["retryable"], 1)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["tracking_skipped_same"], 1)
        self.assertEqual(second["tracking_pushed"], 1)
        self.assertEqual(second["retryable"], 0)
        self.assertEqual(write_mock.call_count, 3)

    def test_duplicate_westerman_target_selects_latest_document_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pool_path = root / "pool.sqlite3"
            pool = WestermanPodPool(pool_path)
            pool.set_candidate_refresh_status("complete")
            pool.upsert_candidates(["WDB2B/OUT/01358"])
            pool.claim_due(lease_owner="test-worker")
            pool.mark_downloaded(
                "WDB2B/OUT/01358",
                lease_owner="test-worker",
                document_count=2,
            )
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "payload_item_id": "old::WDB2BOUT01358",
                                "document_uid": "doc-old",
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01358",
                                "shipment_reference": "WDB2B/OUT/01358",
                                "source_name": "Westerman",
                                "source_type": "westerman_api",
                                "file_name": "older.pdf",
                                "source_modified_at": "2026-07-17T10:00:00Z",
                                "local_path": "/tmp/pod/Westerman/older.pdf",
                            },
                            {
                                "payload_item_id": "new::WDB2BOUT01358",
                                "document_uid": "doc-new",
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01358",
                                "shipment_reference": "WDB2B/OUT/01358",
                                "source_name": "Westerman",
                                "source_type": "westerman_api",
                                "file_name": "latest.pdf",
                                "source_modified_at": "2026-07-18T10:00:00Z",
                                "local_path": "/tmp/pod/Westerman/latest.pdf",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(pool_path),
            }
            models = object()

            def resolve_url(_cfg, *, local_path, source_file_url):
                del source_file_url
                return (
                    f"https://files.test/{Path(local_path).name}",
                    None,
                    "local",
                )

            with mock.patch.object(
                odoo_push, "load_config", return_value=full_cfg
            ), mock.patch.object(
                odoo_push, "resolve_push_file_url", side_effect=resolve_url
            ), mock.patch.object(
                odoo_push, "connect_odoo", return_value=(FakeCommon(), models)
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 1358, "name": "WDB2B/OUT/01358"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                return_value=[{"id": 58, "pod_link": None}],
            ), mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["input_total"], 2)
        self.assertEqual(result["eligible_document_count"], 2)
        self.assertEqual(result["selected_target_count"], 1)
        self.assertEqual(result["coalesced_count"], 1)
        self.assertEqual(result["superseded_count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(
            [detail["status"] for detail in result["details"]],
            ["superseded", "pushed_tracking"],
        )
        self.assertEqual(
            result["details"][0]["selected_document_uid"], "doc-new"
        )
        write_mock.assert_called_once_with(
            models,
            self._config()["odoo"],
            58,
            "https://files.test/latest.pdf",
            mock.ANY,
        )

    def test_coalesced_westerman_payload_is_idempotent_on_rerun(self):
        payload = {
            "items": [
                {
                    "payload_item_id": "old::WDB2BOUT01363",
                    "document_uid": "doc-old",
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01363",
                    "shipment_reference": "WDB2B/OUT/01363",
                    "source_name": "Westerman",
                    "source_type": "westerman_api",
                    "file_name": "a.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/Westerman/a.pdf",
                },
                {
                    "payload_item_id": "new::WDB2BOUT01363",
                    "document_uid": "doc-new",
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01363",
                    "shipment_reference": "WDB2B/OUT/01363",
                    "source_name": "Westerman",
                    "source_type": "westerman_api",
                    "file_name": "z.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/Westerman/z.pdf",
                },
            ]
        }
        current_link = None

        def resolve_url(_cfg, *, local_path, source_file_url):
            del source_file_url
            return f"https://files.test/{Path(local_path).name}", None, "local"

        def tracking_for_picking(*_args, **_kwargs):
            if current_link:
                return [complete_tracking_pod(current_link, id=63)]
            return [{"id": 63, "pod_link": None}]

        def write_tracking(_models, _cfg, _tracking_id, file_url, _pod_time):
            nonlocal current_link
            current_link = file_url
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "_filter_westerman_items_by_pool",
                side_effect=lambda items, *_args: (
                    items,
                    0,
                    {"status": "ok"},
                ),
            ), mock.patch.object(
                odoo_push, "resolve_push_file_url", side_effect=resolve_url
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 1363, "name": "WDB2B/OUT/01363"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                side_effect=tracking_for_picking,
            ), mock.patch.object(
                odoo_push, "write_tracking_pod", side_effect=write_tracking
            ) as write_mock:
                first = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )
                second = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["selected_target_count"], 1)
        self.assertEqual(first["coalesced_count"], 1)
        self.assertEqual(first["attempted"], 1)
        self.assertEqual(first["tracking_pushed"], 1)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["selected_target_count"], 1)
        self.assertEqual(second["coalesced_count"], 1)
        self.assertEqual(second["attempted"], 1)
        self.assertEqual(second["tracking_skipped_same"], 1)
        self.assertEqual(current_link, "https://files.test/z.pdf")
        write_mock.assert_called_once()

    def test_limit_is_applied_after_document_coalescing(self):
        payload = {
            "items": [
                {
                    "document_uid": "a-old",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TARGET-A",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "a-old.pdf",
                    "source_modified_at": "2026-07-17T10:00:00Z",
                    "local_path": "/tmp/pod/CMC/a-old.pdf",
                },
                {
                    "document_uid": "a-new",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TARGET-A",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "a-new.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/CMC/a-new.pdf",
                },
                {
                    "document_uid": "b-only",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TARGET-B",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "b.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/CMC/b.pdf",
                },
            ]
        }

        def resolve_url(_cfg, *, local_path, source_file_url):
            del source_file_url
            return f"https://files.test/{Path(local_path).name}", None, "local"

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push, "resolve_push_file_url", side_effect=resolve_url
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{"id": 1, "tracking_number": "TARGET-A", "pod_link": None}],
            ) as search_mock, mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    limit=1,
                )

        self.assertEqual(result["input_total"], 3)
        self.assertEqual(result["selected_target_count"], 2)
        self.assertEqual(result["coalesced_count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["completed"], 1)
        search_mock.assert_called_once_with(
            mock.ANY,
            self._config()["odoo"],
            "TARGET-A",
            expected_delivery_type="cmc",
        )
        self.assertEqual(
            write_mock.call_args.args[3], "https://files.test/a-new.pdf"
        )

    def test_same_odoo_lookup_target_across_sources_requires_carrier_review(self):
        payload = {
            "items": [
                {
                    "document_uid": "cmc-old",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "SHARED-TARGET",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "cmc.pdf",
                    "source_modified_at": "2026-07-17T10:00:00Z",
                    "local_path": "/tmp/pod/CMC/cmc.pdf",
                },
                {
                    "document_uid": "emtc-new",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "SHARED-TARGET",
                    "source_name": "EMTC",
                    "source_type": "gdrive",
                    "file_name": "emtc.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/EMTC/emtc.pdf",
                },
            ]
        }

        def resolve_url(_cfg, *, local_path, source_file_url):
            del source_file_url
            return f"https://files.test/{Path(local_path).name}", None, "local"

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push, "resolve_push_file_url", side_effect=resolve_url
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ) as connect_mock, mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[
                    {
                        "id": 7,
                        "tracking_number": "SHARED-TARGET",
                        "pod_link": None,
                    }
                ],
            ) as search_mock, mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["input_total"], 2)
        self.assertEqual(result["selected_target_count"], 1)
        self.assertEqual(result["coalesced_count"], 0)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["business_status"], "attention")
        self.assertEqual(result["business_pending"], 1)
        self.assertEqual(result["technical_failure_count"], 0)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(result["manual_review"], 1)
        self.assertEqual(result["cross_source_conflict_count"], 1)
        self.assertEqual(
            result["details"][0]["status"], "cross_source_conflict"
        )
        self.assertEqual(result["details"][0]["conflict_sources"], ["CMC", "EMTC"])
        connect_mock.assert_not_called()
        search_mock.assert_not_called()
        write_mock.assert_not_called()

    def test_business_backlog_does_not_turn_successful_execution_into_failure(self):
        result = odoo_push._finalize_push_results(
            {
                "tracking_pushed": 26,
                "tracking_skipped_same": 35127,
                "tracking_not_found": 3503,
                "picking_pushed": 0,
                "picking_skipped_same": 0,
                "picking_not_found": 426,
                "skipped_existing": 0,
                "ambiguous": 642,
                "needs_review": 438,
                "missing_required": 0,
                "failed": 0,
                "details": [],
                "by_source": {},
                "westerman_pool_gate_status": "ok",
                "westerman_pool_filtered": 0,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_status"], "ok")
        self.assertEqual(result["business_status"], "attention")
        self.assertEqual(result["completed"], 35153)
        self.assertEqual(result["business_pending"], 5009)
        self.assertEqual(result["incomplete"], 5009)
        self.assertEqual(result["retryable"], 3929)
        self.assertEqual(result["technical_failure_count"], 0)

    def test_source_filter_is_applied_before_cross_source_coalescing(self):
        payload = {
            "items": [
                {
                    "document_uid": "cmc-old",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "SHARED-TARGET",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "cmc.pdf",
                    "source_modified_at": "2026-07-17T10:00:00Z",
                    "local_path": "/tmp/pod/CMC/cmc.pdf",
                },
                {
                    "document_uid": "emtc-new",
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "SHARED-TARGET",
                    "source_name": "EMTC",
                    "source_type": "gdrive",
                    "file_name": "emtc.pdf",
                    "source_modified_at": "2026-07-18T10:00:00Z",
                    "local_path": "/tmp/pod/EMTC/emtc.pdf",
                },
            ]
        }

        def resolve_url(_cfg, *, local_path, source_file_url):
            del source_file_url
            return f"https://files.test/{Path(local_path).name}", None, "local"

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push, "resolve_push_file_url", side_effect=resolve_url
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[
                    {
                        "id": 7,
                        "tracking_number": "SHARED-TARGET",
                        "pod_link": None,
                    }
                ],
            ), mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    source_filter="CMC",
                )

        self.assertEqual(result["input_total"], 1)
        self.assertEqual(result["selected_target_count"], 1)
        self.assertEqual(result["coalesced_count"], 0)
        write_mock.assert_called_once()
        self.assertEqual(
            write_mock.call_args.args[3], "https://files.test/cmc.pdf"
        )

    def test_push_payload_can_fail_closed_for_westerman_after_refresh_failure(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01391",
                    "source_name": "Westerman",
                    "source_type": "westerman_api",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "CMC-TRACKING",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch("webapp.odoo_pull.get_index", return_value=EmptyExistingIndex()), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(
                    odoo_push,
                    "resolve_push_file_url",
                    return_value=("https://files.test/pod.pdf", None, "local"),
                ), \
                mock.patch.object(
                    odoo_push,
                    "search_stock_tracking",
                    return_value=[{"id": 10, "tracking_number": "CMC-TRACKING", "pod_link": None}],
                ), \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    excluded_source_types={"westerman_api"},
                )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_excluded"], 1)
        self.assertEqual(result["incomplete"], 0)
        self.assertEqual(result["details"][0]["source"], "CMC")
        write_mock.assert_called_once()

    def test_westerman_picking_match_creates_tracking_from_payload_tracking_number(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pool_path = root / "pool.sqlite3"
            pool = WestermanPodPool(pool_path)
            pool.set_candidate_refresh_status("complete")
            pool.upsert_candidates(["WDB2B/OUT/01358"])
            pool.claim_due(lease_owner="test-worker")
            pool.mark_downloaded(
                "WDB2B/OUT/01358", lease_owner="test-worker", document_count=1
            )
            payload = {
                "items": [
                    {
                        "identifier_status": "ready",
                        "preferred_key_type": "picking_name",
                        "preferred_key": "WDB2BOUT01358",
                        "shipment_reference": "WDB2B/OUT/01358",
                        "tracking_number": "U260201855",
                        "file_url": "https://westerman.test/download_document",
                        "local_path": "/tmp/pod/Westerman/proof.pdf",
                        "source_name": "Westerman",
                        "source_type": "westerman_api",
                    }
                ]
            }
            payload_path = root / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            models = object()
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(pool_path),
            }
            with mock.patch.object(odoo_push, "load_config", return_value=full_cfg), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/pod.pdf", None, "local")), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), models)), \
                mock.patch.object(
                    odoo_push,
                    "search_picking",
                    return_value=[{"id": 1358, "name": "WDB2B/OUT/01358"}],
                ), \
                mock.patch.object(odoo_push, "search_stock_tracking_by_picking_id", return_value=[]), \
                mock.patch.object(odoo_push, "create_stock_tracking", return_value=9001) as create_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    skip_existing=False,
                )

        self.assertEqual(result["tracking_pushed"], 1)
        self.assertEqual(result["details"][0]["status"], "created_tracking")
        create_mock.assert_called_once_with(
            models,
            self._config()["odoo"],
            1358,
            "U260201855",
            "https://files.test/pod.pdf",
            mock.ANY,
        )

    def test_push_payload_filters_stale_westerman_item_at_mutation_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pool_path = root / "pool.sqlite3"
            pool = WestermanPodPool(pool_path)
            pool.set_candidate_refresh_status("complete")
            pool.upsert_candidates(["WDB2B/OUT/01391"])
            pool.expire_missing_candidates([])
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01391",
                                "shipment_reference": "WDB2B/OUT/01391",
                                "tracking_number": "U260203465",
                                "source_name": "Westerman",
                                "source_type": "westerman_api",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(pool_path),
            }
            with mock.patch.object(odoo_push, "load_config", return_value=full_cfg), \
                mock.patch.object(
                    odoo_push,
                    "connect_odoo",
                    return_value=(FakeCommon(), object()),
                ) as connect_mock, \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock, \
                mock.patch.object(odoo_push, "create_stock_tracking") as create_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    skip_existing=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["westerman_pool_gate_status"], "ok")
            self.assertEqual(result["westerman_pool_filtered"], 1)
            connect_mock.assert_not_called()
            write_mock.assert_not_called()
            create_mock.assert_not_called()

    def test_push_payload_isolates_westerman_when_pool_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01391",
                                "shipment_reference": "WDB2B/OUT/01391",
                                "source_name": "Westerman",
                                "source_type": "westerman_api",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(root / "missing.sqlite3"),
            }
            with mock.patch.object(odoo_push, "load_config", return_value=full_cfg), \
                mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    skip_existing=False,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["westerman_pool_gate_status"], "missing_pool")
            self.assertEqual(result["westerman_pool_filtered"], 1)
            connect_mock.assert_not_called()

    def test_legacy_westerman_payload_without_source_type_cannot_bypass_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01391",
                                "shipment_reference": "WDB2B/OUT/01391",
                                "source_name": "Westerman",
                                # Legacy payload intentionally has no source_type.
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(root / "missing.sqlite3"),
            }
            with mock.patch.object(
                odoo_push, "load_config", return_value=full_cfg
            ), mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["westerman_pool_gate_status"], "missing_pool")
        self.assertEqual(result["westerman_pool_filtered"], 1)
        connect_mock.assert_not_called()

    def test_legacy_westerman_payload_respects_pipeline_source_exclusion(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01391",
                    "source_name": "Westerman",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    excluded_source_types={"westerman_api"},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["source_excluded"], 1)
        self.assertEqual(result["total"], 0)
        connect_mock.assert_not_called()

    def test_push_payload_isolates_westerman_after_truncated_candidate_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pool_path = root / "pool.sqlite3"
            pool = WestermanPodPool(pool_path)
            pool.set_candidate_refresh_status("truncated")
            pool.upsert_candidates(["WDB2B/OUT/01391"])
            pool.claim_due(lease_owner="test-worker")
            pool.mark_downloaded(
                "WDB2B/OUT/01391", lease_owner="test-worker", document_count=1
            )
            payload_path = root / "payload.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "identifier_status": "ready",
                                "preferred_key_type": "picking_name",
                                "preferred_key": "WDB2BOUT01391",
                                "shipment_reference": "WDB2B/OUT/01391",
                                "source_name": "Westerman",
                                "source_type": "westerman_api",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(pool_path),
            }
            with mock.patch.object(odoo_push, "load_config", return_value=full_cfg), \
                mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    skip_existing=False,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["total"], 0)
            self.assertEqual(
                result["westerman_pool_gate_status"],
                "candidate_refresh_incomplete",
            )
            connect_mock.assert_not_called()

    def test_westerman_gate_failures_do_not_block_cmc_push(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01391",
                    "shipment_reference": "WDB2B/OUT/01391",
                    "source_name": "Westerman",
                    "source_type": "westerman_api",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "CMC-TRACKING",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload_path = root / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            cases = []
            missing_pool = root / "missing.sqlite3"
            cases.append(("missing_pool", missing_pool, True))

            corrupt_pool = root / "corrupt.sqlite3"
            corrupt_pool.write_bytes(b"not a sqlite database")
            cases.append(("pool_unavailable", corrupt_pool, True))

            stale_pool = root / "stale.sqlite3"
            stale = WestermanPodPool(stale_pool)
            stale.set_candidate_refresh_status(
                "complete",
                now=datetime.now(timezone.utc) - timedelta(hours=49),
            )
            cases.append(("candidate_refresh_stale", stale_pool, True))

            incomplete_pool = root / "incomplete.sqlite3"
            WestermanPodPool(incomplete_pool).set_candidate_refresh_status("failed")
            cases.append(("candidate_refresh_incomplete", incomplete_pool, True))

            cases.append(("source_not_enabled", None, False))

            for expected_status, pool_path, enabled in cases:
                with self.subTest(expected_status=expected_status):
                    full_cfg = self._config()
                    if enabled:
                        full_cfg["westerman_api"] = {
                            "enabled": True,
                            "pool_path": str(pool_path),
                            "candidate_refresh_max_age_hours": 48,
                        }
                    with mock.patch.object(
                        odoo_push, "load_config", return_value=full_cfg
                    ), mock.patch.object(
                        odoo_push,
                        "resolve_push_file_url",
                        return_value=("https://files.test/pod.pdf", None, "local"),
                    ), mock.patch(
                        "webapp.odoo_pull.get_index",
                        return_value=EmptyExistingIndex(),
                    ), mock.patch.object(
                        odoo_push,
                        "connect_odoo",
                        return_value=(FakeCommon(), object()),
                    ), mock.patch.object(
                        odoo_push,
                        "search_stock_tracking",
                        return_value=[{
                            "id": 10,
                            "tracking_number": "CMC-TRACKING",
                            "pod_link": None,
                        }],
                    ), mock.patch.object(
                        odoo_push, "write_tracking_pod"
                    ) as write_mock:
                        result = odoo_push.push_payload(
                            config_path="config.local.json",
                            payload_path=str(payload_path),
                        )

                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["total"], 1)
                    self.assertEqual(
                        result["westerman_pool_gate_status"], expected_status
                    )
                    self.assertEqual(result["westerman_pool_filtered"], 1)
                    self.assertEqual(result["details"][0]["source"], "CMC")
                    write_mock.assert_called_once()

    def test_push_inherits_phase_westerman_gate_after_phase_filtered_all_items(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "CMC-TRACKING",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "local_path": "/tmp/pod/CMC/proof.pdf",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload_path = root / "odoo_backfill_payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "phase1a_summary.json").write_text(
                json.dumps(
                    {
                        "westerman_source_gate": {
                            "status": "candidate_refresh_stale",
                            "filtered_record_count": 2,
                            "candidate_refresh_status": "complete",
                            "candidate_refresh_age_seconds": 176400,
                            "candidate_refresh_max_age_hours": 48,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/pod.pdf", None, "local"),
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{
                    "id": 10,
                    "tracking_number": "CMC-TRACKING",
                    "pod_link": None,
                }],
            ), mock.patch.object(
                odoo_push, "write_tracking_pod"
            ) as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total"], 1)
        self.assertEqual(
            result["westerman_pool_gate_status"],
            "candidate_refresh_stale",
        )
        self.assertEqual(result["westerman_pool_filtered"], 2)
        self.assertEqual(result["details"][0]["source"], "CMC")
        write_mock.assert_called_once()

    def test_payload_lookup_key_converts_canonical_picking_name(self):
        key_type, key_value = odoo_push._payload_lookup_key(
            {"preferred_key_type": "picking_name", "preferred_key": "WCB2BOUT33271"}
        )

        self.assertEqual(key_type, "picking_name")
        self.assertEqual(key_value, "WCB2B/OUT/33271")

    def test_payload_lookup_key_converts_westerman_picking_name(self):
        key_type, key_value = odoo_push._payload_lookup_key(
            {"preferred_key_type": "picking_name", "preferred_key": "WDB2BOUT01358"}
        )

        self.assertEqual(key_type, "picking_name")
        self.assertEqual(key_value, "WDB2B/OUT/01358")

    def test_preflight_treats_different_existing_pod_url_as_ready_update(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/old.pdf?token=do-not-log",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
                mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/new.pdf", None, "local")), \
                mock.patch("webapp.odoo_pull.get_index", return_value=existing_index), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(
                    odoo_push,
                    "search_stock_tracking",
                    return_value=[{
                        "id": 10,
                        "tracking_number": "TN1",
                        "pod_link": "https://files.test/old.pdf?token=do-not-log",
                    }],
                ):
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertEqual(result["ready_to_push_count"], 1)
        self.assertEqual(result["already_has_pod_count"], 0)
        self.assertNotIn("do-not-log", json.dumps(result))

    def test_push_page_summary_uses_gate_coalescing_and_exact_url(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/tn1-new.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn1-old.pdf",
                    "file_url": "https://files.test/tn1-old.pdf",
                    "source_modified_at": "2026-07-17T00:00:00Z",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn1-new.pdf",
                    "file_url": "https://files.test/tn1-new.pdf",
                    "source_modified_at": "2026-07-18T00:00:00Z",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN2",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn2.pdf",
                    "file_url": "https://files.test/tn2.pdf",
                    "source_modified_at": "2026-07-18T00:00:00Z",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                side_effect=lambda _cfg, *, local_path, source_file_url: (
                    source_file_url,
                    None,
                    "local",
                ),
            ):
                result = odoo_push.build_payload_push_summary(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    existing_index=existing_index,
                )

        self.assertEqual(result["input_total"], 3)
        self.assertEqual(result["eligible_document_count"], 3)
        self.assertEqual(result["selected_target_count"], 2)
        self.assertEqual(result["coalesced_count"], 1)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["pending_write_count"], 1)

    def test_preflight_coalesces_before_limit_and_reports_same_metrics(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn1-old.pdf",
                    "file_url": "https://files.test/tn1-old.pdf",
                    "source_modified_at": "2026-07-17T00:00:00Z",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn1-new.pdf",
                    "file_url": "https://files.test/tn1-new.pdf",
                    "source_modified_at": "2026-07-18T00:00:00Z",
                },
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN2",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_name": "tn2.pdf",
                    "file_url": "https://files.test/tn2.pdf",
                    "source_modified_at": "2026-07-18T00:00:00Z",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                side_effect=lambda _cfg, *, local_path, source_file_url: (
                    source_file_url,
                    None,
                    "local",
                ),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=EmptyExistingIndex()
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{"id": 10, "tracking_number": "TN1"}],
            ) as tracking_search:
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                    limit=1,
                )

        self.assertEqual(result["input_total"], 3)
        self.assertEqual(result["eligible_document_count"], 3)
        self.assertEqual(result["selected_target_count"], 2)
        self.assertEqual(result["coalesced_count"], 1)
        self.assertEqual(result["pending_write_count"], 2)
        self.assertEqual(result["selected_for_run_count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["ready_to_push_count"], 1)
        tracking_search.assert_called_once()

    def test_preflight_does_not_count_multiple_tracking_matches_as_writable(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "AMBIGUOUS-TN",
                    "source_name": "EMTC",
                    "source_type": "gdrive",
                    "file_url": "https://files.test/pod.pdf",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/pod.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=EmptyExistingIndex()
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{"id": 1}, {"id": 2}],
            ) as tracking_search, mock.patch.object(
                odoo_push, "search_picking"
            ) as picking_search:
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["ready_to_push_count"], 0)
        self.assertEqual(result["manual_review_count"], 1)
        self.assertEqual(result["preflight_error_count"], 0)
        self.assertEqual(
            result["manual_review"][0]["status"],
            "multiple_tracking_matches",
        )
        tracking_search.assert_called_once_with(
            mock.ANY,
            self._config()["odoo"],
            "AMBIGUOUS-TN",
            expected_delivery_type="spare",
        )
        picking_search.assert_not_called()

    def test_preflight_does_not_count_multi_tracking_picking_as_writable(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WCB2BOUT12345",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_url": "https://files.test/pod.pdf",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/pod.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=EmptyExistingIndex()
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 12345, "name": "WCB2B/OUT/12345"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                return_value=[{"id": 11}, {"id": 12}],
            ):
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["ready_to_push_count"], 0)
        self.assertEqual(result["manual_review_count"], 1)
        self.assertEqual(result["preflight_error_count"], 0)
        self.assertEqual(
            result["manual_review"][0]["status"],
            "multiple_tracking_records",
        )

    def test_preflight_exact_index_hit_still_checks_multi_tracking_cardinality(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="PKG-1",
            picking_name="WCB2B/OUT/12345",
            pod_link="https://files.test/pod.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WCB2BOUT12345",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                    "file_url": "https://files.test/pod.pdf",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/pod.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_picking",
                return_value=[{"id": 12345, "name": "WCB2B/OUT/12345"}],
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking_by_picking_id",
                return_value=[
                    {"id": 11, "pod_link": "https://files.test/pod.pdf"},
                    {"id": 12, "pod_link": None},
                ],
            ):
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertEqual(result["ready_to_push_count"], 0)
        self.assertEqual(result["already_has_pod_count"], 0)
        self.assertEqual(result["manual_review_count"], 1)
        self.assertEqual(
            result["manual_review"][0]["status"],
            "multiple_tracking_records",
        )

    def test_preflight_checks_all_urls_for_tracking_key(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/first.pdf",
        )
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/target.pdf",
        )
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "tracking_number",
                    "preferred_key": "TN1",
                    "source_name": "CMC",
                    "source_type": "onedrive",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[complete_tracking_pod(
                    "https://files.test/target.pdf",
                    id=10,
                    tracking_number="TN1",
                )],
            ) as search_mock, mock.patch.object(
                odoo_push, "search_picking"
            ) as picking_search_mock:
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["ready_to_push_count"], 0)
        self.assertEqual(result["already_has_pod_count"], 1)
        search_mock.assert_called_once()
        picking_search_mock.assert_not_called()

    def test_preflight_marks_same_url_incomplete_state_for_repair(self):
        from webapp.odoo_pull import ExistingPodIndex

        existing_index = ExistingPodIndex()
        existing_index.add_row(
            tracking_number="TN1",
            picking_name=None,
            pod_link="https://files.test/target.pdf",
        )
        payload = {
            "items": [{
                "identifier_status": "ready",
                "preferred_key_type": "tracking_number",
                "preferred_key": "TN1",
                "source_name": "CMC",
                "source_type": "onedrive",
            }]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                odoo_push, "load_config", return_value=self._config()
            ), mock.patch.object(
                odoo_push,
                "resolve_push_file_url",
                return_value=("https://files.test/target.pdf", None, "local"),
            ), mock.patch(
                "webapp.odoo_pull.get_index", return_value=existing_index
            ), mock.patch.object(
                odoo_push,
                "connect_odoo",
                return_value=(FakeCommon(), object()),
            ), mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{
                    "id": 10,
                    "tracking_number": "TN1",
                    "pod_link": "https://files.test/target.pdf",
                    "pod_time": False,
                    "pod_state": "pod",
                    "api_state": "completed",
                }],
            ):
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertEqual(result["already_has_pod_count"], 0)
        self.assertEqual(result["ready_to_push_count"], 1)
        self.assertEqual(
            result["ready_to_push"][0]["status"],
            "ready_repair_tracking",
        )

    def test_preflight_isolates_westerman_when_pool_is_missing(self):
        payload = {
            "items": [
                {
                    "identifier_status": "ready",
                    "preferred_key_type": "picking_name",
                    "preferred_key": "WDB2BOUT01391",
                    "shipment_reference": "WDB2B/OUT/01391",
                    "source_name": "Westerman",
                    "source_type": "westerman_api",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload_path = root / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            full_cfg = self._config()
            full_cfg["westerman_api"] = {
                "enabled": True,
                "pool_path": str(root / "missing.sqlite3"),
            }
            with mock.patch.object(
                odoo_push, "load_config", return_value=full_cfg
            ), mock.patch.object(odoo_push, "connect_odoo") as connect_mock:
                result = odoo_push.preflight_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["westerman_pool_gate_status"], "missing_pool")
        self.assertEqual(result["westerman_pool_filtered"], 1)
        connect_mock.assert_not_called()

    def test_cli_tee_persists_real_signed_url_only_after_redaction(self):
        signed_url = odoo_push.build_signed_storage_url(
            "https://pod.example.test",
            "Westerman/WDB2B/OUT/01391/proof.pdf",
            "t" * 32,
        )
        token = signed_url.rsplit("token=", 1)[1]

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "push.log"
            stdout = io.StringIO()
            with log_path.open("w", encoding="utf-8") as log_fh:
                tee = odoo_push._RedactingTee(stdout, log_fh)
                tee.write(json.dumps({"file_url": signed_url}) + "\n")
            persisted = log_path.read_text(encoding="utf-8")

        self.assertNotIn(token, stdout.getvalue())
        self.assertNotIn(token, persisted)
        self.assertIn("token=[REDACTED]", persisted)

    def test_push_history_and_legacy_log_reads_recursively_redact_tokens(self):
        signed_url = odoo_push.build_signed_storage_url(
            "https://pod.example.test",
            "CMC/proof.pdf",
            "h" * 32,
        )
        token = signed_url.rsplit("token=", 1)[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_dir = root / "runtime" / "odoo_push"
            history_dir.mkdir(parents=True)
            history_file = history_dir / "history.json"
            log_file = history_dir / "push-secret.log"
            log_file.write_text(f"result={signed_url}\n", encoding="utf-8")
            with mock.patch.object(
                odoo_push_history, "PROJECT_ROOT", root
            ), mock.patch.object(
                odoo_push_history, "HISTORY_DIR", history_dir
            ), mock.patch.object(
                odoo_push_history, "HISTORY_FILE", history_file
            ):
                saved = odoo_push_history.append_record(
                    {
                        "push_id": "push-secret",
                        "status": "success",
                        "log_file": "runtime/odoo_push/push-secret.log",
                        "nested": {
                            "file_url": signed_url,
                            "token": token,
                        },
                    }
                )
                public_log = odoo_push_history.get_log_content("push-secret")

            persisted_history = history_file.read_text(encoding="utf-8")

        self.assertNotIn(token, json.dumps(saved))
        self.assertNotIn(token, persisted_history)
        self.assertNotIn(token, public_log)
        self.assertIn("token=[REDACTED]", public_log)

    def test_push_history_persists_partial_outcome_counters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_dir = root / "runtime" / "odoo_push"
            history_file = history_dir / "history.json"
            with mock.patch.object(
                odoo_push_history, "PROJECT_ROOT", root
            ), mock.patch.object(
                odoo_push_history, "HISTORY_DIR", history_dir
            ), mock.patch.object(
                odoo_push_history, "HISTORY_FILE", history_file
            ):
                push_id, _log_path = odoo_push_history.start_push_run(
                    config_path="config.local.json",
                    payload_path="payload.json",
                    dry_run=False,
                    limit=0,
                    source_filter=None,
                )
                saved = odoo_push_history.finish_push_run(
                    push_id,
                    status="partial",
                    result={
                        "total": 5,
                        "input_total": 7,
                        "eligible_document_count": 7,
                        "selected_target_count": 5,
                        "coalesced_count": 2,
                        "superseded_count": 2,
                        "attempted": 5,
                        "completed": 2,
                        "incomplete": 3,
                        "business_status": "attention",
                        "business_pending": 2,
                        "technical_failure_count": 1,
                        "retryable": 1,
                        "manual_review": 2,
                        "ambiguous": 1,
                        "needs_review": 1,
                        "missing_required": 1,
                        "failed": 0,
                    },
                )

        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["input_total"], 7)
        self.assertEqual(saved["eligible_document_count"], 7)
        self.assertEqual(saved["selected_target_count"], 5)
        self.assertEqual(saved["coalesced_count"], 2)
        self.assertEqual(saved["superseded_count"], 2)
        self.assertEqual(saved["attempted"], 5)
        self.assertEqual(saved["completed"], 2)
        self.assertEqual(saved["incomplete"], 3)
        self.assertEqual(saved["business_status"], "attention")
        self.assertEqual(saved["business_pending"], 2)
        self.assertEqual(saved["technical_failure_count"], 1)
        self.assertEqual(saved["retryable"], 1)
        self.assertEqual(saved["manual_review"], 2)
        self.assertEqual(saved["ambiguous"], 1)
        self.assertEqual(saved["needs_review"], 1)
        self.assertEqual(saved["missing_required"], 1)

    def test_push_history_templates_render_partial_separately_from_running(self):
        for relative_path in (
            "webapp/templates/odoo_push.html",
            "webapp/templates/jobs.html",
        ):
            template = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn('rec.status == "partial"', template)
            self.assertIn("部分完成", template)
            self.assertIn("badge-warn", template)
            self.assertIn("coalesced_count", template)

    def test_reconcile_direct_push_endpoint_is_fail_closed(self):
        from webapp import app as app_module

        with mock.patch.object(odoo_push, "push_single_item") as direct_push:
            response = asyncio.run(
                app_module.api_reconcile_push_to_odoo(
                    request=None,
                    match_key="TN1",
                    match_key_type="tracking_number",
                    file_url="https://files.test/tn1.pdf",
                    local_path="CMC/tn1.pdf",
                    source_name="CMC",
                )
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "RECONCILE_DIRECT_PUSH_DISABLED")
        self.assertEqual(body["odoo_push_url"], "/odoo-push")
        direct_push.assert_not_called()

    def test_reconcile_template_has_no_direct_odoo_write_control(self):
        template = (
            PROJECT_ROOT / "webapp/templates/emtc_reconcile.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("api_reconcile_push_to_odoo", template)
        self.assertNotIn("pushToOdoo", template)
        self.assertNotIn("push-to-odoo-btn", template)
        self.assertIn("打开标准推送页", template)

    def test_action_odoo_push_rejects_unconfirmed_real_write(self):
        from webapp import app as app_module

        with mock.patch.object(
            app_module, "build_odoo_push_command"
        ) as build_command, mock.patch.object(
            app_module.job_store, "start_job"
        ) as start_job:
            response = asyncio.run(
                app_module.action_odoo_push(
                    config_path="config.local.json",
                    payload_path="payload.json",
                    dry_run=False,
                    confirm_write=False,
                    limit=0,
                    missing_only=True,
                    source_filter="",
                )
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/odoo-push?error=", response.headers["location"])
        build_command.assert_not_called()
        start_job.assert_not_called()

    def test_action_odoo_push_allows_preview_and_confirmed_write(self):
        from webapp import app as app_module

        command_result = (
            "Odoo push",
            ["python", "webapp/odoo_push.py"],
            {"mode": "odoo_push"},
        )
        with mock.patch.object(
            app_module,
            "build_odoo_push_command",
            return_value=command_result,
        ) as build_command, mock.patch.object(
            app_module.job_store,
            "start_job",
            side_effect=[{"job_id": "preview"}, {"job_id": "write"}],
        ) as start_job:
            preview_response = asyncio.run(
                app_module.action_odoo_push(
                    config_path="config.local.json",
                    payload_path="payload.json",
                    dry_run=True,
                    confirm_write=False,
                    limit=0,
                    missing_only=True,
                    source_filter="",
                )
            )
            write_response = asyncio.run(
                app_module.action_odoo_push(
                    config_path="config.local.json",
                    payload_path="payload.json",
                    dry_run=False,
                    confirm_write=True,
                    limit=0,
                    missing_only=True,
                    source_filter="",
                )
            )

        self.assertEqual(preview_response.headers["location"], "/jobs/preview")
        self.assertEqual(write_response.headers["location"], "/jobs/write")
        self.assertEqual(build_command.call_count, 2)
        self.assertTrue(build_command.call_args_list[0].kwargs["dry_run"])
        self.assertFalse(build_command.call_args_list[1].kwargs["dry_run"])
        self.assertEqual(start_job.call_count, 2)

    def test_push_template_defaults_to_preview_and_requires_write_confirmation(self):
        template = (PROJECT_ROOT / "webapp/templates/odoo_push.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="odooPushDryRun"', template)
        self.assertIn('name="dry_run" value="on" checked', template)
        self.assertIn("window.confirm", template)
        self.assertIn('confirmation.name = "confirm_write"', template)
        self.assertIn("api_odoo_push_preflight", template)
        self.assertIn("selected_target_count", template)
        self.assertIn("pending_write_count", template)
        self.assertIn("manual_review_count", template)
        self.assertIn("preflight_error_count", template)

    def test_delete_push_run_removes_record_and_local_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_dir = root / "runtime" / "odoo_push"
            history_dir.mkdir(parents=True)
            history_file = history_dir / "history.json"
            log_file = history_dir / "push-a.log"
            log_file.write_text("log", encoding="utf-8")
            history_file.write_text(
                json.dumps(
                    [
                        {
                            "push_id": "push-a",
                            "status": "success",
                            "log_file": "runtime/odoo_push/push-a.log",
                        },
                        {
                            "push_id": "push-b",
                            "status": "failed",
                            "log_file": "runtime/odoo_push/push-b.log",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with mock.patch.object(odoo_push_history, "PROJECT_ROOT", root), \
                mock.patch.object(odoo_push_history, "HISTORY_DIR", history_dir), \
                mock.patch.object(odoo_push_history, "HISTORY_FILE", history_file):
                ok, error = odoo_push_history.delete_push_run("push-a")

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertFalse(log_file.exists())
            remaining = json.loads(history_file.read_text(encoding="utf-8"))
            self.assertEqual([rec["push_id"] for rec in remaining], ["push-b"])


if __name__ == "__main__":
    unittest.main()
