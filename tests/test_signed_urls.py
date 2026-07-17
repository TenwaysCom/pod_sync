import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.signed_urls import (
    build_signed_storage_url,
    file_url_secret_error,
    is_valid_file_url_secret,
    redact_sensitive_data,
    redact_sensitive_text,
    select_file_url_secret,
    verify_storage_path_token,
)


class SignedUrlTests(unittest.TestCase):
    def test_build_signed_storage_url_requires_matching_path_and_secret(self):
        url = build_signed_storage_url("https://pod.example.test/", "CMC/2024 POD/EU22114.pdf", "secret")

        self.assertIn("/storage/files/CMC/2024%20POD/EU22114.pdf?token=", url)
        token = url.rsplit("token=", 1)[1]
        self.assertTrue(verify_storage_path_token("CMC/2024 POD/EU22114.pdf", token, "secret"))
        self.assertFalse(verify_storage_path_token("CMC/other.pdf", token, "secret"))
        self.assertFalse(verify_storage_path_token("CMC/2024 POD/EU22114.pdf", token, "other-secret"))

    def test_file_url_secret_validation_rejects_placeholder_and_short_values(self):
        self.assertEqual(file_url_secret_error("CHANGE_ME_RANDOM_32_BYTES"), "placeholder")
        self.assertEqual(file_url_secret_error("short"), "too_short")
        self.assertFalse(is_valid_file_url_secret("PASTE_SECRET_HERE"))
        self.assertTrue(is_valid_file_url_secret("x" * 32))

    def test_invalid_environment_secret_does_not_mask_valid_config(self):
        self.assertEqual(
            select_file_url_secret("short", "c" * 32),
            "c" * 32,
        )
        self.assertEqual(
            select_file_url_secret("e" * 32, "c" * 32),
            "e" * 32,
        )

    def test_recursive_redaction_removes_real_signed_tokens(self):
        signed_url = build_signed_storage_url(
            "https://pod.example.test",
            "Westerman/WDB2B/OUT/01391/proof.pdf",
            "x" * 32,
        )
        token = signed_url.rsplit("token=", 1)[1]

        redacted = redact_sensitive_data(
            {
                "file_url": signed_url,
                "nested": [{"token": token, "message": f"download={signed_url}"}],
                "api_key": "odoo-secret",
            }
        )
        serialized = str(redacted)

        self.assertNotIn(token, serialized)
        self.assertNotIn("odoo-secret", serialized)
        self.assertIn("token=[REDACTED]", redacted["file_url"])
        self.assertEqual(redacted["nested"][0]["token"], "[REDACTED]")

    def test_text_redaction_handles_url_and_serialized_token_fields(self):
        signed_url = build_signed_storage_url(
            "https://pod.example.test",
            "CMC/proof.pdf",
            "y" * 32,
        )
        token = signed_url.rsplit("token=", 1)[1]

        redacted = redact_sensitive_text(
            f'{{"file_url": "{signed_url}", "token": "{token}"}}'
        )

        self.assertNotIn(token, redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)


if __name__ == "__main__":
    unittest.main()
