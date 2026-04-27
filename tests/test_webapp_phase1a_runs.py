import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import phase1a_runs


class Phase1aRunDiscoveryTests(unittest.TestCase):
    def test_discover_phase1a_runs_sorts_latest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_root = tmp / "out"
            older = out_root / "older"
            newer = out_root / "newer"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)

            (older / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T01:00:00Z", "document_count": 2, "artifacts": {}}),
                encoding="utf-8",
            )
            (newer / "phase1a_summary.json").write_text(
                json.dumps({"generated_at": "2026-04-27T03:00:00Z", "document_count": 3, "artifacts": {}}),
                encoding="utf-8",
            )

            with mock.patch.object(phase1a_runs, "PHASE1A_OUT_ROOT", out_root):
                runs = phase1a_runs.discover_phase1a_runs(limit=10)

            self.assertEqual([item["run_name"] for item in runs], ["newer", "older"])

    def test_resolve_phase1a_summary_path_uses_config_relative_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_dir = tmp / "configs"
            output_dir = tmp / "phase1a-out" / "demo"
            config_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            summary_path = output_dir / "phase1a_summary.json"
            summary_path.write_text(json.dumps({"document_count": 1}), encoding="utf-8")

            config_path = config_dir / "phase1a.json"
            config_path.write_text(json.dumps({"output_dir": "../phase1a-out/demo"}), encoding="utf-8")

            resolved = phase1a_runs.resolve_phase1a_summary_path(config_path)

            self.assertEqual(resolved, summary_path)


if __name__ == "__main__":
    unittest.main()
