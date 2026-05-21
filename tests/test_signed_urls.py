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


if __name__ == "__main__":
    unittest.main()
