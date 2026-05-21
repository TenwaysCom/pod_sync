import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    module_path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_gdrive = load_module("probe_gdrive", "clients/emtc_gdrive/probe.py")
probe_onedrive = load_module("probe_onedrive", "clients/cmc_onedrive/probe.py")


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200, headers=None, text=None, url="https://example.test"):
        self._json_data = json_data
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self._text = text if text is not None else content.decode("utf-8", errors="ignore")

    def json(self):
        if self._json_data is None:
            raise ValueError("Response does not contain JSON.")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 256):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]

    @property
    def text(self):
        return self._text


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError(f"Unexpected POST request: {url}")
        return self._responses.pop(0)

    def get(self, url, headers=None, params=None, timeout=None, stream=False, **kwargs):
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
                "stream": stream,
                **kwargs,
            }
        )
        if not self._responses:
            raise AssertionError(f"Unexpected GET request: {url}")
        return self._responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DriveDownloadTests(unittest.TestCase):
    def test_onedrive_normalizes_redeem_wrapper_url(self):
        wrapped_url = (
            "https://onedrive.live.com/?redeem="
            "aHR0cHM6Ly8xZHJ2Lm1zL2YvYy8zYmIxY2MyYTZhZDM5ZTRjL0lnQk1udE5xS3N5eElJQTdlUXdDQUFBQUFiRm1y"
            "ZDVYSWhpRktaMUFhcENjUnRRP2U9NTo2Y3JLM3I="
        )
        expected_url = "https://1drv.ms/f/c/3bb1cc2a6ad39e4c/IgBMntNqKsyxIIA7eQwCAAAAAbFmrd5XIhiFKZ1AapCcRtQ?e=5:6crK3r"
        self.assertEqual(probe_onedrive.normalize_shared_url(wrapped_url), expected_url)

    def test_onedrive_keeps_final_live_url_with_validatepermission(self):
        live_url = (
            "https://onedrive.live.com/?cid=3bb1cc2a6ad39e4c&id=3BB1CC2A6AD39E4C%21134265"
            "&resid=3BB1CC2A6AD39E4C%21134265&ithint=folder&migratedtospo=true"
            "&redeem=aHR0cHM6Ly8xZHJ2Lm1zL2YvYy8zYmIxY2MyYTZhZDM5ZTRjL0lnQk1udE5xS3N5eElJQTdlUXdDQUFBQUFiRm1yZDVYSWhpRktaMUFhcENjUnRR"
            "&v=validatepermission"
        )
        self.assertEqual(probe_onedrive.normalize_shared_url(live_url), live_url)

    def test_onedrive_sanitizes_tracking_query_params_from_1drv_url(self):
        noisy_url = (
            "https://1drv.ms/f/c/3bb1cc2a6ad39e4c/IgBMntNqKsyxIIA7eQwCAAAAAbFmrd5XIhiFKZ1AapCcRtQ"
            "?e=5:6crK3r&sharingv2=true&fromShare=true&at=9&wdOrigin=OWA.LINK"
        )
        expected_url = "https://1drv.ms/f/c/3bb1cc2a6ad39e4c/IgBMntNqKsyxIIA7eQwCAAAAAbFmrd5XIhiFKZ1AapCcRtQ?e=5%3A6crK3r"
        self.assertEqual(probe_onedrive.sanitize_shared_url(noisy_url), expected_url)

    def test_onedrive_retries_transient_skiptoken_400_and_keeps_nextlink_intact(self):
        next_link = "https://graph.microsoft.com/v1.0/drives/drive/items/folder/children?%24skiptoken=abc%2Bdef"
        fake_session = FakeSession(
            [
                FakeResponse(
                    json_data={
                        "value": [{"id": "file-1", "name": "one.pdf"}],
                        "@odata.nextLink": next_link,
                    }
                ),
                FakeResponse(status_code=400, text='{"error":{"code":"BadRequest"}}'),
                FakeResponse(json_data={"value": [{"id": "file-2", "name": "two.pdf"}]}),
            ]
        )

        with mock.patch.object(probe_onedrive.time, "sleep") as sleep_mock:
            items = list(
                probe_onedrive.iter_collection(
                    fake_session,
                    "https://graph.microsoft.com/v1.0/drives/drive/items/folder/children",
                    {"Authorization": "Bearer token"},
                    10,
                )
            )

        self.assertEqual([item["id"] for item in items], ["file-1", "file-2"])
        self.assertEqual(fake_session.calls[1]["url"], next_link)
        self.assertIsNone(fake_session.calls[1]["params"])
        self.assertEqual(fake_session.calls[2]["url"], next_link)
        sleep_mock.assert_called_once()

    def test_onedrive_emit_redacts_temporary_download_url_from_stdout(self):
        download_url = "https://my.microsoftpersonalcontent.com/file.pdf?tempauth=secret-token"
        item = {
            "id": "file-1",
            "name": "proof.pdf",
            "file": {"mimeType": "application/pdf"},
            "size": 10,
            "parentReference": {"path": "/drive/root:/CMC"},
            "@microsoft.graph.downloadUrl": download_url,
        }

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            output = probe_onedrive.emit_item(item, full_path="/CMC/proof.pdf")

        printed = json.loads(stdout.getvalue())
        self.assertEqual(output["download_url"], download_url)
        self.assertNotIn("download_url", printed)
        self.assertNotIn("tempauth", stdout.getvalue())

    def test_onedrive_lists_public_authkey_share_without_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            shared_url = (
                "https://onedrive.live.com/?cid=3bb1cc2a6ad39e4c&id=3BB1CC2A6AD39E4C!134265"
                "&resid=3BB1CC2A6AD39E4C!134265&migratedtospo=true&authkey=!public-key"
            )
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "access_token": "stale-token",
                            "shared_url": shared_url,
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(url=shared_url),
                    FakeResponse(
                        json_data={
                            "id": "folder-1",
                            "name": "Tenways-CMC POD",
                            "folder": {"childCount": 1},
                            "parentReference": {"driveId": "shared-drive-id"},
                        }
                    ),
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "child-file-1",
                                    "name": "proof-001.pdf",
                                    "file": {"mimeType": "application/pdf"},
                                    "size": 120,
                                    "parentReference": {
                                        "driveId": "shared-drive-id",
                                        "path": "/drive/root:/Tenways-CMC POD",
                                    },
                                }
                            ]
                        }
                    ),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            self.assertEqual(fake_session.calls[1]["headers"], None)
            self.assertTrue(
                fake_session.calls[1]["url"].startswith("https://my.microsoftpersonalcontent.com/_api/v2.1/shares/")
            )
            self.assertIn("authKey=%21public-key", fake_session.calls[1]["url"])

    def test_onedrive_rejects_public_short_share_without_authkey_and_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "shared_url": "https://1drv.ms/f/c/3bb1cc2a6ad39e4c/IgBMntNqKsyxIIA7eQwCAAAAAbFmrd5XIhiFKZ1AapCcRtQ",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        url=(
                            "https://onedrive.live.com/?cid=3bb1cc2a6ad39e4c&id=3BB1CC2A6AD39E4C!134265"
                            "&resid=3BB1CC2A6AD39E4C!134265&migratedtospo=true"
                            "&redeem=aHR0cHM6Ly8xZHJ2Lm1zL2YvYy8zYmIxY2MyYTZhZDM5ZTRjL0lnQk1udE5xS3N5eElJQTdlUXdDQUFBQUFiRm1yZDVYSWhpRktaMUFhcENjUnRR"
                        )
                    )
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as exc:
                        with contextlib.redirect_stdout(io.StringIO()):
                            probe_onedrive.main()

            self.assertIn("authkey", str(exc.exception).lower())

    def test_onedrive_refreshes_access_token_before_listing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "client_id": "client-id",
                            "refresh_token": "refresh-token",
                            "root_path": "/CMC/POD",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(json_data={"access_token": "fresh-token", "refresh_token": "rotated-refresh"}),
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "item-1",
                                    "name": "proof.pdf",
                                    "file": {"mimeType": "application/pdf"},
                                }
                            ]
                        }
                    ),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            refreshed_cfg = json.loads(config_path.read_text(encoding="utf-8"))["onedrive"]
            self.assertEqual(fake_session.calls[0]["url"], "https://login.microsoftonline.com/common/oauth2/v2.0/token")
            self.assertEqual(fake_session.calls[0]["data"]["grant_type"], "refresh_token")
            self.assertEqual(fake_session.calls[1]["headers"], {"Authorization": "Bearer fresh-token"})
            self.assertEqual(refreshed_cfg["access_token"], "fresh-token")
            self.assertEqual(refreshed_cfg["refresh_token"], "rotated-refresh")

    def test_onedrive_retries_after_401_with_refreshed_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "access_token": "stale-token",
                            "client_id": "client-id",
                            "refresh_token": "refresh-token",
                            "root_path": "/CMC/POD",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(json_data={"access_token": "startup-token", "refresh_token": "startup-refresh"}),
                    FakeResponse(status_code=401),
                    FakeResponse(json_data={"access_token": "retry-token", "refresh_token": "retry-refresh"}),
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "item-1",
                                    "name": "proof.pdf",
                                    "file": {"mimeType": "application/pdf"},
                                }
                            ]
                        }
                    ),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            refreshed_cfg = json.loads(config_path.read_text(encoding="utf-8"))["onedrive"]
            self.assertEqual(fake_session.calls[1]["headers"], {"Authorization": "Bearer startup-token"})
            self.assertEqual(fake_session.calls[3]["headers"], {"Authorization": "Bearer retry-token"})
            self.assertEqual(refreshed_cfg["access_token"], "retry-token")
            self.assertEqual(refreshed_cfg["refresh_token"], "retry-refresh")

    def test_gdrive_downloads_first_file_to_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gdrive": {
                            "access_token": "token-123",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        }
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
                                    "createdTime": "2026-04-20T00:00:00Z",
                                    "modifiedTime": "2026-04-21T00:00:00Z",
                                    "webViewLink": "https://drive.google.test/view/file-1",
                                    "webContentLink": "https://drive.google.test/content/file-1",
                                    "parents": ["folder-abc"],
                                    "driveId": "drive-1",
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"pdf-binary-content"),
                ]
            )

            stdout = io.StringIO()
            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "5",
                "--download-first-file",
                "--output-dir",
                str(tmp_path / "downloads"),
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(stdout):
                        probe_gdrive.main()

            downloaded_file = tmp_path / "downloads" / "proof.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"pdf-binary-content")
            self.assertEqual(fake_session.calls[1]["url"], "https://www.googleapis.com/drive/v3/files/file-1")
            self.assertEqual(fake_session.calls[1]["params"], {"alt": "media", "supportsAllDrives": "true"})

    def test_gdrive_uses_configured_download_base_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {
                            "base_dir": str(tmp_path / "pod"),
                        },
                        "gdrive": {
                            "access_token": "token-123",
                            "warehouse": "EMTC",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        }
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

            downloaded_file = tmp_path / "pod" / "EMTC" / "proof.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"pdf-binary-content")

    def test_gdrive_refreshes_access_token_before_listing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gdrive": {
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "refresh_token": "refresh-token",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(json_data={"access_token": "fresh-token", "refresh_token": "rotated-refresh-token"}),
                    FakeResponse(
                        json_data={
                            "files": [
                                {
                                    "id": "file-1",
                                    "name": "proof.pdf",
                                    "mimeType": "application/pdf",
                                }
                            ]
                        }
                    ),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            refreshed_cfg = json.loads(config_path.read_text(encoding="utf-8"))["gdrive"]
            self.assertEqual(fake_session.calls[0]["url"], "https://oauth2.googleapis.com/token")
            self.assertEqual(fake_session.calls[0]["data"]["grant_type"], "refresh_token")
            self.assertEqual(fake_session.calls[1]["headers"], {"Authorization": "Bearer fresh-token"})
            self.assertEqual(refreshed_cfg["access_token"], "fresh-token")
            self.assertEqual(refreshed_cfg["refresh_token"], "rotated-refresh-token")

    def test_gdrive_retries_download_after_401_with_refreshed_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gdrive": {
                            "access_token": "stale-token",
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "refresh_token": "refresh-token",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(json_data={"access_token": "startup-token", "refresh_token": "startup-refresh"}),
                    FakeResponse(
                        json_data={
                            "files": [
                                {
                                    "id": "file-1",
                                    "name": "proof.pdf",
                                    "mimeType": "application/pdf",
                                }
                            ]
                        }
                    ),
                    FakeResponse(status_code=401),
                    FakeResponse(json_data={"access_token": "retry-token", "refresh_token": "retry-refresh"}),
                    FakeResponse(content=b"retried-gdrive-content"),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
                "--download-first-file",
                "--output-dir",
                str(tmp_path / "downloads"),
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            refreshed_cfg = json.loads(config_path.read_text(encoding="utf-8"))["gdrive"]
            downloaded_file = tmp_path / "downloads" / "proof.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"retried-gdrive-content")
            self.assertEqual(fake_session.calls[1]["headers"], {"Authorization": "Bearer startup-token"})
            self.assertEqual(fake_session.calls[2]["headers"], {"Authorization": "Bearer startup-token"})
            self.assertEqual(fake_session.calls[4]["headers"], {"Authorization": "Bearer retry-token"})
            self.assertEqual(refreshed_cfg["access_token"], "retry-token")
            self.assertEqual(refreshed_cfg["refresh_token"], "retry-refresh")

    def test_gdrive_falls_back_to_existing_access_token_when_refresh_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gdrive": {
                            "access_token": "existing-token",
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "refresh_token": "refresh-token",
                            "folder_id": "folder-abc",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(status_code=401),
                    FakeResponse(
                        json_data={
                            "files": [
                                {
                                    "id": "file-1",
                                    "name": "proof.pdf",
                                    "mimeType": "application/pdf",
                                }
                            ]
                        }
                    ),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            self.assertEqual(fake_session.calls[1]["headers"], {"Authorization": "Bearer existing-token"})

    def test_gdrive_downloads_specific_file_id_without_listing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gdrive": {
                            "access_token": "token-123",
                            "timeout_seconds": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "id": "file-2",
                            "name": "proof-direct.pdf",
                            "mimeType": "application/pdf",
                            "size": "21",
                            "createdTime": "2026-04-20T00:00:00Z",
                            "modifiedTime": "2026-04-21T00:00:00Z",
                            "parents": [],
                            "driveId": "drive-1",
                        }
                    ),
                    FakeResponse(content=b"direct-gdrive-content"),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--download-file-id",
                "file-2",
                "--output-dir",
                str(tmp_path / "downloads"),
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            downloaded_file = tmp_path / "downloads" / "proof-direct.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"direct-gdrive-content")
            self.assertEqual(fake_session.calls[0]["url"], "https://www.googleapis.com/drive/v3/files/file-2")

    def test_gdrive_downloads_all_files_recursively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {
                            "base_dir": str(tmp_path / "pod"),
                        },
                        "gdrive": {
                            "access_token": "token-123",
                            "warehouse": "EMTC",
                            "folder_id": "folder-root",
                            "timeout_seconds": 15,
                        }
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
                                    "id": "folder-2026",
                                    "name": "2026",
                                    "mimeType": "application/vnd.google-apps.folder",
                                },
                                {
                                    "id": "file-top",
                                    "name": "top.pdf",
                                    "mimeType": "application/pdf",
                                    "size": "3",
                                },
                            ]
                        }
                    ),
                    FakeResponse(content=b"top"),
                    FakeResponse(
                        json_data={
                            "files": [
                                {
                                    "id": "file-nested",
                                    "name": "nested.png",
                                    "mimeType": "image/png",
                                    "size": "6",
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"nested"),
                ]
            )

            argv = [
                "probe_gdrive.py",
                "--config",
                str(config_path),
                "--limit",
                "0",
                "--recursive",
                "--download-all-files",
            ]

            with mock.patch.object(probe_gdrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_gdrive.main()

            self.assertTrue((tmp_path / "pod" / "EMTC" / "top.pdf").exists())
            self.assertTrue((tmp_path / "pod" / "EMTC" / "2026" / "nested.png").exists())

    def test_gdrive_sync_all_files_supports_workers(self):
        items = [
            {"id": "file-1", "path": "proof-one.pdf", "name": "proof-one.pdf", "is_folder": False, "modified_time": "2026-04-20T00:00:00Z"},
            {"id": "file-2", "path": "proof-two.pdf", "name": "proof-two.pdf", "is_folder": False, "modified_time": "2026-04-21T00:00:00Z"},
        ]
        call_manifest_locks = []

        def fake_download_and_record_file(**kwargs):
            call_manifest_locks.append(kwargs.get("manifest_lock") is not None)
            return {"local_size": 11}

        with mock.patch.object(probe_gdrive, "download_and_record_file", side_effect=fake_download_and_record_file):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = probe_gdrive.sync_all_files(
                    session=object(),
                    cfg={"warehouse": "EMTC"},
                    download_cfg={"base_dir": "/tmp/pod"},
                    headers={},
                    timeout=15,
                    items=items,
                    explicit_output_dir=None,
                    workers=2,
                    incremental=False,
                )

        self.assertEqual(summary["downloaded_files"], 2)
        self.assertEqual(summary["failed_files"], 0)
        self.assertEqual(summary["total_files_seen"], 2)
        self.assertEqual(call_manifest_locks, [True, True])

    def test_onedrive_downloads_first_file_using_content_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "access_token": "token-456",
                            "drive_id": "",
                            "root_path": "/CMC/POD",
                            "timeout_seconds": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "item-1",
                                    "name": "pod-image.jpg",
                                    "file": {"mimeType": "image/jpeg"},
                                    "size": 18,
                                    "createdDateTime": "2026-04-20T00:00:00Z",
                                    "lastModifiedDateTime": "2026-04-21T00:00:00Z",
                                    "webUrl": "https://onedrive.test/item-1",
                                    "parentReference": {
                                        "driveId": "resolved-drive-id",
                                        "path": "/drive/root:/CMC/POD",
                                    },
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"jpg-binary-content"),
                ]
            )

            stdout = io.StringIO()
            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "5",
                "--download-first-file",
                "--output-dir",
                str(tmp_path / "downloads"),
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(stdout):
                        probe_onedrive.main()

            downloaded_file = tmp_path / "downloads" / "pod-image.jpg"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"jpg-binary-content")
            self.assertEqual(
                fake_session.calls[1]["url"],
                "https://graph.microsoft.com/v1.0/drives/resolved-drive-id/items/item-1/content",
            )
            self.assertTrue(fake_session.calls[1]["stream"])

    def test_onedrive_uses_configured_download_base_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {
                            "base_dir": str(tmp_path / "pod"),
                        },
                        "onedrive": {
                            "access_token": "token-456",
                            "warehouse": "CMC",
                            "drive_id": "",
                            "root_path": "/CMC/POD",
                            "timeout_seconds": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "item-1",
                                    "name": "pod-image.jpg",
                                    "file": {"mimeType": "image/jpeg"},
                                    "size": 18,
                                    "parentReference": {
                                        "driveId": "resolved-drive-id",
                                        "path": "/drive/root:/CMC/POD",
                                    },
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"jpg-binary-content"),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "1",
                "--download-first-file",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            downloaded_file = tmp_path / "pod" / "CMC" / "pod-image.jpg"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"jpg-binary-content")

    def test_onedrive_lists_children_from_root_item_id(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    json_data={
                        "id": "folder-1",
                        "name": "Tenways-CMC POD",
                        "folder": {"childCount": 1},
                        "parentReference": {"driveId": "shared-drive-id"},
                    }
                ),
                FakeResponse(
                    json_data={
                        "value": [
                            {
                                "id": "child-file-1",
                                "name": "proof-001.pdf",
                                "file": {"mimeType": "application/pdf"},
                                "size": 120,
                                "parentReference": {
                                    "driveId": "shared-drive-id",
                                    "path": "/drive/root:/Tenways-CMC POD",
                                },
                            }
                        ]
                    }
                ),
            ]
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            first_file = probe_onedrive.walk_from_item_id(
                session=fake_session,
                drive_id="shared-drive-id",
                item_id="folder-1",
                headers={"Authorization": "Bearer test"},
                timeout=10,
                recursive=False,
                max_items=10,
            )

        self.assertEqual(first_file["id"], "child-file-1")
        self.assertIn("Tenways-CMC POD/proof-001.pdf", stdout.getvalue())

    def test_onedrive_downloads_specific_item_id_without_listing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "onedrive": {
                            "access_token": "token-456",
                            "drive_id": "direct-drive-id",
                            "timeout_seconds": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "id": "item-2",
                            "name": "pod-direct.pdf",
                            "file": {"mimeType": "application/pdf"},
                            "size": 23,
                            "createdDateTime": "2026-04-20T00:00:00Z",
                            "lastModifiedDateTime": "2026-04-21T00:00:00Z",
                            "parentReference": {
                                "driveId": "direct-drive-id",
                                "path": "/drive/root:/CMC/POD",
                            },
                        }
                    ),
                    FakeResponse(content=b"direct-onedrive-content"),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--download-file-id",
                "item-2",
                "--output-dir",
                str(tmp_path / "downloads"),
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            downloaded_file = tmp_path / "downloads" / "pod-direct.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"direct-onedrive-content")
            self.assertEqual(fake_session.calls[0]["url"], "https://graph.microsoft.com/v1.0/drives/direct-drive-id/items/item-2")

    def test_onedrive_downloads_all_files_recursively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {
                            "base_dir": str(tmp_path / "pod"),
                        },
                        "onedrive": {
                            "access_token": "token-456",
                            "warehouse": "CMC",
                            "drive_id": "",
                            "root_path": "/CMC/POD",
                            "timeout_seconds": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )

            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "folder-2026",
                                    "name": "2026",
                                    "folder": {"childCount": 1},
                                    "parentReference": {
                                        "driveId": "resolved-drive-id",
                                        "path": "/drive/root:/CMC/POD",
                                    },
                                },
                                {
                                    "id": "item-top",
                                    "name": "top.jpg",
                                    "file": {"mimeType": "image/jpeg"},
                                    "size": 3,
                                    "parentReference": {
                                        "driveId": "resolved-drive-id",
                                        "path": "/drive/root:/CMC/POD",
                                    },
                                },
                            ]
                        }
                    ),
                    FakeResponse(content=b"top"),
                    FakeResponse(
                        json_data={
                            "value": [
                                {
                                    "id": "item-nested",
                                    "name": "nested.pdf",
                                    "file": {"mimeType": "application/pdf"},
                                    "size": 6,
                                    "parentReference": {
                                        "driveId": "resolved-drive-id",
                                        "path": "/drive/root:/CMC/POD/2026",
                                    },
                                }
                            ]
                        }
                    ),
                    FakeResponse(content=b"nested"),
                ]
            )

            argv = [
                "probe_onedrive.py",
                "--config",
                str(config_path),
                "--limit",
                "0",
                "--recursive",
                "--download-all-files",
            ]

            with mock.patch.object(probe_onedrive.requests, "Session", return_value=fake_session):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe_onedrive.main()

            self.assertTrue((tmp_path / "pod" / "CMC" / "top.jpg").exists())
            self.assertTrue((tmp_path / "pod" / "CMC" / "2026" / "nested.pdf").exists())


if __name__ == "__main__":
    unittest.main()
