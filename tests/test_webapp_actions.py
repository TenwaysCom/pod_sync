import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import actions


class WebAppActionTests(unittest.TestCase):
    def test_build_cmc_onedrive_download_all_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"cmc_onedrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_cmc_onedrive_command(
                config_path=str(config_path),
                limit=0,
                recursive=True,
                mode="download_all_files",
                download_file_id="",
                output_dir=str(tmp / "downloads"),
                workers=3,
                incremental=True,
            )

            self.assertEqual(label, "CMC OneDrive / 下载全部文件")
            self.assertIn("--download-all-files", command)
            self.assertIn("--recursive", command)
            self.assertIn("--workers", command)
            self.assertEqual(metadata["workers"], 3)
            self.assertEqual(metadata["mode"], "download_all_files")

    def test_build_emtc_gdrive_download_all_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"emtc_gdrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_emtc_gdrive_command(
                config_path=str(config_path),
                limit=0,
                recursive=True,
                mode="download_all_files",
                download_file_id="",
                output_dir=str(tmp / "downloads"),
                workers=4,
                export_mime_type="application/pdf",
                incremental=True,
            )

            self.assertEqual(label, "EMTC Google Drive / 下载全部文件")
            self.assertIn("--download-all-files", command)
            self.assertIn("--recursive", command)
            self.assertIn("--workers", command)
            self.assertIn("4", command)
            self.assertEqual(metadata["workers"], 4)
            self.assertEqual(metadata["mode"], "download_all_files")

    def test_build_phase1a_command_requires_existing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "phase1a.json"
            config_path.write_text(json.dumps({"output_dir": "./out/demo"}), encoding="utf-8")

            label, command, metadata = actions.build_phase1a_command(config_path=str(config_path))

            self.assertEqual(label, "运行 POD 识别")
            self.assertEqual(command[-2:], ["--config", str(config_path.resolve())])
            self.assertEqual(metadata["phase1a_config_path"], str(config_path.resolve()))

    def test_build_refresh_remote_counts_command_requires_existing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"cmc_onedrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_refresh_remote_counts_command(config_path=str(config_path))

            self.assertEqual(label, "刷新远端数量对比")
            self.assertEqual(command[-2:], ["--config", str(config_path.resolve())])
            self.assertIn("refresh_remote_counts.py", command[1])
            self.assertEqual(metadata["action_type"], "refresh_remote_counts")

    def test_load_runtime_config_snapshot_masks_secret_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": "./storage/pod"},
                        "cmc_onedrive": {
                            "enabled": True,
                            "warehouse": "CMC",
                            "access_token": "abc",
                            "refresh_token": "def",
                            "root_item_id": "item-1",
                        },
                        "emtc_gdrive": {
                            "enabled": True,
                            "warehouse": "EMTC",
                            "access_token": "",
                            "refresh_token": "",
                            "folder_id": "folder-1",
                        },
                        "westerman_api": {
                            "enabled": False,
                            "warehouse": "Westerman",
                            "api_key": "secret",
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = actions.load_runtime_config_snapshot(str(config_path))

            self.assertTrue(snapshot["exists"])
            self.assertEqual(snapshot["sources"][0]["has_access_token"], True)
            self.assertEqual(snapshot["sources"][0]["has_secret"], True)
            self.assertEqual(snapshot["sources"][1]["has_folder_id"], True)
            self.assertEqual(snapshot["sources"][2]["has_secret"], True)
            self.assertEqual(snapshot["storage_dir"], str((tmp / "storage" / "pod").resolve()))

    def test_resolve_storage_dir_uses_config_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"download": {"base_dir": "./downloads/pod"}}), encoding="utf-8")

            resolved = actions.resolve_storage_dir(str(config_path))

            self.assertEqual(resolved, (tmp / "downloads" / "pod").resolve())


if __name__ == "__main__":
    unittest.main()
