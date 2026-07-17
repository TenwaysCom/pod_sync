import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    module_path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_gdrive = load_module("probe_gdrive_sync_tests", "clients/emtc_gdrive/probe.py")
sync_integrity = load_module("sync_integrity", "clients/common/sync_integrity.py")
verify_sync_integrity = load_module("verify_sync_integrity", "ops/verify_sync_integrity.py")


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200, headers=None, url="https://example.test"):
        self._json_data = json_data
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._json_data is None:
            raise ValueError("Response does not contain JSON.")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 256):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)

    def post(self, url, data=None, headers=None, timeout=None):
        if not self._responses:
            raise AssertionError(f"Unexpected POST request: {url}")
        return self._responses.pop(0)

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        if not self._responses:
            raise AssertionError(f"Unexpected GET request: {url}")
        return self._responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SyncIntegrityTests(unittest.TestCase):
    def test_manifest_append_is_safe_for_concurrent_workers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "westerman_api.jsonl"

            def append(index):
                sync_integrity.append_manifest_entry(
                    manifest_path,
                    {"source_file_id": str(index), "payload": "x" * 2048},
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(append, range(64)))

            entries = sync_integrity.load_manifest_entries(manifest_path)
            self.assertEqual(len(entries), 64)
            self.assertEqual(
                {entry["source_file_id"] for entry in entries},
                {str(index) for index in range(64)},
            )

    def test_normalize_runtime_config_resolves_project_storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": "./storage/pod"},
                        "gdrive": {"enabled": True, "warehouse": "EMTC"},
                    }
                ),
                encoding="utf-8",
            )

            normalized = sync_integrity.normalize_runtime_config(
                json.loads(config_path.read_text(encoding="utf-8")),
                config_path,
            )
            self.assertEqual(normalized["download"]["base_dir"], str((tmp_path / "storage" / "pod").resolve()))

    def test_gdrive_download_writes_manifest_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": str(tmp_path / "pod")},
                        "gdrive": {
                            "access_token": "token-123",
                            "warehouse": "EMTC",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        },
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "files": [
                                {
                                    "id": "file-1",
                                    "name": "proof.pdf",
                                    "mimeType": "application/pdf",
                                    "size": "18",
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"pdf-binary-content"),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
                "--download-first-file",
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            manifest_path = tmp_path / "pod" / "_integrity" / "EMTC" / "gdrive.jsonl"
            self.assertTrue(manifest_path.exists())
            entries = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["source"], "gdrive")
            self.assertEqual(entry["warehouse"], "EMTC")
            self.assertEqual(entry["source_file_id"], "file-1")
            self.assertEqual(entry["source_size"], 18)
            self.assertTrue(entry["verify_remote_size"])
            self.assertEqual(entry["local_size"], len(b"pdf-binary-content"))
            self.assertEqual(entry["sha256"], hashlib.sha256(b"pdf-binary-content").hexdigest())

    def test_verify_sync_integrity_reports_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            base_dir = tmp_path / "pod"
            local_file = base_dir / "EMTC" / "proof.pdf"
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_bytes(b"abc")

            manifest_path = base_dir / "_integrity" / "EMTC" / "gdrive.jsonl"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "source": "gdrive",
                        "warehouse": "EMTC",
                        "source_file_id": "file-1",
                        "file_name": "proof.pdf",
                        "local_path": str(local_file.resolve()),
                        "local_size": 3,
                        "sha256": hashlib.sha256(b"abc").hexdigest(),
                        "verify_remote_size": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": str(base_dir)},
                        "gdrive": {"enabled": True, "warehouse": "EMTC"},
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            argv = ["verify_sync_integrity.py", "--config", str(config_path), "--source", "gdrive"]
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout):
                    verify_sync_integrity.main()

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["failed_records"], 0)
            self.assertEqual(payload["sources"][0]["status"], "ok")

    def test_verify_sync_integrity_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            base_dir = tmp_path / "pod"
            local_file = base_dir / "EMTC" / "proof.pdf"
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_bytes(b"abd")

            manifest_path = base_dir / "_integrity" / "EMTC" / "gdrive.jsonl"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "source": "gdrive",
                        "warehouse": "EMTC",
                        "source_file_id": "file-1",
                        "file_name": "proof.pdf",
                        "local_path": str(local_file.resolve()),
                        "local_size": 3,
                        "sha256": hashlib.sha256(b"abc").hexdigest(),
                        "verify_remote_size": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": str(base_dir)},
                        "gdrive": {"enabled": True, "warehouse": "EMTC"},
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            argv = ["verify_sync_integrity.py", "--config", str(config_path), "--source", "gdrive"]
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as exc:
                        verify_sync_integrity.main()

            self.assertEqual(exc.exception.code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["failed_records"], 1)
            self.assertEqual(payload["sources"][0]["results"][0]["checks"], ["sha256_mismatch"])


if __name__ == "__main__":
    unittest.main()
