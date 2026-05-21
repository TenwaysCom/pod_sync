import tempfile
import time
import unittest
from pathlib import Path
import sys
import json
import os
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.jobs import JobStore


class WebAppJobTests(unittest.TestCase):
    def test_job_store_runs_subprocess_and_captures_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job = store.start_job(
                action="test",
                label="Test command",
                command=["python3", "-c", "print('job runner ok')"],
                metadata={"category": "test"},
            )

            for _ in range(50):
                current = store.get_job(job["job_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.1)
            else:
                self.fail("Job did not finish in time.")

            self.assertEqual(current["status"], "success")
            self.assertEqual(current["exit_code"], 0)
            self.assertIn("job runner ok", store.read_log(job["job_id"]))

    def test_read_log_redacts_sensitive_urls_and_tails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-log-redact"
            log_path = store.root_dir / f"{job_id}.log"
            log_path.write_text(
                "prefix\n"
                "https://my.microsoftpersonalcontent.com/file.pdf?tempauth=secret-token\n"
                "https://api.example.test/file?access_token=secret-access\n"
                "tail\n",
                encoding="utf-8",
            )

            content = store.read_log(job_id, max_chars=500)

            self.assertIn("[REDACTED_MICROSOFT_DOWNLOAD_URL]", content)
            self.assertIn("access_token=[REDACTED]", content)
            self.assertNotIn("secret-token", content)
            self.assertNotIn("secret-access", content)

    def test_job_timeout_kills_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            marker = tmp_path / "child-survived.txt"
            store = JobStore(tmp_path / "jobs")
            store._job_timeout_seconds = 1
            child_code = (
                "import pathlib, time; "
                "time.sleep(3); "
                f"pathlib.Path({str(marker)!r}).write_text('alive', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(30)"
            )
            job = store.start_job(
                action="test",
                label="Timeout child",
                command=[sys.executable, "-c", parent_code],
            )

            for _ in range(80):
                current = store.get_job(job["job_id"])
                if current["status"] == "failed" and current["exit_code"] == -9:
                    break
                time.sleep(0.1)
            else:
                self.fail("Job did not time out in time.")

            time.sleep(3.5)
            self.assertEqual(current["status"], "failed")
            self.assertEqual(current["exit_code"], -9)
            self.assertFalse(marker.exists())

    def test_delete_job_removes_finished_job_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job = store.start_job(
                action="test",
                label="Delete me",
                command=["python3", "-c", "print('done')"],
            )

            for _ in range(50):
                current = store.get_job(job["job_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.1)
            else:
                self.fail("Job did not finish in time.")

            ok, error = store.delete_job(job["job_id"])

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertIsNone(store.get_job(job["job_id"]))
            self.assertFalse((store.root_dir / f"{job['job_id']}.log").exists())

    def test_delete_job_rejects_running_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-running"
            payload = {
                "job_id": job_id,
                "action": "test",
                "label": "Running job",
                "status": "running",
                "created_at": "2026-04-27T10:00:00Z",
                "started_at": "2026-04-27T10:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": os.getpid(),
                "cwd": str(PROJECT_ROOT),
                "command": ["python3", "-c", "import time; time.sleep(5)"],
                "command_display": "python3 -c 'import time; time.sleep(5)'",
                "log_path": str(store.root_dir / f"{job_id}.log"),
                "metadata": {},
            }
            (store.root_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            ok, error = store.delete_job(job_id)

            self.assertFalse(ok)
            self.assertEqual(error, "运行中的任务不能删除。")
            self.assertTrue((store.root_dir / f"{job_id}.json").exists())

    def test_get_job_recovers_stale_running_job_without_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-stale"
            payload = {
                "job_id": job_id,
                "action": "test",
                "label": "Stale job",
                "status": "running",
                "created_at": "2026-04-27T10:00:00Z",
                "started_at": "2026-04-27T08:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": None,
                "cwd": str(PROJECT_ROOT),
                "command": ["python3", "-c", "print('never')"],
                "command_display": "python3 -c \"print('never')\"",
                "log_path": str(store.root_dir / f"{job_id}.log"),
                "metadata": {},
            }
            (store.root_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            current = store.get_job(job_id)

            self.assertEqual(current["status"], "failed")
            self.assertEqual(current["exit_code"], -2)
            self.assertIsNotNone(current["finished_at"])
            self.assertIn("未记录 pid", current["status_note"])

    def test_get_job_keeps_recent_running_job_without_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-recent"
            payload = {
                "job_id": job_id,
                "action": "test",
                "label": "Recent job",
                "status": "running",
                "created_at": "2026-04-27T10:00:00Z",
                "started_at": "2026-04-27T10:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": None,
                "cwd": str(PROJECT_ROOT),
                "command": ["python3", "-c", "print('never')"],
                "command_display": "python3 -c \"print('never')\"",
                "log_path": str(store.root_dir / f"{job_id}.log"),
                "metadata": {},
            }
            with mock.patch("webapp.jobs.datetime") as mocked_datetime:
                from datetime import datetime, timezone

                mocked_datetime.now.return_value = datetime(2026, 4, 27, 10, 0, 30, tzinfo=timezone.utc)
                mocked_datetime.fromisoformat = datetime.fromisoformat

                (store.root_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")
                current = store.get_job(job_id)

            self.assertEqual(current["status"], "running")
            self.assertIsNone(current["exit_code"])

    def test_delete_job_allows_recovered_stale_running_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-stale-delete"
            payload = {
                "job_id": job_id,
                "action": "test",
                "label": "Stale job",
                "status": "running",
                "created_at": "2026-04-27T10:00:00Z",
                "started_at": "2026-04-27T08:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": None,
                "cwd": str(PROJECT_ROOT),
                "command": ["python3", "-c", "print('never')"],
                "command_display": "python3 -c \"print('never')\"",
                "log_path": str(store.root_dir / f"{job_id}.log"),
                "metadata": {},
            }
            (store.root_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            ok, error = store.delete_job(job_id)

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertIsNone(store.get_job(job_id))


if __name__ == "__main__":
    unittest.main()
