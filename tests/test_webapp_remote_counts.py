import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops import refresh_remote_counts
from webapp.remote_counts import load_remote_count_snapshot, write_remote_count_snapshot


class WebAppRemoteCountTests(unittest.TestCase):
    def test_load_remote_count_snapshot_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = load_remote_count_snapshot(Path(tmpdir) / "missing.json")

            self.assertFalse(snapshot["exists"])
            self.assertEqual(snapshot["sources"], [])

    def test_write_and_load_remote_count_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "remote_counts.json"
            payload = {
                "generated_at": "2026-04-27T12:00:00Z",
                "config_path": "/tmp/config.local.json",
                "sources": [
                    {
                        "key": "cmc_onedrive",
                        "label": "CMC OneDrive",
                        "remote_file_count": 10,
                        "local_file_count": 10,
                        "delta_local_vs_remote": 0,
                    }
                ],
            }

            write_remote_count_snapshot(payload, path)
            snapshot = load_remote_count_snapshot(path)

            self.assertTrue(snapshot["exists"])
            self.assertEqual(snapshot["generated_at"], payload["generated_at"])
            self.assertEqual(snapshot["config_path"], payload["config_path"])
            self.assertEqual(snapshot["sources"][0]["remote_file_count"], 10)

    def test_count_gdrive_remote_uses_refreshing_session_headers(self):
        fake_raw_session = mock.Mock()
        fake_raw_session.__enter__ = mock.Mock(return_value=fake_raw_session)
        fake_raw_session.__exit__ = mock.Mock(return_value=False)
        fake_session = mock.Mock()
        fake_session.prepare_headers.return_value = {"Authorization": "Bearer test"}

        with mock.patch.object(
            refresh_remote_counts.gdrive_probe,
            "load_config",
            return_value=({"emtc_gdrive": {}}, {"timeout_seconds": 30}, {}),
        ), mock.patch.object(
            refresh_remote_counts.gdrive_probe,
            "RefreshingGDriveSession",
            return_value=fake_session,
        ), mock.patch.object(
            refresh_remote_counts.gdrive_probe,
            "has_refresh_flow",
            return_value=True,
        ), mock.patch.object(
            refresh_remote_counts.gdrive_probe,
            "iter_items",
            return_value=[
                {"is_folder": True},
                {"is_folder": False},
                {"is_folder": False},
            ],
        ) as iter_items_mock, mock.patch.object(
            refresh_remote_counts.gdrive_probe,
            "normalize_limit",
            return_value=None,
        ), mock.patch.object(
            refresh_remote_counts.requests,
            "Session",
            return_value=fake_raw_session,
        ):
            result = refresh_remote_counts.count_gdrive_remote("config.local.json")

        self.assertEqual(result, {"remote_file_count": 2, "remote_folder_count": 1})
        fake_session.prepare_headers.assert_called_once_with(force_refresh=True)
        self.assertEqual(iter_items_mock.call_args.kwargs["headers"], {"Authorization": "Bearer test"})


if __name__ == "__main__":
    unittest.main()
