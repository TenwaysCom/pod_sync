import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import burger_ingest


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PDF_BASE64 = base64.b64encode(PDF_BYTES).decode("ascii")


def _push_result(status="created_tracking", target_id=9001):
    return {
        "ok": True,
        "completed": 1,
        "business_pending": 0,
        "technical_failure_count": 0,
        "details": [
            {
                "status": status,
                "target_model": "stock.tracking",
                "target_id": target_id,
                "target_name": "WBB2B/OUT/42899",
                "error": None,
                "retryable": False,
            }
        ],
    }


class BurgerIngestTests(unittest.TestCase):
    def _write_config(self, root, *, token="burger-token", odoo_enabled=True, push_immediately=True, odoo_url=""):
        config = {
            "download": {"base_dir": str(root / "pod")},
            "pod_sync": {"base_url": "https://pod.test"},
            "odoo": {
                "enabled": odoo_enabled,
                "url": "https://odoo.test",
                "db": "db",
                "uid": 1,
                "api_key": "key",
            },
            "burger_ingest": {
                "enabled": True,
                "warehouse": "Burger",
                "odoo_url": odoo_url,
                "api_token": token,
                "push_immediately": push_immediately,
            },
        }
        config_path = root / "config.local.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def _ingest(self, config_path, root, **overrides):
        payload = {
            "picking_name": "WBB2B/OUT/42899",
            "file_name": "WBB2B_OUT_42899.pdf",
            "content_base64": PDF_BASE64,
            "pod_time": "2026-10-30 04:00:20",
        }
        payload.update(overrides)
        return burger_ingest.ingest_burger_pod(
            payload=payload,
            provided_token="burger-token",
            config_path=str(config_path),
            runtime_dir=root / "runtime",
        )

    def test_ingest_stores_manifest_and_builds_phase_payload_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)

            with mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ) as push_mock:
                result = self._ingest(config_path, root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["picking_name"], "WBB2B/OUT/42899")
            self.assertEqual(result["canonical_picking_name"], "WBB2BOUT42899")
            self.assertEqual(result["pod_time"], "2026-10-30T04:00:20Z")
            self.assertEqual(result["push"]["status"], "created_tracking")
            self.assertEqual(result["business_status"], "complete")

            stored = Path(result["local_path"])
            self.assertTrue(stored.is_file())
            self.assertEqual(stored.read_bytes(), PDF_BYTES)
            self.assertTrue(stored.name.startswith("WBB2BOUT42899_"))
            self.assertEqual(stored.parent.name, "WBB2BOUT42899")
            self.assertEqual(stored.parent.parent.name, "Burger")

            manifest_path = Path(result["manifest_path"])
            self.assertEqual(
                manifest_path,
                root / "pod" / "_integrity" / "Burger" / "burger_api.jsonl",
            )
            entries = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["shipment_reference"], "WBB2B/OUT/42899")
            self.assertEqual(entries[0]["source_type"], "burger_api")
            self.assertEqual(entries[0]["local_size"], len(PDF_BYTES))

            payload_path = Path(push_mock.call_args.kwargs["payload_path"])
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            item = payload["items"][0]
            self.assertEqual(item["identifier_status"], "ready")
            self.assertEqual(item["preferred_key_type"], "picking_name")
            self.assertEqual(item["preferred_key"], "WBB2BOUT42899")
            self.assertEqual(item["source_name"], "Burger")
            self.assertEqual(item["source_type"], "burger_api")
            self.assertEqual(item["local_path"], str(stored))
            self.assertEqual(item["source_modified_at"], "2026-10-30T04:00:20Z")
            self.assertFalse(push_mock.call_args.kwargs["incremental_pool"])

    def test_ingest_accepts_canonical_picking_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ):
                result = self._ingest(config_path, root, picking_name="wbb2c-out-22487")
            self.assertEqual(result["picking_name"], "WBB2C/OUT/22487")
            self.assertEqual(result["canonical_picking_name"], "WBB2COUT22487")

    def test_ingest_rejects_wrong_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
                burger_ingest.ingest_burger_pod(
                    payload={"picking_name": "WBB2B/OUT/1", "content_base64": PDF_BASE64},
                    provided_token="wrong",
                    config_path=str(config_path),
                    runtime_dir=root / "runtime",
                )
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(raised.exception.error_code, "INVALID_TOKEN")

    def test_ingest_fails_closed_without_configured_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root, token="")
            with mock.patch.dict(os.environ, {"POD_SYNC_BURGER_TOKEN": ""}):
                with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
                    self._ingest(config_path, root)
            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(raised.exception.error_code, "TOKEN_NOT_CONFIGURED")

    def test_ingest_reads_token_from_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root, token="")
            with mock.patch.dict(
                os.environ, {"POD_SYNC_BURGER_TOKEN": "burger-token"}
            ), mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ):
                result = self._ingest(config_path, root)
            self.assertTrue(result["ok"])

    def test_ingest_rejects_non_burger_picking_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
                self._ingest(config_path, root, picking_name="WCB2B/OUT/33271")
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.error_code, "INVALID_PICKING_NAME")

    def test_ingest_rejects_invalid_base64_and_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
                self._ingest(config_path, root, content_base64="not base64!!")
            self.assertEqual(raised.exception.error_code, "INVALID_BASE64")

            with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
                self._ingest(config_path, root, file_name="pod.html")
            self.assertEqual(raised.exception.error_code, "UNSUPPORTED_FILE_TYPE")

    def test_ingest_is_idempotent_for_identical_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ) as push_mock:
                first = self._ingest(config_path, root)
                second = self._ingest(config_path, root)

            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["local_path"], second["local_path"])
            self.assertEqual(push_mock.call_count, 2)

            manifest_path = Path(first["manifest_path"])
            entries = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 1)

    def test_ingest_keeps_history_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            changed_bytes = PDF_BYTES + b"revised\n"
            with mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ):
                first = self._ingest(config_path, root)
                second = self._ingest(
                    config_path,
                    root,
                    content_base64=base64.b64encode(changed_bytes).decode("ascii"),
                )

            self.assertNotEqual(first["local_path"], second["local_path"])
            self.assertEqual(
                Path(first["local_path"]).read_bytes(), PDF_BYTES
            )
            self.assertEqual(
                Path(second["local_path"]).read_bytes(), changed_bytes
            )
            entries = [
                json.loads(line)
                for line in Path(second["manifest_path"])
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["source_file_id"], entries[1]["source_file_id"])

    def test_ingest_stores_without_push_when_odoo_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root, odoo_enabled=False)
            with mock.patch.object(burger_ingest, "push_payload") as push_mock:
                result = self._ingest(config_path, root)
            push_mock.assert_not_called()
            self.assertTrue(result["stored"])
            self.assertEqual(result["push"]["status"], "skipped")
            self.assertEqual(result["push"]["reason"], "odoo_push_disabled")
            self.assertEqual(result["business_status"], "stored")

    def test_ingest_stores_without_push_when_immediate_push_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root, push_immediately=False)
            with mock.patch.object(burger_ingest, "push_payload") as push_mock:
                result = self._ingest(config_path, root)
            push_mock.assert_not_called()
            self.assertEqual(result["push"]["reason"], "push_immediately_disabled")
            self.assertEqual(result["business_status"], "stored")

    def test_settings_normalizes_odoo_url_override(self):
        config = {
            "burger_ingest": {
                "warehouse": "Burger",
                "api_token": "token",
                "odoo_url": "http://feature.test:18070/",
            }
        }

        self.assertEqual(
            burger_ingest._settings(config)["odoo_url"],
            "http://feature.test:18070",
        )

        config["burger_ingest"]["odoo_url"] = ""
        with mock.patch.dict(
            os.environ, {"POD_SYNC_BURGER_ODOO_URL": "https://env.test/"}, clear=False
        ):
            self.assertEqual(
                burger_ingest._settings(config)["odoo_url"], "https://env.test"
            )

        config["burger_ingest"]["odoo_url"] = "feature.test:18070"
        with self.assertRaises(burger_ingest.BurgerIngestError) as raised:
            burger_ingest._settings(config)
        self.assertEqual(raised.exception.error_code, "INVALID_CONFIG")

    def test_ingest_pushes_with_isolated_odoo_url_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(
                root, odoo_url="http://feature.test:18070"
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["burger_ingest"].update(
                {
                    "odoo_db": "feature-db",
                    "odoo_uid": 42,
                    "odoo_api_key": "feature-key",
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            seen = {}

            def fake_push(*, config_path, payload_path, incremental_pool=False):
                push_cfg = json.loads(
                    Path(config_path).read_text(encoding="utf-8")
                )
                seen["odoo"] = dict(push_cfg["odoo"])
                seen["override_path"] = Path(config_path)
                return _push_result()

            with mock.patch.object(
                burger_ingest, "push_payload", side_effect=fake_push
            ):
                result = self._ingest(config_path, root)

            base_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(seen["odoo"]["url"], "http://feature.test:18070")
            self.assertEqual(seen["odoo"]["db"], "feature-db")
            self.assertEqual(seen["odoo"]["uid"], 42)
            self.assertEqual(seen["odoo"]["api_key"], "feature-key")
            self.assertFalse(seen["override_path"].exists())
            self.assertEqual(base_cfg["odoo"]["url"], "https://odoo.test")
            self.assertEqual(base_cfg["odoo"]["db"], "db")
            self.assertEqual(
                result["push"]["odoo_url"], "http://feature.test:18070"
            )

    def test_ingest_without_override_reuses_base_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            seen = {}

            def fake_push(*, config_path, payload_path, incremental_pool=False):
                seen["config_path"] = Path(config_path)
                at_url = json.loads(Path(config_path).read_text(encoding="utf-8"))[
                    "odoo"
                ]["url"]
                seen["url"] = at_url
                return _push_result()

            with mock.patch.object(
                burger_ingest, "push_payload", side_effect=fake_push
            ):
                result = self._ingest(config_path, root)

            self.assertEqual(seen["config_path"], config_path)
            self.assertEqual(seen["url"], "https://odoo.test")
            self.assertTrue(seen["config_path"].exists())
            self.assertIsNone(result["push"]["odoo_url"])

    def test_audit_log_records_each_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            with mock.patch.object(
                burger_ingest, "push_payload", return_value=_push_result()
            ):
                result = self._ingest(config_path, root)
            audit_path = root / "runtime" / "ingest.jsonl"
            entries = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["request_id"], result["request_id"])
            self.assertEqual(entries[0]["push_status"], "created_tracking")


class BurgerIngestRouteTests(unittest.TestCase):
    def test_route_bypasses_console_auth_and_forwards_token(self):
        os.environ.setdefault("POD_SYNC_DISABLE_SCHEDULER", "1")
        from fastapi.testclient import TestClient

        from webapp import app as app_module

        with mock.patch.object(
            app_module.burger_ingest,
            "ingest_burger_pod",
            return_value={"ok": True, "stored": True},
        ) as handler, mock.patch.object(
            app_module, "ENABLE_PASSWORD_LOGIN", True
        ), mock.patch.object(
            app_module, "AUTH_CONFIGURATION_ERROR", ""
        ):
            client = TestClient(app_module.app)
            protected = client.get("/", follow_redirects=False)
            response = client.post(
                "/api/burger/pod",
                json={"picking_name": "WBB2B/OUT/42899", "content_base64": PDF_BASE64},
                headers={"X-Burger-Token": "burger-token"},
            )

        self.assertEqual(protected.status_code, 303)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            handler.call_args.kwargs["provided_token"], "burger-token"
        )

    def test_route_maps_ingest_errors_to_json(self):
        os.environ.setdefault("POD_SYNC_DISABLE_SCHEDULER", "1")
        from fastapi.testclient import TestClient

        from webapp import app as app_module

        with mock.patch.object(
            app_module.burger_ingest,
            "ingest_burger_pod",
            side_effect=burger_ingest.BurgerIngestError(
                "token invalid", status_code=401, error_code="INVALID_TOKEN"
            ),
        ):
            client = TestClient(app_module.app)
            response = client.post(
                "/api/burger/pod",
                json={"picking_name": "WBB2B/OUT/42899"},
                headers={"X-Burger-Token": "wrong"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error_code"], "INVALID_TOKEN")


if __name__ == "__main__":
    unittest.main()
