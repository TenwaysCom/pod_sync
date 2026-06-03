import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_push
from webapp import odoo_push_history


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

    def test_push_single_item_does_not_overwrite_different_existing_pod(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
            mock.patch.object(odoo_push, "resolve_push_file_url", return_value=("https://files.test/new.pdf", None, "local")), \
            mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
            mock.patch.object(
                odoo_push,
                "search_stock_tracking",
                return_value=[{"id": 10, "tracking_number": "TN1", "pod_link": "https://files.test/old.pdf"}],
            ), \
            mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
            result = odoo_push.push_single_item(
                config_path="config.local.json",
                match_key="TN1",
                match_key_type="tracking_number",
                file_url="https://files.test/new.pdf",
            )

        self.assertEqual(result["status"], "needs_review")
        self.assertIn("different pod_link", result["error"])
        write_mock.assert_not_called()

    def test_push_payload_does_not_overwrite_different_existing_pod(self):
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
                mock.patch("webapp.odoo_pull.get_index", return_value=EmptyExistingIndex()), \
                mock.patch.object(odoo_push, "connect_odoo", return_value=(FakeCommon(), object())), \
                mock.patch.object(
                    odoo_push,
                    "search_stock_tracking",
                    return_value=[{"id": 10, "tracking_number": "TN1", "pod_link": "https://files.test/old.pdf"}],
                ), \
                mock.patch.object(odoo_push, "write_tracking_pod") as write_mock:
                result = odoo_push.push_payload(
                    config_path="config.local.json",
                    payload_path=str(payload_path),
                )

        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(result["details"][0]["status"], "needs_review")
        self.assertIn("different pod_link", result["details"][0]["error"])
        write_mock.assert_not_called()

    def test_payload_lookup_key_converts_canonical_picking_name(self):
        key_type, key_value = odoo_push._payload_lookup_key(
            {"preferred_key_type": "picking_name", "preferred_key": "WCB2BOUT33271"}
        )

        self.assertEqual(key_type, "picking_name")
        self.assertEqual(key_value, "WCB2B/OUT/33271")

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
