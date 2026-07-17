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
        self.url = SimpleNamespace(path=path, netloc="testserver", scheme="http", query="")
        self.query_params = query_params or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self._form = form or {}

    async def form(self):
        return self._form


class WebAppSecurityTests(unittest.TestCase):
    def setUp(self):
        app_module._LOGIN_FAILURES.clear()

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

    def test_unsigned_storage_request_is_public_when_no_secret_is_configured(self):
        request = FakeRequest(path="/storage/files/Westerman/proof.pdf")
        downstream = SimpleNamespace(status_code=200)

        async def call_next(_request):
            return downstream

        with mock.patch.object(app_module, "_storage_requires_signature", return_value=False):
            response = asyncio.run(app_module.auth_middleware(request, call_next))

        self.assertIs(response, downstream)

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

    def test_unauthenticated_page_redirects_to_login(self):
        with (
            mock.patch.object(app_module, "ENABLE_PASSWORD_LOGIN", True),
            mock.patch.object(app_module, "ADMIN_PASSWORD", "secret"),
            mock.patch.object(app_module, "AUTH_CONFIGURATION_ERROR", ""),
        ):
            async def call_next(_request):
                self.fail("unauthenticated request should not reach downstream handler")

            response = asyncio.run(app_module.auth_middleware(FakeRequest(path="/"), call_next))

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login?"))
        self.assertIn("next=%2F", response.headers["location"])

    def test_login_page_renders_password_form(self):
        with (
            mock.patch.object(app_module, "ENABLE_PASSWORD_LOGIN", True),
            mock.patch.object(app_module, "ADMIN_PASSWORD", "secret"),
            mock.patch.object(app_module, "AUTH_CONFIGURATION_ERROR", ""),
            mock.patch.object(app_module.templates, "TemplateResponse") as template_response,
        ):
            template_response.return_value = SimpleNamespace(status_code=200)
            response = asyncio.run(app_module.login_page(FakeRequest(path="/login"), next="/jobs"))

        self.assertEqual(response.status_code, 200)
        template_response.assert_called_once()
        kwargs = template_response.call_args.kwargs
        self.assertEqual(kwargs["name"], "login.html")
        self.assertEqual(kwargs["context"]["next"], "/jobs")

    def test_login_post_sets_session_cookie(self):
        with (
            mock.patch.object(app_module, "ENABLE_PASSWORD_LOGIN", True),
            mock.patch.object(app_module, "ADMIN_PASSWORD", "secret"),
            mock.patch.object(app_module, "AUTH_CONFIGURATION_ERROR", ""),
        ):
            response = asyncio.run(app_module.login_post(FakeRequest(path="/login"), password="secret", next="/jobs"))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/jobs")
        self.assertIn(app_module.SESSION_COOKIE, response.headers.get("set-cookie", ""))

    def test_login_post_rejects_wrong_password(self):
        with (
            mock.patch.object(app_module, "ENABLE_PASSWORD_LOGIN", True),
            mock.patch.object(app_module, "ADMIN_PASSWORD", "secret"),
            mock.patch.object(app_module, "AUTH_CONFIGURATION_ERROR", ""),
        ):
            response = asyncio.run(app_module.login_post(FakeRequest(path="/login"), password="wrong", next="/jobs"))

        self.assertEqual(response.status_code, 303)
        self.assertIn("/login?", response.headers["location"])
        self.assertIn("error=", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
