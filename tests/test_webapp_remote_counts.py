import json
import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


if __name__ == "__main__":
    unittest.main()
