import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp import scheduler as scheduler_module


class _FakeJobStore:
    def __init__(self):
        self.started = []

    def start_job(self, **kwargs):
        self.started.append(kwargs)
        return {"job_id": "job-westerman"}


def _run_concurrent_scheduler_tick(
    config_path: str,
    log_path: str,
    claim_path: str,
    marker_path: str,
    barrier,
):
    instance = scheduler_module.Scheduler(
        _FakeJobStore(),
        config_path=config_path,
        log_path=log_path,
        claim_path=claim_path,
    )

    def claimed_job():
        with Path(marker_path).open("a", encoding="utf-8") as marker:
            marker.write(f"{os.getpid()}\n")
        # Widen the race: without the process claim both workers read the due
        # config before either one commits next_run_at.
        time.sleep(0.2)
        return f"job-{os.getpid()}"

    instance._run_pipeline_job = claimed_job
    barrier.wait(timeout=10)
    instance.tick()


class SchedulerWestermanTests(unittest.TestCase):
    def _call_update_scheduler(self, app_module, *, steps, start_time="09:00"):
        return asyncio.run(
            app_module.action_update_scheduler(
                enabled="1",
                interval_hours=24,
                start_time=start_time,
                pipeline_config_path="config.local.json",
                pipeline_phase1a_config_path="phase1a/config.json",
                pipeline_cmc_incremental=True,
                pipeline_emtc_incremental=True,
                pipeline_cmc_limit=0,
                pipeline_emtc_limit=0,
                pipeline_cmc_workers=3,
                pipeline_emtc_workers=3,
                pipeline_westerman_limit=0,
                pipeline_westerman_workers=3,
                step_cmc_sync=steps["cmc_sync"],
                step_emtc_sync=steps["emtc_sync"],
                step_westerman_sync=steps["westerman_sync"],
                step_phase1a=steps["phase1a"],
                step_refresh_reports=steps["refresh_reports"],
                step_odoo_push=steps["odoo_push"],
            )
        )

    def _call_manual_pipeline(self, app_module):
        return asyncio.run(
            app_module.action_run_pipeline(
                config_path="config.local.json",
                phase1a_config_path="phase1a/config.json",
                cmc_incremental=True,
                emtc_incremental=True,
                cmc_limit=0,
                emtc_limit=0,
                cmc_workers=3,
                emtc_workers=3,
                westerman_limit=0,
                westerman_workers=3,
                skip_westerman=False,
                use_saved_scheduler_config=True,
                redirect_to="/scheduler",
            )
        )

    def test_importing_app_does_not_start_scheduler(self):
        env = os.environ.copy()
        env.pop("POD_SYNC_DISABLE_SCHEDULER", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        script = (
            "from unittest import mock\n"
            "with mock.patch('webapp.scheduler.Scheduler.start', "
            "side_effect=RuntimeError('scheduler started during import')):\n"
            "    import webapp.app\n"
            "print('imported-without-start')\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("imported-without-start", completed.stdout)

    def test_app_lifespan_starts_and_stops_scheduler_by_default(self):
        from webapp import app as app_module

        async def exercise_lifespan():
            async with app_module.app_lifespan(app_module.app):
                return None

        with mock.patch.dict(
            os.environ, {"POD_SYNC_DISABLE_SCHEDULER": ""}
        ), mock.patch.object(
            app_module.scheduler, "start", return_value=True
        ) as start, mock.patch.object(app_module.scheduler, "stop") as stop:
            asyncio.run(exercise_lifespan())

        start.assert_called_once_with()
        stop.assert_called_once_with()

    def test_app_lifespan_honors_scheduler_disable_environment(self):
        from webapp import app as app_module

        async def exercise_lifespan():
            async with app_module.app_lifespan(app_module.app):
                return None

        with mock.patch.dict(
            os.environ, {"POD_SYNC_DISABLE_SCHEDULER": "1"}
        ), mock.patch.object(app_module.scheduler, "start") as start, mock.patch.object(
            app_module.scheduler, "stop"
        ) as stop:
            asyncio.run(exercise_lifespan())

        start.assert_not_called()
        stop.assert_not_called()

    def test_default_config_enables_westerman_sync_step(self):
        config = scheduler_module._default_config()

        self.assertFalse(config["enabled"])
        self.assertEqual(config["start_time"], "09:00")
        self.assertEqual(config["schedule_timezone"], "Asia/Hong_Kong")
        self.assertTrue(config["steps"]["westerman_sync"])
        self.assertEqual(config["pipeline_options"]["westerman_limit"], 0)
        self.assertEqual(config["pipeline_options"]["westerman_workers"], 3)

    def test_load_config_adds_westerman_defaults_to_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "scheduler.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "steps": {"cmc_sync": False},
                        "pipeline_options": {"cmc_workers": 7},
                    }
                ),
                encoding="utf-8",
            )
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=config_path,
                log_path=tmp / "scheduler.log",
            )

            config = instance.load_config()

            self.assertFalse(config["steps"]["cmc_sync"])
            self.assertTrue(config["steps"]["westerman_sync"])
            self.assertEqual(config["pipeline_options"]["cmc_workers"], 7)
            self.assertEqual(config["pipeline_options"]["westerman_workers"], 3)

    def test_load_config_migrates_legacy_schedule_without_moving_clock_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "scheduler.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "interval_hours": 12,
                        "next_run_at": "2026-08-04T09:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=config_path,
                log_path=tmp / "scheduler.log",
            )

            config = instance.load_config()

            self.assertEqual(config["start_time"], "17:00")
            self.assertEqual(config["next_run_at"], "2026-08-04T09:00:00Z")

    def test_compute_next_run_uses_hong_kong_start_time_and_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["start_time"] = "09:30"
            config["interval_hours"] = 12

            before_first = instance.compute_next_run(
                config,
                now=datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc),
            )
            after_first = instance.compute_next_run(
                config,
                now=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(
                before_first,
                datetime(2026, 8, 4, 1, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(
                after_first,
                datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc),
            )

    def test_update_config_recomputes_next_run_only_when_schedule_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["enabled"] = True
            config["start_time"] = "09:00"
            config["next_run_at"] = "2099-01-01T01:00:00Z"
            instance.save_config(config)

            unchanged = instance.update_config(
                {"enabled": True, "interval_hours": 24, "start_time": "09:00"}
            )
            with mock.patch(
                "webapp.scheduler._utc_now",
                return_value=datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc),
            ):
                changed = instance.update_config({"start_time": "09:30"})

            self.assertEqual(unchanged["next_run_at"], "2099-01-01T01:00:00Z")
            self.assertEqual(changed["next_run_at"], "2026-08-04T01:30:00Z")

    def test_scheduler_config_save_rejects_invalid_start_time(self):
        from webapp import app as app_module

        steps = scheduler_module._default_config()["steps"]
        with mock.patch.object(app_module.scheduler, "update_config") as update_config:
            response = self._call_update_scheduler(
                app_module,
                steps=steps,
                start_time="25:00",
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("启动时间格式无效", unquote_plus(response.headers["location"]))
        update_config.assert_not_called()

    def test_load_config_preserves_explicit_westerman_opt_out_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "scheduler.json"
            config_path.write_text(
                json.dumps({"steps": {"westerman_sync": False}}),
                encoding="utf-8",
            )
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=config_path,
                log_path=tmp / "scheduler.log",
            )

            self.assertFalse(instance.load_config()["steps"]["westerman_sync"])
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"steps": {"westerman_sync": False}},
            )

    def test_scheduler_explicit_westerman_opt_out_reaches_pipeline_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            job_store = _FakeJobStore()
            instance = scheduler_module.Scheduler(
                job_store,
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["steps"]["westerman_sync"] = False
            instance.save_config(config)

            with mock.patch(
                "webapp.actions.build_pipeline_command",
                return_value=(
                    "pipeline",
                    ["python", "pipeline_runner.py"],
                    {"category": "pipeline"},
                ),
            ) as build_command:
                job_id = instance._run_pipeline_job()

            self.assertEqual(job_id, "job-westerman")
            self.assertTrue(build_command.call_args.kwargs["skip_westerman"])
            self.assertEqual(job_store.started[0]["action"], "run_pipeline")

    def test_status_exposes_westerman_step_toggle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )

            status = instance.status()

            westerman_step = next(
                step for step in status["step_definitions"]
                if step["key"] == "westerman_sync"
            )
            self.assertTrue(status["steps"]["westerman_sync"])
            self.assertTrue(westerman_step["default"])

    def test_scheduler_default_runs_westerman_with_configured_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            job_store = _FakeJobStore()
            instance = scheduler_module.Scheduler(
                job_store,
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["pipeline_options"]["westerman_limit"] = 40
            config["pipeline_options"]["westerman_workers"] = 5
            instance.save_config(config)

            with mock.patch(
                "webapp.actions.build_pipeline_command",
                return_value=("pipeline", ["python", "pipeline_runner.py"], {"category": "pipeline"}),
            ) as build_command:
                job_id = instance._run_pipeline_job()

            self.assertEqual(job_id, "job-westerman")
            kwargs = build_command.call_args.kwargs
            self.assertEqual(kwargs["westerman_limit"], 40)
            self.assertEqual(kwargs["westerman_workers"], 5)
            self.assertFalse(kwargs["skip_westerman"])
            self.assertEqual(job_store.started[0]["action"], "run_pipeline")

    def test_scheduler_config_save_rejects_invalid_step_combinations(self):
        from webapp import app as app_module

        invalid_cases = (
            (
                {
                    "cmc_sync": False,
                    "emtc_sync": False,
                    "westerman_sync": False,
                    "phase1a": False,
                    "refresh_reports": False,
                    "odoo_push": False,
                },
                "至少需要启用一个执行步骤",
            ),
            (
                {
                    "cmc_sync": True,
                    "emtc_sync": False,
                    "westerman_sync": False,
                    "phase1a": False,
                    "refresh_reports": False,
                    "odoo_push": True,
                },
                "必须同时启用 Phase 1A",
            ),
        )
        for steps, expected_message in invalid_cases:
            with self.subTest(expected_message=expected_message), mock.patch.object(
                app_module.scheduler, "update_config"
            ) as update_config:
                response = self._call_update_scheduler(app_module, steps=steps)

                self.assertEqual(response.status_code, 303)
                self.assertIn(
                    expected_message, unquote_plus(response.headers["location"])
                )
                update_config.assert_not_called()

    def test_manual_pipeline_rejects_invalid_saved_step_combinations(self):
        from webapp import app as app_module

        invalid_cases = (
            (
                {
                    "cmc_sync": False,
                    "emtc_sync": False,
                    "westerman_sync": False,
                    "phase1a": False,
                    "refresh_reports": False,
                    "odoo_push": False,
                },
                "至少需要启用一个执行步骤",
            ),
            (
                {
                    "cmc_sync": True,
                    "emtc_sync": False,
                    "westerman_sync": False,
                    "phase1a": False,
                    "refresh_reports": False,
                    "odoo_push": True,
                },
                "必须同时启用 Phase 1A",
            ),
        )
        for steps, expected_message in invalid_cases:
            saved_config = {
                "steps": steps,
                "pipeline_options": scheduler_module._default_config()[
                    "pipeline_options"
                ],
            }
            with self.subTest(expected_message=expected_message), mock.patch.object(
                app_module.scheduler, "load_config", return_value=saved_config
            ), mock.patch.object(
                app_module, "build_pipeline_command"
            ) as build_command, mock.patch.object(
                app_module.job_store, "start_job"
            ) as start_job:
                response = self._call_manual_pipeline(app_module)

                self.assertEqual(response.status_code, 303)
                self.assertIn(
                    expected_message, unquote_plus(response.headers["location"])
                )
                build_command.assert_not_called()
                start_job.assert_not_called()

    def test_background_scheduler_rejects_legacy_invalid_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            job_store = _FakeJobStore()
            instance = scheduler_module.Scheduler(
                job_store,
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["steps"]["phase1a"] = False
            config["steps"]["odoo_push"] = True
            instance.save_config(config)

            with mock.patch("webapp.actions.build_pipeline_command") as build_command:
                job_id = instance._run_pipeline_job()

            self.assertIsNone(job_id)
            build_command.assert_not_called()
            self.assertEqual(job_store.started, [])
            self.assertIn(
                "必须同时启用 Phase 1A",
                (tmp / "scheduler.log").read_text(encoding="utf-8"),
            )

    def test_failed_command_build_schedules_future_retry_without_fake_last_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["enabled"] = True
            config["next_run_at"] = "2020-01-01T00:00:00Z"
            instance.save_config(config)

            with mock.patch.object(instance, "_run_pipeline_job", return_value=None) as run_job:
                instance.tick()
                first = instance.load_config()
                instance.tick()

            self.assertEqual(run_job.call_count, 1)
            self.assertIsNone(first["last_run_at"])
            self.assertIsNone(first["last_run_job_id"])
            self.assertFalse(first["last_attempt_ok"])
            self.assertTrue(first["last_attempt_at"])
            self.assertTrue(first["next_run_at"])
            self.assertGreater(
                scheduler_module._parse_iso(first["next_run_at"]),
                scheduler_module._utc_now(),
            )

    def test_delayed_tick_advances_from_scheduled_time_without_cadence_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=tmp / "scheduler.json",
                log_path=tmp / "scheduler.log",
            )
            config = scheduler_module._default_config()
            config["enabled"] = True
            config["interval_hours"] = 12
            config["start_time"] = "09:30"
            config["next_run_at"] = "2026-08-04T01:30:00Z"
            instance.save_config(config)

            delayed_now = datetime(2026, 8, 4, 2, 5, tzinfo=timezone.utc)
            with mock.patch(
                "webapp.scheduler._utc_now",
                return_value=delayed_now,
            ), mock.patch.object(
                instance,
                "_run_pipeline_job",
                return_value="job-delayed",
            ):
                instance.tick()

            saved = instance.load_config()
            self.assertEqual(saved["last_run_at"], "2026-08-04T02:05:00Z")
            self.assertEqual(saved["last_scheduled_at"], "2026-08-04T01:30:00Z")
            self.assertEqual(saved["next_run_at"], "2026-08-04T13:30:00Z")

    def test_concurrent_worker_ticks_claim_one_scheduled_occurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "scheduler.json"
            log_path = tmp / "scheduler.log"
            claim_path = tmp / "scheduler.claim.lock"
            marker_path = tmp / "started-jobs.txt"
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=config_path,
                log_path=log_path,
                claim_path=claim_path,
            )
            config = scheduler_module._default_config()
            config["enabled"] = True
            config["next_run_at"] = "2020-01-01T00:00:00Z"
            instance.save_config(config)

            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(3)
            processes = [
                context.Process(
                    target=_run_concurrent_scheduler_tick,
                    args=(
                        str(config_path),
                        str(log_path),
                        str(claim_path),
                        str(marker_path),
                        barrier,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            barrier.wait(timeout=10)
            for process in processes:
                process.join(timeout=15)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            started = marker_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(started), 1)
            saved = instance.load_config()
            self.assertEqual(saved["last_run_job_id"], f"job-{started[0]}")
            self.assertGreater(
                scheduler_module._parse_iso(saved["next_run_at"]),
                scheduler_module._utc_now(),
            )


if __name__ == "__main__":
    unittest.main()
