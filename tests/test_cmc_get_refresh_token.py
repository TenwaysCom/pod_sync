import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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


auth = load_module("cmc_get_refresh_token", "clients/cmc_onedrive/get_refresh_token.py")


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, content=None):
        self._json_data = json_data or {}
        self.status_code = status_code
        if content is None:
            content = json.dumps(self._json_data).encode("utf-8")
        self.content = content

    def json(self):
        return self._json_data


DEVICE_CODE_PAYLOAD = {
    "device_code": "device-code-value",
    "user_code": "CODE-1234",
    "verification_uri": "https://microsoft.com/devicelogin",
    "interval": 5,
    "expires_in": 900,
    "message": "To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code CODE-1234.",
}


class DeviceCodeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = Path(self.tmpdir.name) / "config.local.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "cmc_onedrive": {
                        "drive_id": "3bb1cc2a6ad39e4c",
                        "root_item_id": "3BB1CC2A6AD39E4C!134265",
                        "client_id": "client-id",
                        "tenant_id": "common",
                        "scope": "offline_access openid profile Files.Read User.Read",
                        "warehouse": "CMC",
                    },
                    "download": {"base_dir": "storage/pod"},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)

    def _run_main(self, posts, gets):
        argv = [
            "get_refresh_token.py",
            "--config",
            str(self.config_path),
            "--write-config",
        ]
        buffer = io.StringIO()
        with mock.patch.object(requests, "post", side_effect=posts) as post_mock, mock.patch.object(
            requests, "get", side_effect=gets
        ), mock.patch.object(auth.time, "sleep", return_value=None), mock.patch.object(
            sys, "argv", argv
        ), redirect_stdout(buffer):
            auth.main()
        return buffer.getvalue(), post_mock

    def test_success_writes_tokens_without_echoing_them(self):
        posts = [
            FakeResponse(DEVICE_CODE_PAYLOAD),
            FakeResponse({"error": "authorization_pending"}, status_code=400),
            FakeResponse(
                {
                    "access_token": "ACCESS-SECRET-VALUE",
                    "refresh_token": "REFRESH-SECRET-VALUE",
                    "expires_in": 3600,
                    "scope": "Files.Read",
                }
            ),
        ]
        gets = [FakeResponse({"userPrincipalName": "someone@example.com", "displayName": "Someone"})]

        output, _ = self._run_main(posts, gets)

        self.assertNotIn("ACCESS-SECRET-VALUE", output)
        self.assertNotIn("REFRESH-SECRET-VALUE", output)
        self.assertIn("someone@example.com", output)
        self.assertIn("CODE-1234", output)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        cmc = saved["cmc_onedrive"]
        self.assertEqual(cmc["access_token"], "ACCESS-SECRET-VALUE")
        self.assertEqual(cmc["refresh_token"], "REFRESH-SECRET-VALUE")
        self.assertEqual(cmc["drive_id"], "3bb1cc2a6ad39e4c")
        self.assertEqual(cmc["root_item_id"], "3BB1CC2A6AD39E4C!134265")
        self.assertEqual(cmc["warehouse"], "CMC")
        self.assertEqual(saved["download"]["base_dir"], "storage/pod")
        self.assertEqual(os.stat(self.config_path).st_mode & 0o777, 0o600)

    def test_slow_down_is_retried(self):
        posts = [
            FakeResponse(DEVICE_CODE_PAYLOAD),
            FakeResponse({"error": "slow_down"}, status_code=400),
            FakeResponse({"access_token": "A", "refresh_token": "R", "expires_in": 3600}),
        ]
        gets = [FakeResponse({"userPrincipalName": "someone@example.com"})]

        output, _ = self._run_main(posts, gets)

        self.assertIn("someone@example.com", output)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cmc_onedrive"]["refresh_token"], "R")

    def test_device_code_request_failure_surfaces_provider_error(self):
        response = FakeResponse(
            {
                "error": "unauthorized_client",
                "error_description": "AADSTS7000218: The request body must contain the following parameter: 'client_assertion'.",
            },
            status_code=400,
        )
        with mock.patch.object(requests, "post", return_value=response):
            with self.assertRaises(SystemExit) as case:
                auth.request_device_code("https://login.example/oauth2/v2.0", "client-id", "scope", 30)
        message = str(case.exception)
        self.assertIn("unauthorized_client", message)
        self.assertIn("AADSTS7000218", message)

    def test_missing_refresh_token_is_rejected(self):
        posts = [
            FakeResponse(DEVICE_CODE_PAYLOAD),
            FakeResponse({"access_token": "A", "expires_in": 3600}),
        ]
        gets = []
        argv = ["get_refresh_token.py", "--config", str(self.config_path)]
        with mock.patch.object(requests, "post", side_effect=posts), mock.patch.object(
            requests, "get", side_effect=gets
        ), mock.patch.object(auth.time, "sleep", return_value=None), mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as case:
                auth.main()
        self.assertIn("offline_access", str(case.exception))

    def test_declined_login_reports_clearly(self):
        posts = [
            FakeResponse(DEVICE_CODE_PAYLOAD),
            FakeResponse({"error": "authorization_declined"}, status_code=400),
        ]
        argv = ["get_refresh_token.py", "--config", str(self.config_path)]
        with mock.patch.object(requests, "post", side_effect=posts), mock.patch.object(
            auth.time, "sleep", return_value=None
        ), mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as case:
                auth.main()
        self.assertIn("declined", str(case.exception))


if __name__ == "__main__":
    unittest.main()
