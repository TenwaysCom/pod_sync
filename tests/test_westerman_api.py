import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    module_path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


westerman_probe = load_module("westerman_probe_tests", "clients/westerman_api/probe.py")


class FakeResponse:
    def __init__(
        self,
        *,
        json_data=None,
        content=b"",
        status_code=200,
        headers=None,
        url="https://westerman.example.test/api",
    ):
        self._json_data = json_data
        self._content = content
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.url = url
        self.text = "" if json_data is None else json.dumps(json_data)

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
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected {method} request: {url}")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)


class WestermanApiTests(unittest.TestCase):
    def _write_config(self, tmp_path, source_config):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "download": {"base_dir": str(tmp_path / "pod")},
                    "westerman_api": source_config,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def _run_probe(self, config_path, fake_session, *args):
        argv = ["probe.py", "--config", str(config_path), *args]
        output = io.StringIO()
        with mock.patch.object(westerman_probe.requests, "Session", return_value=fake_session):
            with mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(output):
                    try:
                        westerman_probe.main()
                    except SystemExit as exc:
                        exc.stdout = output.getvalue()
                        raise
        return output.getvalue()

    def _indicium_config(self):
        return {
            "api_mode": "indicium",
            "base_url": "https://westerman.example.test/indicium/api",
            "api_key": "test-token",
            "api_key_prefix": "Bearer",
            "api_key_header": "Authorization",
            "warehouse": "Westerman",
            "pod_document_types": [" POD "],
            "timeout_seconds": 30,
            "download_timeout_seconds": 120,
        }

    def _single_pod_session(
        self,
        content,
        *,
        document_id="pod-doc",
        file_name="POD.pdf",
        external_id="U260201855",
        document_date_time=None,
    ):
        document = {
            "document_id": document_id,
            "document_type": "POD",
            "document_file_name": file_name,
        }
        if document_date_time:
            document["document_date_time"] = document_date_time
        shipment = {
            "shipment_reference": "WDB2B/OUT/01359",
            "shipment_document": [document],
        }
        if external_id:
            shipment["external_id"] = external_id
        return FakeSession(
            [
                FakeResponse(
                    json_data=[
                        {
                            "status": "Ok",
                            "body": {
                                "Shipment": [shipment]
                            },
                        }
                    ]
                ),
                FakeResponse(
                    json_data=[
                        {
                            "status": "Ok",
                            "body": {
                                "document_id": document_id,
                                "document_type": "POD",
                                "document_file_name": file_name,
                                "mime_type": "application/pdf",
                                "content_base64": base64.b64encode(content).decode("ascii"),
                            },
                        }
                    ]
                ),
            ]
        )

    def test_document_content_validation_rejects_unknown_type_and_accepts_jpeg_soi(self):
        with self.assertRaisesRegex(SystemExit, "unsupported MIME"):
            westerman_probe._validate_document_content(b"<html>error</html>", "", "document.bin")

        westerman_probe._validate_document_content(
            b"\xff\xd8\xdbjpeg payload",
            "image/jpeg",
            "proof.jpg",
        )

    def test_indicium_lookup_posts_shipment_reference_with_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "shipment_document": [{"document_id": "doc-1"}],
                                        }
                                    ]
                                },
                            }
                        ]
                    )
                ]
            )

            stdout = self._run_probe(
                config_path,
                fake_session,
                "--tracking-number",
                "WDB2B/OUT/01359",
            )

            self.assertEqual(len(fake_session.calls), 1)
            method, url, kwargs = fake_session.calls[0]
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://westerman.example.test/indicium/api/get_order_info")
            self.assertEqual(kwargs["json"], {"shipment_reference": "WDB2B/OUT/01359"})
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
            result = json.loads(stdout)
            self.assertEqual(result["shipment_reference"], "WDB2B/OUT/01359")
            self.assertEqual(result["download_candidate_count"], 1)
            self.assertFalse((tmp_path / "pod").exists())

    def test_indicium_rejects_non_https_base_url_before_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            source_config = self._indicium_config()
            source_config["base_url"] = "http://westerman.example.test/indicium/api"
            config_path = self._write_config(tmp_path, source_config)
            fake_session = FakeSession([])

            with self.assertRaisesRegex(SystemExit, "valid HTTPS URL"):
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                )
            self.assertEqual(fake_session.calls, [])

    def test_indicium_uses_only_configured_provider_proxy(self):
        source_config = self._indicium_config()
        source_config["proxy_url"] = "http://127.0.0.1:7890"
        session = westerman_probe.create_session(source_config)

        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies["http"], "http://127.0.0.1:7890")
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:7890")

    def test_indicium_rejects_invalid_provider_proxy(self):
        source_config = self._indicium_config()
        source_config["proxy_url"] = "socks5://127.0.0.1:7890"

        with self.assertRaisesRegex(SystemExit, "proxy_url"):
            westerman_probe.create_session(source_config)

    def test_indicium_downloads_only_pod_and_writes_integrity_manifest(self):
        pdf_content = b"%PDF-1.4\nwesterman pod\n%%EOF"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "external_id": "U260201855",
                                            "shipment_document": [
                                                {
                                                    "document_id": "packing-doc",
                                                    "document_type": "PackingSlip",
                                                    "document_file_name": "packing.pdf",
                                                },
                                                {
                                                    "document_id": "pod-doc",
                                                    "document_type": "pod",
                                                    "document_file_name": "POD_01359.pdf",
                                                    "document_date_time": "2026-07-16 12:29:16",
                                                },
                                            ]
                                        }
                                    ]
                                },
                            }
                        ]
                    ),
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "document_id": "pod-doc",
                                    "document_file_name": "../../POD_01359.pdf",
                                    "document_type": " POD ",
                                    "mime_type": "application/pdf",
                                    "content_base64": base64.b64encode(pdf_content).decode("ascii"),
                                },
                            }
                        ]
                    ),
                ]
            )

            stdout = self._run_probe(
                config_path,
                fake_session,
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
            )

            self.assertEqual([call[0] for call in fake_session.calls], ["POST", "GET"])
            self.assertEqual(fake_session.calls[1][2]["params"], {"document_id": "pod-doc"})
            content_hash = hashlib.sha256(pdf_content).hexdigest()[:12]
            destination = (
                tmp_path
                / "pod"
                / "Westerman"
                / "WDB2B"
                / "OUT"
                / "01359"
                / f"pod-doc_{content_hash}_POD_01359.pdf"
            )
            self.assertEqual(destination.read_bytes(), pdf_content)
            self.assertFalse((tmp_path / "POD_01359.pdf").exists())

            manifest_path = tmp_path / "pod" / "_integrity" / "Westerman" / "westerman_api.jsonl"
            entries = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["source_file_id"], "pod-doc")
            self.assertEqual(entry["source_path"], "WDB2B/OUT/01359/POD_01359.pdf")
            self.assertEqual(entry["shipment_reference"], "WDB2B/OUT/01359")
            self.assertEqual(entry["tracking_number"], "U260201855")
            self.assertEqual(entry["document_type"].strip().casefold(), "pod")
            self.assertEqual(entry["source_modified_time"], "2026-07-16T10:29:16Z")
            self.assertEqual(entry["source_created_time"], "2026-07-16T10:29:16Z")
            self.assertEqual(entry["mime_type"], "application/pdf")
            self.assertEqual(entry["local_size"], len(pdf_content))
            self.assertEqual(entry["sha256"], hashlib.sha256(pdf_content).hexdigest())
            self.assertEqual(westerman_probe.verify_manifest_entry(entry)["status"], "ok")
            self.assertNotIn("test-token", stdout)
            self.assertNotIn("content_base64", stdout)
            result = json.loads(stdout)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual(result["skipped_count"], 0)

    def test_atomic_cli_keeps_explicit_output_dir_outside_config_base(self):
        content = b"%PDF-1.7\natomic pod"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            explicit_output = tmp_path / "atomic-output"

            stdout = self._run_probe(
                config_path,
                self._single_pod_session(content),
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
                "--output-dir",
                str(explicit_output),
            )

            result = json.loads(stdout)
            downloaded_path = Path(result["downloads"][0]["downloaded_to"])
            self.assertTrue(downloaded_path.is_file())
            self.assertEqual(downloaded_path.read_bytes(), content)
            downloaded_path.relative_to(explicit_output)
            self.assertFalse((tmp_path / "pod" / "Westerman").exists())

    def test_manifest_timestamp_rules_prefer_document_time_then_delivery_date(self):
        cfg = self._indicium_config()
        self.assertEqual(
            westerman_probe._document_manifest_timestamp(
                cfg,
                {"delivery_date": "2026-07-15", "delivery_time": "08:00:00"},
                {"document_date_time": "2026-07-16 12:29:16"},
                {"document_date_time": "2026-07-16T12:00:00+01:00"},
            ),
            "2026-07-16T11:00:00Z",
        )
        self.assertEqual(
            westerman_probe._document_manifest_timestamp(
                cfg,
                {"delivery_date": "2026-07-16"},
                {},
                {},
            ),
            "2026-07-15T22:00:00Z",
        )

    def test_invalid_document_timezone_fails_without_guessing_utc(self):
        cfg = self._indicium_config()
        cfg["document_timezone"] = "Not/A-Timezone"
        with self.assertRaisesRegex(SystemExit, "valid IANA timezone"):
            westerman_probe._as_manifest_timestamp(
                "2026-07-16 12:29:16",
                cfg,
                field_name="document_date_time",
            )

    def test_bad_vendor_timestamp_does_not_block_valid_pod_download(self):
        content = b"%PDF-1.4\nvalid pod with bad vendor time\n%%EOF"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())

            stdout = self._run_probe(
                config_path,
                self._single_pod_session(
                    content,
                    document_date_time="not-a-provider-datetime",
                ),
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
            )

            result = json.loads(stdout)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual(result["failed_count"], 0)
            self.assertNotIn("not-a-provider-datetime", stdout)
            manifest_entry = json.loads(
                Path(result["manifest_path"])
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertIsNone(manifest_entry["source_modified_time"])
            self.assertIsNone(manifest_entry["source_created_time"])

    def test_indicium_uses_odoo_tracking_fallback_when_vendor_external_id_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            cfg, download_cfg = westerman_probe.load_config(config_path)
            result = westerman_probe._run_indicium(
                SimpleNamespace(download=True, output_dir=None),
                cfg,
                download_cfg,
                "WDB2B/OUT/01359",
                session=self._single_pod_session(
                    b"%PDF-1.4\nfallback tracking\n%%EOF",
                    external_id=None,
                ),
                emit=False,
                fallback_tracking_number="ODOO-TRACK-1",
            )

            manifest_entry = json.loads(
                Path(result["manifest_path"]).read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(manifest_entry["tracking_number"], "ODOO-TRACK-1")

    def test_indicium_no_matching_pod_is_a_successful_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "shipment_document": [
                                                {
                                                    "document_id": "label-doc",
                                                    "document_type": "Label",
                                                }
                                            ]
                                        }
                                    ]
                                },
                            }
                        ]
                    )
                ]
            )

            stdout = self._run_probe(
                config_path,
                fake_session,
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
            )

            self.assertEqual(len(fake_session.calls), 1)
            result = json.loads(stdout)
            self.assertEqual(result["downloaded_count"], 0)
            self.assertEqual(result["manifest_path"], None)
            self.assertFalse((tmp_path / "pod").exists())

    def test_indicium_rerun_is_idempotent_and_changed_content_is_versioned(self):
        first_content = b"%PDF-1.4\nfirst version\n%%EOF"
        second_content = b"%PDF-1.4\nsecond version\n%%EOF"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            args = (
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
            )

            self._run_probe(config_path, self._single_pod_session(first_content), *args)
            repeat_stdout = self._run_probe(
                config_path,
                self._single_pod_session(first_content),
                *args,
            )
            repeat_result = json.loads(repeat_stdout)
            self.assertEqual(repeat_result["downloaded_count"], 0)
            self.assertEqual(repeat_result["already_downloaded_count"], 1)

            manifest_path = tmp_path / "pod" / "_integrity" / "Westerman" / "westerman_api.jsonl"
            self.assertEqual(len(manifest_path.read_text(encoding="utf-8").splitlines()), 1)

            changed_stdout = self._run_probe(
                config_path,
                self._single_pod_session(second_content),
                *args,
            )
            changed_result = json.loads(changed_stdout)
            self.assertEqual(changed_result["downloaded_count"], 1)
            entries = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 2)
            self.assertNotEqual(entries[0]["local_path"], entries[1]["local_path"])
            self.assertEqual(
                [westerman_probe.verify_manifest_entry(entry)["status"] for entry in entries],
                ["ok", "ok"],
            )

    def test_same_content_backfills_new_manifest_timestamp_metadata(self):
        content = b"%PDF-1.4\nmanifest timestamp backfill\n%%EOF"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            args = (
                "--shipment-reference",
                "WDB2B/OUT/01359",
                "--download",
            )

            self._run_probe(config_path, self._single_pod_session(content), *args)
            backfill_stdout = self._run_probe(
                config_path,
                self._single_pod_session(
                    content,
                    document_date_time="2026-07-16 12:29:16",
                ),
                *args,
            )
            backfill_result = json.loads(backfill_stdout)
            self.assertEqual(backfill_result["downloaded_count"], 1)
            self.assertEqual(backfill_result["downloads"][0]["file_status"], "reused")

            manifest_path = (
                tmp_path
                / "pod"
                / "_integrity"
                / "Westerman"
                / "westerman_api.jsonl"
            )
            entries = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["local_path"], entries[1]["local_path"])
            self.assertIsNone(entries[0]["source_modified_time"])
            self.assertEqual(
                entries[1]["source_modified_time"],
                "2026-07-16T10:29:16Z",
            )

            repeat_stdout = self._run_probe(
                config_path,
                self._single_pod_session(
                    content,
                    document_date_time="2026-07-16 12:29:16",
                ),
                *args,
            )
            self.assertEqual(json.loads(repeat_stdout)["already_downloaded_count"], 1)
            self.assertEqual(
                len(manifest_path.read_text(encoding="utf-8").splitlines()),
                2,
            )

    def test_indicium_continues_after_one_document_fails(self):
        good_content = b"%PDF-1.4\ngood document\n%%EOF"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "shipment_document": [
                                                {"document_id": "bad-doc", "document_type": "POD"},
                                                {"document_id": "good-doc", "document_type": "POD"},
                                            ],
                                        }
                                    ]
                                },
                            }
                        ]
                    ),
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "document_id": "bad-doc",
                                    "document_type": "POD",
                                    "content_base64": "invalid-base64!",
                                },
                            }
                        ]
                    ),
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "document_id": "good-doc",
                                    "document_type": "POD",
                                    "document_file_name": "good.pdf",
                                    "mime_type": "application/pdf",
                                    "content_base64": base64.b64encode(good_content).decode("ascii"),
                                },
                            }
                        ]
                    ),
                ]
            )

            with self.assertRaisesRegex(SystemExit, "invalid base64") as raised:
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                    "--download",
                )

            result = json.loads(raised.exception.stdout)
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["downloaded_count"], 1)
            self.assertEqual([call[0] for call in fake_session.calls], ["POST", "GET", "GET"])
            manifest_path = tmp_path / "pod" / "_integrity" / "Westerman" / "westerman_api.jsonl"
            entry = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(entry["source_file_id"], "good-doc")
            self.assertEqual(westerman_probe.verify_manifest_entry(entry)["status"], "ok")

    def test_indicium_rejects_invalid_base64_without_writing_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "shipment_document": [
                                                {
                                                    "document_id": "bad-doc",
                                                    "document_type": "POD",
                                                }
                                            ]
                                        }
                                    ]
                                },
                            }
                        ]
                    ),
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "document_id": "bad-doc",
                                    "document_type": "POD",
                                    "content_base64": "not-valid-base64!",
                                },
                            }
                        ]
                    ),
                ]
            )

            with self.assertRaisesRegex(SystemExit, "invalid base64"):
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                    "--download",
                )

            self.assertFalse((tmp_path / "pod" / "_integrity").exists())

    def test_indicium_rejects_pdf_metadata_with_non_pdf_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())

            with self.assertRaisesRegex(SystemExit, "valid PDF header"):
                self._run_probe(
                    config_path,
                    self._single_pod_session(b"<html>not a PDF</html>"),
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                    "--download",
                )
            self.assertFalse((tmp_path / "pod" / "_integrity").exists())

    def test_indicium_rejects_error_envelope_without_writing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [FakeResponse(json_data=[{"status": "Error", "body": {}}])]
            )

            with self.assertRaisesRegex(SystemExit, "returned status Error"):
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                    "--download",
                )

            self.assertFalse((tmp_path / "pod").exists())

    def test_indicium_rejects_mismatched_shipment_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/09999",
                                            "shipment_document": [],
                                        }
                                    ]
                                },
                            }
                        ]
                    )
                ]
            )

            with self.assertRaisesRegex(SystemExit, "different shipment_reference"):
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                )

    def test_indicium_rejects_mismatched_document_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(tmp_path, self._indicium_config())
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "Shipment": [
                                        {
                                            "shipment_reference": "WDB2B/OUT/01359",
                                            "shipment_document": [
                                                {"document_id": "requested-doc", "document_type": "POD"}
                                            ]
                                        }
                                    ]
                                },
                            }
                        ]
                    ),
                    FakeResponse(
                        json_data=[
                            {
                                "status": "Ok",
                                "body": {
                                    "document_id": "other-doc",
                                    "document_type": "POD",
                                    "content_base64": base64.b64encode(b"wrong document").decode("ascii"),
                                },
                            }
                        ]
                    ),
                ]
            )

            with self.assertRaisesRegex(SystemExit, "different document_id"):
                self._run_probe(
                    config_path,
                    fake_session,
                    "--shipment-reference",
                    "WDB2B/OUT/01359",
                    "--download",
                )
            self.assertFalse((tmp_path / "pod" / "_integrity").exists())

    def test_missing_api_mode_preserves_generic_base64_download(self):
        content = b"legacy-pdf"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(
                tmp_path,
                {
                    "base_url": "https://legacy.example.test/api",
                    "api_key": "legacy-secret",
                    "api_key_header": "X-API-Key",
                    "lookup_path": "/pod",
                    "http_method": "GET",
                    "tracking_param_name": "tracking_number",
                    "base64_content_field": "data.content",
                    "file_name_field": "data.file_name",
                    "download_url_field": "",
                    "warehouse": "Westerman",
                },
            )
            fake_session = FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "data": {
                                "content": base64.b64encode(content).decode("ascii"),
                                "file_name": "legacy.pdf",
                            }
                        }
                    )
                ]
            )

            self._run_probe(
                config_path,
                fake_session,
                "--tracking-number",
                "TRACK-1",
                "--download",
            )

            self.assertEqual(len(fake_session.calls), 1)
            method, url, kwargs = fake_session.calls[0]
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://legacy.example.test/api/pod")
            self.assertEqual(kwargs["params"], {"tracking_number": "TRACK-1"})
            self.assertEqual(kwargs["headers"]["X-API-Key"], "legacy-secret")
            self.assertEqual((tmp_path / "pod" / "Westerman" / "legacy.pdf").read_bytes(), content)

    def test_generic_mode_omits_final_url_and_text_response_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(
                tmp_path,
                {
                    "base_url": "https://legacy.example.test/api",
                    "api_key": "legacy-secret",
                    "api_key_header": "X-API-Key",
                    "lookup_path": "/pod",
                    "http_method": "GET",
                    "tracking_param_name": "tracking_number",
                    "warehouse": "Westerman",
                },
            )
            response = FakeResponse(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                url=(
                    "https://download.example.test/proof.pdf"
                    "?token=super-secret-download-token"
                ),
            )
            response.text = "provider response containing super-secret-body"

            output = self._run_probe(
                config_path,
                FakeSession([response]),
                "--tracking-number",
                "TRACK-1",
            )

            result = json.loads(output)
            self.assertEqual(result["http_status"], 200)
            self.assertEqual(result["body_type"], "text")
            self.assertEqual(
                result["body_diagnostic"],
                "non_json_response_body_omitted",
            )
            self.assertNotIn("final_url", result)
            self.assertNotIn("body_preview", result)
            self.assertNotIn("super-secret-download-token", output)
            self.assertNotIn("super-secret-body", output)
            self.assertNotIn("download.example.test", output)

    def test_generic_mode_omits_json_response_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(
                tmp_path,
                {
                    "base_url": "https://legacy.example.test/api",
                    "api_key": "legacy-secret",
                    "api_key_header": "X-API-Key",
                    "lookup_path": "/pod",
                    "http_method": "GET",
                    "tracking_param_name": "tracking_number",
                    "warehouse": "Westerman",
                },
            )

            output = self._run_probe(
                config_path,
                FakeSession(
                    [
                        FakeResponse(
                            json_data={"super-secret-response-key": "super-secret-value"},
                            url="https://download.example.test/proof.pdf?token=secret",
                        )
                    ]
                ),
                "--tracking-number",
                "TRACK-1",
            )

            result = json.loads(output)
            self.assertEqual(result["body_type"], "json")
            self.assertEqual(
                result["body_diagnostic"],
                "json_response_body_omitted",
            )
            self.assertNotIn("body_summary", result)
            self.assertNotIn("super-secret-response-key", output)
            self.assertNotIn("super-secret-value", output)
            self.assertNotIn("download.example.test", output)

    def test_generic_http_error_omits_final_url_and_text_response_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = self._write_config(
                tmp_path,
                {
                    "base_url": "https://legacy.example.test/api",
                    "api_key": "legacy-secret",
                    "api_key_header": "X-API-Key",
                    "lookup_path": "/pod",
                    "http_method": "GET",
                    "tracking_param_name": "tracking_number",
                    "warehouse": "Westerman",
                },
            )
            response = FakeResponse(
                status_code=503,
                headers={"Content-Type": "text/plain"},
                url=(
                    "https://download.example.test/proof.pdf"
                    "?token=super-secret-download-token"
                ),
            )
            response.text = "provider response containing super-secret-body"

            with self.assertRaisesRegex(SystemExit, "HTTP 503") as raised:
                self._run_probe(
                    config_path,
                    FakeSession([response]),
                    "--tracking-number",
                    "TRACK-1",
                )

            combined = f"{raised.exception}\n{raised.exception.stdout}"
            self.assertNotIn("super-secret-download-token", combined)
            self.assertNotIn("super-secret-body", combined)
            self.assertNotIn("download.example.test", combined)
            self.assertNotIn("final_url", combined)
            self.assertNotIn("body_preview", combined)


if __name__ == "__main__":
    unittest.main()
