import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import odoo_push


class FakeCommon:
    def version(self):
        return {"server_version": "test"}


class OdooPushTests(unittest.TestCase):
    def _config(self):
        return {"odoo": {"enabled": True, "url": "https://odoo.test", "db": "db", "uid": 1, "api_key": "key"}}

    def test_push_single_item_rejects_multiple_tracking_matches(self):
        with mock.patch.object(odoo_push, "load_config", return_value=self._config()), \
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

    def test_local_file_url_rejects_placeholder_secret(self):
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

            self.assertIsNone(odoo_push.get_local_file_url(cfg, str(local_file)))

    def test_payload_lookup_key_converts_canonical_picking_name(self):
        key_type, key_value = odoo_push._payload_lookup_key(
            {"preferred_key_type": "picking_name", "preferred_key": "WCB2BOUT33271"}
        )

        self.assertEqual(key_type, "picking_name")
        self.assertEqual(key_value, "WCB2B/OUT/33271")


if __name__ == "__main__":
    unittest.main()
