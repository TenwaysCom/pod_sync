import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import actions
from webapp import pipeline_runner


class WebAppActionTests(unittest.TestCase):
    def test_odoo_push_ui_uses_batch_preflight_and_incremental_write_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.local.json"
            payload_path = Path(tmpdir) / "payload.json"
            config_path.write_text("{}", encoding="utf-8")
            payload_path.write_text('{"items": []}', encoding="utf-8")

            _, preview_command, preview_metadata = actions.build_odoo_push_command(
                config_path=str(config_path),
                payload_path=str(payload_path),
                dry_run=True,
                missing_only=True,
            )
            _, write_command, write_metadata = actions.build_odoo_push_command(
                config_path=str(config_path),
                payload_path=str(payload_path),
                dry_run=False,
                missing_only=True,
            )

        self.assertIn("--incremental-preflight", preview_command)
        self.assertIn("--dry-run", preview_command)
        self.assertNotIn("--incremental-pool", preview_command)
        self.assertIn("--incremental-pool", write_command)
        self.assertNotIn("--missing-only", write_command)
        self.assertTrue(preview_metadata["incremental_preflight"])
        self.assertTrue(write_metadata["incremental_pool"])

    def test_load_westerman_pool_snapshot_reports_durable_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            pool_path = tmp / "westerman.sqlite3"
            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"pool_path": str(pool_path)}}),
                encoding="utf-8",
            )
            from clients.westerman_api.pool import WestermanPodPool

            WestermanPodPool(pool_path).upsert_candidates(["WDB2B/OUT/01436"])

            snapshot = actions.load_westerman_pool_snapshot(str(config_path))

            self.assertTrue(snapshot["exists"])
            self.assertEqual(snapshot["total"], 1)
            self.assertEqual(snapshot["due_count"], 1)
            self.assertEqual(snapshot["pull_states"], {"pending": 1})

    def test_build_cmc_onedrive_download_all_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"cmc_onedrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_cmc_onedrive_command(
                config_path=str(config_path),
                limit=0,
                recursive=True,
                mode="download_all_files",
                download_file_id="",
                output_dir=str(tmp / "downloads"),
                workers=3,
                incremental=True,
            )

            self.assertEqual(label, "CMC OneDrive / 下载全部文件")
            self.assertIn("--download-all-files", command)
            self.assertIn("--recursive", command)
            self.assertIn("--workers", command)
            self.assertEqual(metadata["workers"], 3)
            self.assertEqual(metadata["mode"], "download_all_files")

    def test_build_emtc_gdrive_download_all_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"emtc_gdrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_emtc_gdrive_command(
                config_path=str(config_path),
                limit=0,
                recursive=True,
                mode="download_all_files",
                download_file_id="",
                output_dir=str(tmp / "downloads"),
                workers=4,
                export_mime_type="application/pdf",
                incremental=True,
            )

            self.assertEqual(label, "EMTC Google Drive / 下载全部文件")
            self.assertIn("--download-all-files", command)
            self.assertIn("--recursive", command)
            self.assertIn("--workers", command)
            self.assertIn("4", command)
            self.assertEqual(metadata["workers"], 4)
            self.assertEqual(metadata["mode"], "download_all_files")

    def test_build_westerman_command_uses_shipment_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            output_dir = tmp / "pod" / "Westerman"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": str(tmp / "pod")},
                        "westerman_api": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )

            _label, command, metadata = actions.build_westerman_api_command(
                config_path=str(config_path),
                shipment_reference="WDB2B/OUT/01358",
                mode="download",
                output_dir=str(output_dir),
            )

            reference_index = command.index("--shipment-reference")
            self.assertEqual(command[reference_index + 1], "WDB2B/OUT/01358")
            self.assertIn("--download", command)
            self.assertEqual(metadata["shipment_reference"], "WDB2B/OUT/01358")
            self.assertEqual(metadata["output_dir"], str(output_dir))

    def test_build_westerman_download_uses_config_default_when_output_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(
                json.dumps({"download": {"base_dir": str(tmp / "pod")}}),
                encoding="utf-8",
            )

            _label, command, metadata = actions.build_westerman_api_command(
                config_path=str(config_path),
                shipment_reference="WDB2B/OUT/01358",
                mode="download",
                output_dir="",
            )

            self.assertNotIn("--output-dir", command)
            self.assertIsNone(metadata["output_dir"])

    def test_build_westerman_download_rejects_absolute_output_outside_base_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(
                json.dumps({"download": {"base_dir": str(tmp / "pod")}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "download.base_dir"):
                actions.build_westerman_api_command(
                    config_path=str(config_path),
                    shipment_reference="WDB2B/OUT/01358",
                    mode="download",
                    output_dir=str(tmp / "outside"),
                )

    def test_build_westerman_download_rejects_parent_path_segment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            base_dir = tmp / "pod"
            config_path = tmp / "config.local.json"
            config_path.write_text(
                json.dumps({"download": {"base_dir": str(base_dir)}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "不能包含 .."):
                actions.build_westerman_api_command(
                    config_path=str(config_path),
                    shipment_reference="WDB2B/OUT/01358",
                    mode="download",
                    output_dir=str(base_dir / "nested" / ".." / "Westerman"),
                )

    def test_build_phase1a_command_requires_existing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "phase1a.json"
            config_path.write_text(json.dumps({"output_dir": "./out/demo"}), encoding="utf-8")

            label, command, metadata = actions.build_phase1a_command(config_path=str(config_path))

            self.assertEqual(label, "运行 POD 识别")
            self.assertEqual(command[-2:], ["--config", str(config_path.resolve())])
            self.assertEqual(metadata["phase1a_config_path"], str(config_path.resolve()))

    def test_build_pipeline_command_includes_westerman_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")

            label, command, metadata = actions.build_pipeline_command(
                config_path=str(config_path),
                phase1a_config_path=str(phase1a_path),
                cmc_incremental=True,
                emtc_incremental=True,
                cmc_limit=0,
                emtc_limit=0,
                cmc_workers=3,
                emtc_workers=3,
                westerman_limit=25,
                westerman_workers=4,
            )

            limit_index = command.index("--westerman-limit")
            workers_index = command.index("--westerman-workers")
            self.assertEqual(command[limit_index + 1], "25")
            self.assertEqual(command[workers_index + 1], "4")
            self.assertIn("Westerman + CMC + EMTC + 识别", label)
            self.assertEqual(metadata["westerman_limit"], 25)
            self.assertEqual(metadata["westerman_workers"], 4)
            self.assertFalse(metadata["skip_westerman"])

    def test_build_pipeline_command_requires_explicit_westerman_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text("{}", encoding="utf-8")
            phase1a_path.write_text("{}", encoding="utf-8")

            label, command, metadata = actions.build_pipeline_command(
                config_path=str(config_path),
                phase1a_config_path=str(phase1a_path),
                cmc_incremental=True,
                emtc_incremental=True,
                cmc_limit=0,
                emtc_limit=0,
                cmc_workers=3,
                emtc_workers=3,
            )

            self.assertIn("--skip-westerman", command)
            self.assertTrue(metadata["skip_westerman"])
            self.assertNotIn("Westerman", label)

    def test_build_pipeline_command_preserves_independent_incremental_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text("{}", encoding="utf-8")
            phase1a_path.write_text("{}", encoding="utf-8")

            label, command, metadata = actions.build_pipeline_command(
                config_path=str(config_path),
                phase1a_config_path=str(phase1a_path),
                cmc_incremental=True,
                emtc_incremental=False,
                cmc_limit=0,
                emtc_limit=0,
                cmc_workers=3,
                emtc_workers=3,
            )

            self.assertNotIn("--cmc-full-sync", command)
            self.assertIn("--emtc-full-sync", command)
            self.assertNotIn("--full-sync", command)
            self.assertIn("CMC 增量 / EMTC 全量", label)
            self.assertTrue(metadata["cmc_incremental"])
            self.assertFalse(metadata["emtc_incremental"])

    def test_pipeline_westerman_failure_is_visible_but_phase1a_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            payload_path = tmp / "phase1a" / "out" / "test" / "odoo_backfill_payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            labels = []

            def fake_run_step(label, _command):
                labels.append(label)
                if label == "Westerman API 同步":
                    summary = {
                        "candidate_count": 8,
                        "due_count": 5,
                        "checked_count": 0,
                        "waiting_pod_count": 0,
                        "downloaded_count": 0,
                        "already_downloaded_count": 0,
                        "retry_count": 0,
                        "failed_count": 5,
                        "scan_truncated": True,
                        "candidate_refresh_status": "truncated",
                        "error_code": "CANDIDATE_SCAN_TRUNCATED",
                    }
                    return 2, "provider authentication failed\n" + json.dumps(summary, indent=2) + "\n"
                return 0, ""

            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(pipeline_runner, "_run_step", side_effect=fake_run_step),
                mock.patch.object(pipeline_runner, "_phase1a_summary_snapshot", return_value={}),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, payload_path),
                ),
                mock.patch(
                    "webapp.odoo_push.push_payload",
                    return_value={"ok": True, "total": 0},
                ) as push_payload,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_reports=True,
                    skip_odoo_push=False,
                )

            self.assertEqual(labels, ["Westerman API 同步", "Phase 1A 识别"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["degraded"])
            self.assertEqual(result["failed_step"], "westerman_sync")
            self.assertEqual(result["soft_failed_steps"], ["westerman_sync"])
            self.assertTrue(next(step for step in result["steps"] if step["step"] == "phase1a")["ok"])
            self.assertEqual(result["data_report"]["westerman_sync"]["candidate_count"], 8)
            self.assertEqual(result["data_report"]["westerman_sync"]["failed_count"], 5)
            self.assertTrue(result["data_report"]["westerman_sync"]["scan_truncated"])
            self.assertEqual(
                result["data_report"]["westerman_sync"]["candidate_refresh_status"],
                "truncated",
            )
            self.assertEqual(
                result["data_report"]["westerman_sync"]["error_code"],
                "CANDIDATE_SCAN_TRUNCATED",
            )
            self.assertEqual(
                push_payload.call_args.kwargs["excluded_source_types"],
                {"westerman_api"},
            )

    def test_pipeline_complete_westerman_auth_failure_uses_per_item_pool_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            payload_path = (
                tmp / "phase1a" / "out" / "test" / "odoo_backfill_payload.json"
            )
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            def fake_run_step(label, _command):
                if label == "Westerman API 同步":
                    return 3, json.dumps(
                        {
                            "candidate_count": 2,
                            "checked_count": 1,
                            "downloaded_count": 1,
                            "partial_document_failure_count": 1,
                            "provider_auth_failed": True,
                            "candidate_refresh_status": "complete",
                            "error_code": "PROVIDER_AUTH_ERROR",
                        }
                    )
                return 0, ""

            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(
                    pipeline_runner, "_run_step", side_effect=fake_run_step
                ),
                mock.patch.object(
                    pipeline_runner, "_phase1a_summary_snapshot", return_value={}
                ),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, payload_path),
                ),
                mock.patch(
                    "webapp.odoo_push.push_payload",
                    return_value={"ok": True, "total": 0},
                ) as push_payload,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_reports=True,
                    skip_odoo_push=False,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["degraded"])
            self.assertEqual(result["soft_failed_steps"], ["westerman_sync"])
            self.assertEqual(
                result["data_report"]["westerman_sync"][
                    "candidate_refresh_status"
                ],
                "complete",
            )
            self.assertIsNone(
                push_payload.call_args.kwargs["excluded_source_types"]
            )

    def test_pipeline_reports_westerman_pool_gate_as_degraded_after_other_pushes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            payload_path = tmp / "phase1a" / "out" / "test" / "odoo_backfill_payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            def fake_run_step(label, _command):
                if label == "Westerman API 同步":
                    return 0, json.dumps(
                        {
                            "candidate_count": 1,
                            "candidate_refresh_status": "complete",
                        }
                    )
                return 0, ""

            push_result = {
                "ok": True,
                "total": 1,
                "tracking_pushed": 1,
                "westerman_pool_gate_status": "candidate_refresh_stale",
                "westerman_pool_filtered": 1,
                "westerman_candidate_refresh_max_age_hours": 48,
            }
            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(pipeline_runner, "_run_step", side_effect=fake_run_step),
                mock.patch.object(pipeline_runner, "_phase1a_summary_snapshot", return_value={}),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, payload_path),
                ),
                mock.patch("webapp.odoo_push.push_payload", return_value=push_result),
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_reports=True,
                    skip_odoo_push=False,
                )

            self.assertFalse(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["failed_step"], "westerman_pool_gate")
            self.assertEqual(result["soft_failed_steps"], ["westerman_pool_gate"])
            self.assertEqual(
                result["data_report"]["odoo_push"]["tracking_pushed"], 1
            )

    def test_pipeline_reports_phase_gate_degraded_when_odoo_push_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            labels = []

            def fake_run_step(label, _command):
                labels.append(label)
                if label == "Westerman API 同步":
                    return 0, json.dumps(
                        {
                            "candidate_count": 1,
                            "candidate_refresh_status": "complete",
                        }
                    )
                return 0, ""

            phase_summary = {
                "document_count": 1,
                "payload_item_count": 1,
                "run_name": "test",
                "westerman_source_gate": {
                    "status": "candidate_refresh_stale",
                    "filtered_record_count": 2,
                    "candidate_refresh_status": "complete",
                    "candidate_refresh_age_seconds": 176400,
                    "candidate_refresh_max_age_hours": 48,
                },
            }
            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(
                    pipeline_runner, "_run_step", side_effect=fake_run_step
                ),
                mock.patch.object(
                    pipeline_runner,
                    "_phase1a_summary_snapshot",
                    return_value={},
                ),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=(phase_summary, tmp / "unused-payload.json"),
                ),
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_reports=True,
                    skip_odoo_push=True,
                )

        self.assertEqual(labels, ["Westerman API 同步", "Phase 1A 识别"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["failed_step"], "westerman_pool_gate")
        self.assertEqual(result["soft_failed_steps"], ["westerman_pool_gate"])
        self.assertEqual(
            result["data_report"]["phase1a"]["westerman_source_gate"][
                "status"
            ],
            "candidate_refresh_stale",
        )
        self.assertEqual(
            result["data_report"]["phase1a"]["westerman_source_gate"][
                "filtered_record_count"
            ],
            2,
        )

    def test_pipeline_cmc_failure_isolated_from_other_sources_and_push(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            payload_path = tmp / "phase1a" / "out" / "test" / "odoo_backfill_payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            labels = []

            def fake_run_step(label, _command):
                labels.append(label)
                if label == "Westerman API 同步":
                    return 0, json.dumps(
                        {
                            "candidate_count": 2,
                            "checked_count": 2,
                            "candidate_refresh_status": "ok",
                            "scan_truncated": False,
                        }
                    )
                if label == "CMC OneDrive 同步":
                    return 1, json.dumps(
                        {
                            "event": "collection_cursor_failed",
                            "source": "onedrive",
                            "restart_count": 2,
                        }
                    )
                if label == "EMTC Google Drive 同步":
                    return 0, json.dumps(
                        {
                            "event": "sync_summary",
                            "source": "gdrive",
                            "downloaded_files": 1,
                        }
                    )
                if label == "Phase 1A 识别":
                    return 0, ""
                self.fail(f"unexpected pipeline step: {label}")

            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(pipeline_runner, "_run_step", side_effect=fake_run_step),
                mock.patch.object(pipeline_runner, "_phase1a_summary_snapshot", return_value={}),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, payload_path),
                ),
                mock.patch(
                    "webapp.odoo_push.push_payload",
                    return_value={"ok": True, "total": 0},
                ) as push_payload,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_odoo_push=False,
                    skip_reports=True,
                )

            self.assertEqual(
                labels,
                [
                    "Westerman API 同步",
                    "CMC OneDrive 同步",
                    "EMTC Google Drive 同步",
                    "Phase 1A 识别",
                ],
            )
            self.assertFalse(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["failed_step"], "cmc_sync")
            self.assertEqual(result["soft_failed_steps"], ["cmc_sync"])
            self.assertEqual(
                result["data_report"]["westerman_sync"]["candidate_count"],
                2,
            )
            self.assertEqual(result["data_report"]["cmc_sync"]["status"], "failed")
            self.assertEqual(result["data_report"]["cmc_sync"]["exit_code"], 1)
            self.assertEqual(result["data_report"]["cmc_sync"]["cursor_failure_count"], 1)
            self.assertEqual(result["data_report"]["emtc_sync"]["downloaded_files"], 1)
            self.assertTrue(next(step for step in result["steps"] if step["step"] == "emtc_sync")["ok"])
            self.assertTrue(next(step for step in result["steps"] if step["step"] == "phase1a")["ok"])
            self.assertEqual(
                push_payload.call_args.kwargs["excluded_source_types"],
                {"onedrive"},
            )

    def test_pipeline_emtc_failure_isolated_from_other_sources_and_push(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            phase1a_path = tmp / "phase1a.json"
            config_path.write_text(
                json.dumps({"westerman_api": {"enabled": True}}),
                encoding="utf-8",
            )
            phase1a_path.write_text("{}", encoding="utf-8")
            payload_path = tmp / "phase1a" / "out" / "test" / "odoo_backfill_payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            labels = []

            def fake_run_step(label, _command):
                labels.append(label)
                if label == "Westerman API 同步":
                    return 0, json.dumps(
                        {
                            "candidate_count": 1,
                            "candidate_refresh_status": "complete",
                        }
                    )
                if label == "CMC OneDrive 同步":
                    return 0, json.dumps(
                        {
                            "event": "sync_summary",
                            "source": "onedrive",
                            "downloaded_files": 1,
                        }
                    )
                if label == "EMTC Google Drive 同步":
                    return 1, json.dumps(
                        {
                            "event": "sync_summary",
                            "source": "gdrive",
                            "failed_files": 1,
                        }
                    )
                if label == "Phase 1A 识别":
                    return 0, ""
                self.fail(f"unexpected pipeline step: {label}")

            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", tmp),
                mock.patch.object(pipeline_runner, "_run_step", side_effect=fake_run_step),
                mock.patch.object(pipeline_runner, "_phase1a_summary_snapshot", return_value={}),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, payload_path),
                ),
                mock.patch(
                    "webapp.odoo_push.push_payload",
                    return_value={"ok": True, "total": 0},
                ) as push_payload,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase1a_path),
                    skip_odoo_push=False,
                    skip_reports=True,
                )

            self.assertEqual(
                labels,
                [
                    "Westerman API 同步",
                    "CMC OneDrive 同步",
                    "EMTC Google Drive 同步",
                    "Phase 1A 识别",
                ],
            )
            self.assertFalse(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["failed_step"], "emtc_sync")
            self.assertEqual(result["soft_failed_steps"], ["emtc_sync"])
            self.assertEqual(result["data_report"]["emtc_sync"]["status"], "failed")
            self.assertEqual(result["data_report"]["emtc_sync"]["exit_code"], 1)
            self.assertEqual(result["data_report"]["cmc_sync"]["downloaded_files"], 1)
            self.assertTrue(next(step for step in result["steps"] if step["step"] == "phase1a")["ok"])
            self.assertEqual(
                push_payload.call_args.kwargs["excluded_source_types"],
                {"gdrive"},
            )

    def test_build_refresh_remote_counts_command_requires_existing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"cmc_onedrive": {"enabled": True}}), encoding="utf-8")

            label, command, metadata = actions.build_refresh_remote_counts_command(config_path=str(config_path))

            self.assertEqual(label, "刷新远端数量对比")
            self.assertEqual(command[-2:], ["--config", str(config_path.resolve())])
            self.assertIn("refresh_remote_counts.py", command[1])
            self.assertEqual(metadata["action_type"], "refresh_remote_counts")

    def test_load_runtime_config_snapshot_masks_secret_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "download": {"base_dir": "./storage/pod"},
                        "cmc_onedrive": {
                            "enabled": True,
                            "warehouse": "CMC",
                            "access_token": "abc",
                            "refresh_token": "def",
                            "root_item_id": "item-1",
                        },
                        "emtc_gdrive": {
                            "enabled": True,
                            "warehouse": "EMTC",
                            "access_token": "",
                            "refresh_token": "",
                            "folder_id": "folder-1",
                        },
                        "westerman_api": {
                            "enabled": False,
                            "warehouse": "Westerman",
                            "api_key": "secret",
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = actions.load_runtime_config_snapshot(str(config_path))

            self.assertTrue(snapshot["exists"])
            self.assertEqual(snapshot["sources"][0]["has_access_token"], True)
            self.assertEqual(snapshot["sources"][0]["has_secret"], True)
            self.assertEqual(snapshot["sources"][1]["has_folder_id"], True)
            self.assertEqual(snapshot["sources"][2]["has_secret"], True)
            self.assertEqual(snapshot["storage_dir"], str((tmp / "storage" / "pod").resolve()))

    def test_resolve_storage_dir_uses_config_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.local.json"
            config_path.write_text(json.dumps({"download": {"base_dir": "./downloads/pod"}}), encoding="utf-8")

            resolved = actions.resolve_storage_dir(str(config_path))

            self.assertEqual(resolved, (tmp / "downloads" / "pod").resolve())

    def test_pipeline_push_uses_payload_from_current_phase_run_not_stale_latest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            output_root = root / "phase1a" / "out"
            stale_dir = output_root / "999999999999"
            stale_dir.mkdir(parents=True)
            stale_payload = stale_dir / "odoo_backfill_payload.json"
            stale_payload.write_text(json.dumps({"items": []}), encoding="utf-8")
            (stale_dir / "phase1a_summary.json").write_text(
                json.dumps({"run_name": "stale"}), encoding="utf-8"
            )
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text(
                json.dumps(
                    {"output_dir": str(output_root), "auto_run_dir": True}
                ),
                encoding="utf-8",
            )

            current_dir = output_root / "202607180001"
            current_payload = current_dir / "odoo_backfill_payload.json"
            current_summary_path = current_dir / "phase1a_summary.json"
            generated_at = "2026-07-18T01:02:03Z"
            summary = {
                "job_id": "pod-recognition-current",
                "run_name": "202607180001",
                "generated_at": generated_at,
                "output_dir": str(current_dir),
                "artifacts": {
                    "payload_json": str(current_payload),
                    "summary_json": str(current_summary_path),
                },
            }

            def fake_run_step(label, _command):
                self.assertEqual(label, "Phase 1A 识别")
                current_dir.mkdir(parents=True)
                current_payload.write_text(
                    json.dumps(
                        {
                            "job_id": summary["job_id"],
                            "generated_at": generated_at,
                            "items": [],
                        }
                    ),
                    encoding="utf-8",
                )
                current_summary_path.write_text(
                    json.dumps(summary), encoding="utf-8"
                )
                return 0, json.dumps(summary)

            with (
                mock.patch.object(pipeline_runner, "PROJECT_ROOT", root),
                mock.patch.object(
                    pipeline_runner, "_run_step", side_effect=fake_run_step
                ),
                mock.patch(
                    "webapp.odoo_push.push_payload",
                    return_value={"ok": True, "status": "ok", "total": 0},
                ) as push_payload,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_reports=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            push_payload.call_args.kwargs["payload_path"], str(current_payload)
        )
        self.assertNotEqual(
            push_payload.call_args.kwargs["payload_path"], str(stale_payload)
        )
        self.assertEqual(
            result["data_report"]["odoo_push"]["payload_origin"],
            "current_phase1a_run",
        )
        self.assertEqual(
            result["data_report"]["phase1a"]["summary_path"],
            str(current_summary_path.resolve()),
        )
        self.assertEqual(
            result["data_report"]["phase1a"]["output_dir"],
            str(current_dir.resolve()),
        )
        self.assertEqual(
            result["data_report"]["phase1a"]["payload_path"],
            str(current_payload.resolve()),
        )

    def test_current_phase_result_falls_back_to_only_changed_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "phase1a.json"
            output_dir = root / "phase1a-output"
            payload_path = output_dir / "odoo_backfill_payload.json"
            summary_path = output_dir / "phase1a_summary.json"
            config_path.write_text(
                json.dumps({"output_dir": str(output_dir)}), encoding="utf-8"
            )
            before = pipeline_runner._phase1a_summary_snapshot(config_path)
            output_dir.mkdir()
            payload = {
                "job_id": "pod-recognition-current",
                "generated_at": "2026-07-18T01:02:03Z",
                "items": [],
            }
            summary = {
                "job_id": payload["job_id"],
                "run_name": output_dir.name,
                "generated_at": payload["generated_at"],
                "output_dir": str(output_dir),
                "artifacts": {
                    "payload_json": str(payload_path),
                    "summary_json": str(summary_path),
                },
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            detected_summary, detected_payload = (
                pipeline_runner._current_phase1a_result(
                    output="wrapper suppressed child stdout",
                    config_path=config_path,
                    before=before,
                )
            )

        self.assertEqual(detected_summary["job_id"], payload["job_id"])
        self.assertEqual(detected_payload, payload_path.resolve())

    def test_current_phase_result_rejects_unchanged_old_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "phase1a.json"
            output_dir = root / "phase1a-output"
            output_dir.mkdir()
            payload_path = output_dir / "odoo_backfill_payload.json"
            summary_path = output_dir / "phase1a_summary.json"
            config_path.write_text(
                json.dumps({"output_dir": str(output_dir)}), encoding="utf-8"
            )
            payload = {
                "job_id": "pod-recognition-stale",
                "generated_at": "2026-07-17T01:02:03Z",
                "items": [],
            }
            summary = {
                "job_id": payload["job_id"],
                "run_name": output_dir.name,
                "generated_at": payload["generated_at"],
                "output_dir": str(output_dir),
                "artifacts": {
                    "payload_json": str(payload_path),
                    "summary_json": str(summary_path),
                },
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            before = pipeline_runner._phase1a_summary_snapshot(config_path)

            with self.assertRaisesRegex(
                ValueError, "summary was not produced by this run"
            ):
                pipeline_runner._current_phase1a_result(
                    output=json.dumps(summary),
                    config_path=config_path,
                    before=before,
                )

    def test_pipeline_skipped_phase_fails_closed_instead_of_using_stale_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            stale_payload = (
                root
                / "phase1a"
                / "out"
                / "999999999999"
                / "odoo_backfill_payload.json"
            )
            stale_payload.parent.mkdir(parents=True)
            stale_payload.write_text(json.dumps({"items": []}), encoding="utf-8")
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text("{}", encoding="utf-8")

            with mock.patch("webapp.odoo_push.push_payload") as push_payload:
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    skip_reports=True,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_step"], "odoo_push_payload")
        self.assertIn("--odoo-push-payload", result["error"])
        push_payload.assert_not_called()

    def test_pipeline_allows_explicit_payload_only_when_phase_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            payload_path = root / "explicit-payload.json"
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text("{}", encoding="utf-8")
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            with mock.patch(
                "webapp.odoo_push.push_payload",
                return_value={"ok": True, "status": "ok", "total": 0},
            ) as push_payload:
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    skip_reports=True,
                    odoo_push_payload_path=str(payload_path),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data_report"]["odoo_push"]["payload_origin"], "explicit"
        )
        self.assertEqual(
            push_payload.call_args.kwargs["payload_path"], str(payload_path)
        )
        self.assertTrue(push_payload.call_args.kwargs["incremental_pool"])

    def test_partial_push_refreshes_reports_before_pipeline_returns_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            payload_path = root / "explicit-payload.json"
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text("{}", encoding="utf-8")
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            push_result = {
                "ok": False,
                "status": "partial",
                "total": 2,
                "input_total": 3,
                "eligible_document_count": 3,
                "selected_target_count": 2,
                "coalesced_count": 1,
                "superseded_count": 1,
                "attempted": 2,
                "completed": 1,
                "incomplete": 1,
                "retryable": 1,
                "failed": 1,
            }

            with (
                mock.patch(
                    "webapp.odoo_push.push_payload", return_value=push_result
                ),
                mock.patch(
                    "webapp.odoo_pull.fetch_and_save_index",
                    return_value=mock.Mock(
                        summary=lambda: {},
                        shadow_status="",
                        coverage_status="complete",
                    ),
                ),
                mock.patch(
                    "webapp.odoo_missing_report.refresh_odoo_missing_snapshot",
                    return_value={"warnings": [], "shadow_comparison": {}},
                ),
                mock.patch(
                    "webapp.odoo_pod_compare.refresh_pod_odoo_compare_snapshot"
                ) as compare_mock,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    odoo_push_payload_path=str(payload_path),
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_step"], "odoo_push")
        self.assertEqual(result["data_report"]["odoo_push"]["status"], "partial")
        self.assertEqual(result["data_report"]["odoo_push"]["input_total"], 3)
        self.assertEqual(
            result["data_report"]["odoo_push"]["selected_target_count"], 2
        )
        self.assertEqual(
            result["data_report"]["odoo_push"]["coalesced_count"], 1
        )
        self.assertEqual(result["data_report"]["odoo_push"]["attempted"], 2)
        self.assertTrue(
            next(
                step
                for step in result["steps"]
                if step["step"] == "refresh_odoo_pod_index"
            )["ok"]
        )
        compare_mock.assert_called_once()

    def test_business_attention_push_keeps_pipeline_successful(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            payload_path = root / "explicit-payload.json"
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text("{}", encoding="utf-8")
            payload_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            push_result = {
                "ok": True,
                "status": "ok",
                "execution_status": "ok",
                "business_status": "attention",
                "business_pending": 5009,
                "technical_failure_count": 0,
                "total": 40162,
                "input_total": 40968,
                "selected_target_count": 40162,
                "coalesced_count": 806,
                "attempted": 40162,
                "completed": 35153,
                "incomplete": 5009,
                "retryable": 3929,
                "tracking_pushed": 26,
                "tracking_skipped_same": 35127,
                "tracking_not_found": 3503,
                "picking_not_found": 426,
                "ambiguous": 642,
                "needs_review": 438,
                "failed": 0,
            }

            with mock.patch(
                "webapp.odoo_push.push_payload", return_value=push_result
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    skip_reports=True,
                    odoo_push_payload_path=str(payload_path),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["business_status"], "attention")
        self.assertEqual(result["business_attention_steps"], ["odoo_push"])
        self.assertEqual(
            result["data_report"]["odoo_push"]["business_pending"], 5009
        )
        push_step = next(
            step for step in result["steps"] if step["step"] == "odoo_push"
        )
        self.assertTrue(push_step["ok"])
        self.assertEqual(push_step["status"], "attention")

    def test_report_only_pipeline_fails_and_skips_dependent_compare_on_missing_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.local.json"
            config_path.write_text("{}", encoding="utf-8")

            with (
                mock.patch(
                    "webapp.odoo_pull.fetch_and_save_index",
                    return_value=mock.Mock(
                        summary=lambda: {},
                        shadow_status="",
                        coverage_status="complete",
                    ),
                ),
                mock.patch(
                    "webapp.odoo_missing_report.refresh_odoo_missing_snapshot",
                    side_effect=RuntimeError("temporary report failure"),
                ),
                mock.patch(
                    "webapp.odoo_pod_compare.refresh_pod_odoo_compare_snapshot",
                    return_value={"ok": True},
                ) as compare_mock,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    skip_odoo_push=True,
                    skip_reports=False,
                )

            self.assertFalse(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["warnings"][0]["step"], "refresh_odoo_missing")
            self.assertFalse(
                next(step for step in result["steps"] if step["step"] == "refresh_odoo_missing")["ok"]
            )
            self.assertFalse(
                next(step for step in result["steps"] if step["step"] == "refresh_pod_compare")["ok"]
            )
            compare_mock.assert_not_called()

    def test_incomplete_coverage_skips_compare_and_fails_report_only_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.local.json"
            config_path.write_text("{}", encoding="utf-8")

            with (
                mock.patch(
                    "webapp.odoo_pull.fetch_and_save_index",
                    return_value=mock.Mock(
                        summary=lambda: {},
                        shadow_status="",
                        coverage_status="unavailable:TimeoutError",
                    ),
                ),
                mock.patch(
                    "webapp.odoo_missing_report.refresh_odoo_missing_snapshot",
                    return_value={},
                ),
                mock.patch(
                    "webapp.odoo_pod_compare.refresh_pod_odoo_compare_snapshot"
                ) as compare_mock,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=True,
                    skip_odoo_push=True,
                    skip_reports=False,
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["degraded"])
        self.assertEqual(
            result["warnings"][0]["step"], "refresh_odoo_pod_index_coverage"
        )
        compare_mock.assert_not_called()

    def test_successful_business_step_is_not_reversed_by_report_refresh_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.local.json"
            phase_path = root / "phase1a.json"
            config_path.write_text("{}", encoding="utf-8")
            phase_path.write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(pipeline_runner, "_run_step", return_value=(0, "")),
                mock.patch.object(
                    pipeline_runner, "_phase1a_summary_snapshot", return_value={}
                ),
                mock.patch.object(
                    pipeline_runner,
                    "_current_phase1a_result",
                    return_value=({}, root / "unused-payload.json"),
                ),
                mock.patch(
                    "webapp.odoo_pull.fetch_and_save_index",
                    return_value=mock.Mock(
                        summary=lambda: {},
                        shadow_status="",
                        coverage_status="complete",
                    ),
                ),
                mock.patch(
                    "webapp.odoo_missing_report.refresh_odoo_missing_snapshot",
                    side_effect=RuntimeError("temporary report failure"),
                ),
                mock.patch(
                    "webapp.odoo_pod_compare.refresh_pod_odoo_compare_snapshot"
                ) as compare_mock,
            ):
                result = pipeline_runner.run_pipeline(
                    config_path=str(config_path),
                    phase1a_config_path=str(phase_path),
                    skip_westerman=True,
                    skip_cmc=True,
                    skip_emtc=True,
                    skip_phase1a=False,
                    skip_odoo_push=True,
                    skip_reports=False,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["warnings"][0]["step"], "refresh_odoo_missing")
            compare_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
