import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from webapp import app as app_module
from webapp.signed_urls import build_signed_storage_url


class FakeRequest:
    def __init__(self, *, path="/", query_params=None, cookies=None, headers=None, form=None):
        self.url = SimpleNamespace(path=path, netloc="testserver", scheme="http")
        self.query_params = query_params or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self._form = form or {}

    async def form(self):
        return self._form


class WebAppSecurityTests(unittest.TestCase):
    def test_signed_storage_token_uses_decoded_relative_path(self):
        url = build_signed_storage_url("http://testserver", "CMC/2024 POD/EU22114.pdf", "secret")
        token = url.rsplit("token=", 1)[1]
        request = FakeRequest(
            path="/storage/files/CMC/2024%20POD/EU22114.pdf",
            query_params={"token": token},
        )

        with mock.patch.dict(os.environ, {"POD_SYNC_FILE_URL_SECRET": "secret"}):
            self.assertTrue(app_module._has_valid_storage_signature(request))

    def test_signed_storage_token_rejects_missing_token(self):
        request = FakeRequest(path="/storage/files/CMC/EU22114.pdf")

        with mock.patch.dict(os.environ, {"POD_SYNC_FILE_URL_SECRET": "secret"}):
            self.assertFalse(app_module._has_valid_storage_signature(request))

    def test_storage_signature_is_optional_without_secret(self):
        with mock.patch.dict(os.environ, {"POD_SYNC_FILE_URL_SECRET": ""}):
            self.assertFalse(app_module._storage_requires_signature())

    def test_storage_signature_is_required_with_valid_secret(self):
        with mock.patch.dict(os.environ, {"POD_SYNC_FILE_URL_SECRET": "x" * 32}):
            self.assertTrue(app_module._storage_requires_signature())

    def test_normalize_redirect_target_rejects_external_urls(self):
        self.assertEqual(app_module.normalize_redirect_target("https://evil.test", "/"), "/")
        self.assertEqual(app_module.normalize_redirect_target("//evil.test/path", "/"), "/")
        self.assertEqual(app_module.normalize_redirect_target("/jobs", "/"), "/jobs")

    def test_csrf_accepts_matching_cookie_and_form_token(self):
        token = "csrf-token"
        request = FakeRequest(
            cookies={app_module.CSRF_COOKIE: app_module._encode_csrf_token(token)},
            form={"csrf_token": token},
        )

        self.assertTrue(asyncio.run(app_module._is_valid_csrf(request)))

    def test_csrf_rejects_missing_origin_and_token(self):
        request = FakeRequest()

        self.assertFalse(asyncio.run(app_module._is_valid_csrf(request)))


if __name__ == "__main__":
    unittest.main()
