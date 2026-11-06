import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.burger_ingest import BurgerIngestError, ingest_burger_pod, sign_burger_request
from webapp.odoo_reconcile_cache import resolve_source_delivery_types


APP_ID = "burger-app"
APP_KEY = "burger-secret-key"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"burger-pod-image"
OTHER_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"another-burger-image"
FIXED_NOW = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)


def _config(root: Path, *, enabled: bool = True, app_key: str = APP_KEY) -> Path:
    path = root / "config.json"
    path.write_text(
        json.dumps(
            {
                "pod_sync": {"base_url": "https://pod.example.test"},
                "download": {"base_dir": str(root / "storage" / "pod")},
                "burger_api": {
                    "enabled": enabled,
                    "app_id": APP_ID,
                    "app_key": app_key,
                    "warehouse": "Burger",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _body(picking_name: str, content: bytes, *, data_url: bool = False) -> bytes:
    encoded = base64.b64encode(content).decode("ascii")
    if data_url:
        encoded = f"data:image/png;base64,{encoded}"
    return json.dumps(
        {"picking_name": picking_name, "image_base64": encoded}
    ).encode("utf-8")


def _ingest(config_path: Path, body: bytes, *, app_id: str = APP_ID, timestamp: str | None = None, signature: str | None = None, now: datetime = FIXED_NOW, max_bytes: int | None = None):
    stamp = timestamp if timestamp is not None else str(int(now.timestamp()))
    signed = signature if signature is not None else sign_burger_request(app_id, APP_KEY, stamp, body)
    return ingest_burger_pod(
        body=body,
        app_id=app_id,
        timestamp=stamp,
        signature=signed,
        config_path=str(config_path),
        now=now,
        max_bytes=max_bytes,
    )


class BurgerIngestTests(unittest.TestCase):
    def test_burger_is_not_part_of_the_odoo_index_refresh(self):
        self.assertNotIn("burger", resolve_source_delivery_types({}))

    def test_valid_signature_stores_manifest_and_returns_local_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _config(root)
            body = _body("WCB2B/OUT/12345", PNG_BYTES, data_url=True)

            result = _ingest(config_path, body)

            self.assertFalse(result["duplicate"])
            self.assertEqual(result["picking_name"], "WCB2B/OUT/12345")
            self.assertIn("/storage/files/Burger/WCB2B_OUT_12345/", result["file_url"])
            self.assertTrue(result["file_url"].startswith("https://pod.example.test/"))
            stored = list((root / "storage" / "pod" / "Burger").rglob("*.png"))
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].read_bytes(), PNG_BYTES)
            manifest = (root / "storage" / "pod" / "_integrity" / "Burger" / "burger_api.jsonl").read_text(encoding="utf-8")
            entry = json.loads(manifest)
            self.assertEqual(entry["warehouse"], "Burger")
            self.assertEqual(entry["source"], "burger_api")
            self.assertEqual(entry["shipment_reference"], "WCB2B/OUT/12345")
            self.assertEqual(entry["mime_type"], "image/png")
            self.assertEqual(entry["source_url"], result["file_url"])
            self.assertTrue(entry["source_file_id"].endswith(entry["sha256"]))

    def test_same_picking_and_bytes_are_not_stored_twice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _config(root)
            body = _body("WCB2B/OUT/12345", PNG_BYTES)

            first = _ingest(config_path, body)
            second = _ingest(config_path, body)

            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["file_url"], second["file_url"])
            manifest_path = root / "storage" / "pod" / "_integrity" / "Burger" / "burger_api.jsonl"
            self.assertEqual(len(manifest_path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len(list((root / "storage" / "pod" / "Burger").rglob("*.png"))), 1)

    def test_same_picking_keeps_distinct_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _config(root)

            first = _ingest(config_path, _body("WCB2B/OUT/12345", PNG_BYTES))
            second = _ingest(config_path, _body("WCB2B/OUT/12345", OTHER_PNG_BYTES))

            self.assertFalse(first["duplicate"])
            self.assertFalse(second["duplicate"])
            self.assertNotEqual(first["file_url"], second["file_url"])
            self.assertEqual(len(list((root / "storage" / "pod" / "Burger").rglob("*.png"))), 2)

    def test_same_bytes_for_different_pickings_stay_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _config(root)

            first = _ingest(config_path, _body("WCB2B/OUT/12345", PNG_BYTES))
            second = _ingest(config_path, _body("WCB2B/OUT/67890", PNG_BYTES))
            manifest_lines = (
                root / "storage" / "pod" / "_integrity" / "Burger" / "burger_api.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            identities = {json.loads(line)["source_file_id"] for line in manifest_lines}

            self.assertNotEqual(first["file_url"], second["file_url"])
            self.assertEqual(len(identities), 2)

    def test_rejects_bad_signature_expired_timestamp_and_wrong_app_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _config(Path(tmpdir))
            body = _body("WCB2B/OUT/12345", PNG_BYTES)
            stamp = str(int(FIXED_NOW.timestamp()))

            with self.assertRaises(BurgerIngestError) as bad_signature:
                _ingest(config_path, body, signature="0" * 64)
            self.assertEqual(bad_signature.exception.status_code, 401)

            expired = str(int((FIXED_NOW - timedelta(minutes=10)).timestamp()))
            with self.assertRaises(BurgerIngestError) as expired_signature:
                _ingest(
                    config_path,
                    body,
                    timestamp=expired,
                    signature=sign_burger_request(APP_ID, APP_KEY, expired, body),
                )
            self.assertEqual(expired_signature.exception.status_code, 401)

            with self.assertRaises(BurgerIngestError) as wrong_app:
                _ingest(
                    config_path,
                    body,
                    app_id="other-app",
                    signature=sign_burger_request("other-app", APP_KEY, stamp, body),
                )
            self.assertEqual(wrong_app.exception.status_code, 401)

    def test_rejects_non_image_and_oversize_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _config(Path(tmpdir))

            with self.assertRaises(BurgerIngestError) as non_image:
                _ingest(config_path, _body("WCB2B/OUT/12345", b"%PDF-1.4 not an image"))
            self.assertEqual(non_image.exception.status_code, 400)

            with self.assertRaises(BurgerIngestError) as oversize:
                _ingest(config_path, _body("WCB2B/OUT/12345", PNG_BYTES), max_bytes=8)
            self.assertEqual(oversize.exception.status_code, 413)

    def test_missing_credentials_are_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _config(Path(tmpdir), app_key="")
            with self.assertRaises(BurgerIngestError) as missing:
                _ingest(config_path, _body("WCB2B/OUT/12345", PNG_BYTES))
            self.assertEqual(missing.exception.status_code, 503)

    def test_route_checks_signature_without_session_login(self):
        from fastapi.testclient import TestClient

        from webapp import app as app_module

        def fake_ingest(**kwargs):
            if kwargs["signature"] == "0" * 64:
                raise BurgerIngestError(401, "invalid signature")
            return {
                "picking_name": "WCB2B/OUT/12345",
                "file_url": "https://pod.example.test/storage/files/Burger/proof.png",
                "duplicate": False,
            }

        previous_login = app_module.ENABLE_PASSWORD_LOGIN
        previous_scheduler = os.environ.get("POD_SYNC_DISABLE_SCHEDULER")
        os.environ["POD_SYNC_DISABLE_SCHEDULER"] = "1"
        app_module.ENABLE_PASSWORD_LOGIN = True
        try:
            with mock.patch("webapp.app.ingest_burger_pod", side_effect=fake_ingest):
                client = TestClient(app_module.app)
                rejected = client.post(
                    "/api/burger/pod",
                    content=b"{}",
                    headers={
                        "X-App-Id": APP_ID,
                        "X-Timestamp": "1",
                        "X-Signature": "0" * 64,
                    },
                )
                accepted = client.post(
                    "/api/burger/pod",
                    content=b"{}",
                    headers={
                        "X-App-Id": APP_ID,
                        "X-Timestamp": "1",
                        "X-Signature": "a" * 64,
                    },
                )
        finally:
            app_module.ENABLE_PASSWORD_LOGIN = previous_login
            if previous_scheduler is None:
                os.environ.pop("POD_SYNC_DISABLE_SCHEDULER", None)
            else:
                os.environ["POD_SYNC_DISABLE_SCHEDULER"] = previous_scheduler

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertFalse(accepted.json()["duplicate"])


if __name__ == "__main__":
    unittest.main()
