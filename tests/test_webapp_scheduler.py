import json
import sys
import tempfile
import unittest
from pathlib import Path
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


class SchedulerWestermanTests(unittest.TestCase):
    def test_default_config_disables_westerman_sync(self):
        config = scheduler_module._default_config()

        self.assertFalse(config["steps"]["westerman_sync"])
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
            self.assertFalse(config["steps"]["westerman_sync"])
            self.assertEqual(config["pipeline_options"]["cmc_workers"], 7)
            self.assertEqual(config["pipeline_options"]["westerman_workers"], 3)

    def test_load_config_preserves_explicit_westerman_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "scheduler.json"
            config_path.write_text(
                json.dumps({"steps": {"westerman_sync": True}}),
                encoding="utf-8",
            )
            instance = scheduler_module.Scheduler(
                _FakeJobStore(),
                config_path=config_path,
                log_path=tmp / "scheduler.log",
            )

            self.assertTrue(instance.load_config()["steps"]["westerman_sync"])

    def test_scheduler_passes_westerman_options_and_step_toggle(self):
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
            self.assertTrue(kwargs["skip_westerman"])
            self.assertEqual(job_store.started[0]["action"], "run_pipeline")


if __name__ == "__main__":
    unittest.main()
