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


class OdooPushTests(unittest.TestCase):
    def _config(self):
        return {"odoo": {"enabled": True, "url": "https://odoo.test", "db": "db", "uid": 1, "api_key": "key"}}

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
                        "pod_link": "https://files.test/pod.pdf",
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
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
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
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
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
                return_value=[{"id": 10, "tracking_number": "TN1", "pod_link": None}],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod"), \
            mock.patch("webapp.odoo_pull.load_saved_index", return_value=index), \
            mock.patch("webapp.odoo_pull.save_index") as save_mock:
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
        save_mock.assert_called_once_with(index)

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
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
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
                return_value=[{
                    "id": 10,
                    "tracking_number": "TN1",
                    "pod_link": "https://files.test/pod.pdf",
                }],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/pod.pdf",
            )

        self.assertEqual(result["status"], "skipped_same")
        write_mock.assert_not_called()

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
                odoo_push, "connect_odoo"
            ) as connect_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                    missing_only=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["details"], [])
        connect_mock.assert_not_called()

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
                    return_value=[{
                        "id": 10,
                        "tracking_number": "TN1",
                        "pod_link": "https://files.test/pod.pdf",
                    }],
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

            self.assertTrue(result["ok"])
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["westerman_pool_gate_status"], "missing_pool")
            self.assertEqual(result["westerman_pool_filtered"], 1)
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

            self.assertTrue(result["ok"])
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

                    self.assertTrue(result["ok"])
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

        self.assertTrue(result["ok"])
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
