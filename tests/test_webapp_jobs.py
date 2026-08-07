import tempfile
import time
import unittest
from pathlib import Path
import sys
import json
import os
import subprocess
import threading
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.jobs import JobStore


class WebAppJobTests(unittest.TestCase):
    def _wait_for_finished(self, store, job_id, attempts=80):
        for _ in range(attempts):
            current = store.get_job(job_id)
            if current["status"] not in {"queued", "running"}:
                return current
            time.sleep(0.05)
        self.fail(f"Job {job_id} did not finish in time.")

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

    def test_job_store_captures_indented_pipeline_report_on_partial_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            report = {
                "ok": False,
                "status": "partial",
                "data_report": {
                    "westerman_sync": {
                        "candidate_count": 71,
                        "downloaded_count": 1,
                    }
                },
            }
            code = (
                "import json, sys; "
                f"print(json.dumps({report!r}, ensure_ascii=False, indent=2)); "
                "sys.exit(1)"
            )
            job = store.start_job(
                action="pipeline",
                label="Partial pipeline",
                command=[sys.executable, "-c", code],
            )

            for _ in range(50):
                current = store.get_job(job["job_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.1)
            else:
                self.fail("Job did not finish in time.")

            self.assertEqual(current["status"], "failed")
            self.assertEqual(current["exit_code"], 1)
            self.assertEqual(current["report"], report)

    def test_report_extractor_ignores_earlier_json_and_reads_final_pretty_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-pretty-report"
            expected = {
                "ok": True,
                "status": "ok",
                "steps": [{"name": "sync", "ok": True}],
            }
            (store.root_dir / f"{job_id}.log").write_text(
                "$ pipeline\n"
                '{"probe": "earlier"}\n'
                "pipeline output\n"
                + json.dumps(expected, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(store._extract_report_from_log(job_id), expected)

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

            owner_lock = store._owner_lock_path(job["job_id"])
            owner_lock.touch(exist_ok=True)

            ok, error = store.delete_job(job["job_id"])

            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertIsNone(store.get_job(job["job_id"]))
            self.assertFalse((store.root_dir / f"{job['job_id']}.log").exists())
            self.assertFalse(owner_lock.exists())

    def test_cleanup_finished_jobs_deletes_only_expired_terminal_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")

            def write_job(job_id, *, status, created_at, finished_at=None):
                payload = {
                    "job_id": job_id,
                    "action": "test",
                    "label": job_id,
                    "status": status,
                    "created_at": created_at,
                    "started_at": created_at,
                    "finished_at": finished_at,
                    "exit_code": None if status in {"queued", "running"} else 0,
                    "pid": os.getpid() if status == "running" else None,
                    "metadata": {},
                }
                store._write_job(payload)
                store._log_path(job_id).write_text(f"log for {job_id}", encoding="utf-8")
                store._owner_lock_path(job_id).touch()

            write_job(
                "job-expired",
                status="success",
                created_at="2000-01-01T00:00:00Z",
                finished_at="2000-01-01T00:01:00Z",
            )
            write_job(
                "job-recent",
                status="success",
                created_at="2999-01-01T00:00:00Z",
                finished_at="2999-01-01T00:01:00Z",
            )
            write_job(
                "job-running-old",
                status="running",
                created_at="2000-01-01T00:00:00Z",
            )

            summary = store.get_storage_summary(retention_days=30)
            result = store.cleanup_finished_jobs(retention_days=30)

            self.assertEqual(summary["total_count"], 3)
            self.assertEqual(summary["cleanup_count"], 1)
            self.assertGreater(summary["cleanup_bytes"], 0)
            self.assertEqual(result["deleted_count"], 1)
            self.assertEqual(result["deleted_bytes"], summary["cleanup_bytes"])
            for path in store._job_files("job-expired"):
                self.assertFalse(path.exists())
            self.assertIsNotNone(store.get_job("job-recent"))
            self.assertIsNotNone(store.get_job("job-running-old"))

    def test_cleanup_is_disabled_when_retention_is_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-old"
            store._write_job(
                {
                    "job_id": job_id,
                    "status": "success",
                    "created_at": "2000-01-01T00:00:00Z",
                    "finished_at": "2000-01-01T00:01:00Z",
                }
            )

            result = store.cleanup_finished_jobs(retention_days=0)

            self.assertEqual(result["deleted_count"], 0)
            self.assertTrue(store._job_path(job_id).exists())

    def test_finished_job_triggers_automatic_retention_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            store._job_retention_days = 30
            old_job_id = "job-expired-before-next-run"
            store._write_job(
                {
                    "job_id": old_job_id,
                    "status": "success",
                    "created_at": "2000-01-01T00:00:00Z",
                    "finished_at": "2000-01-01T00:01:00Z",
                }
            )
            store._log_path(old_job_id).write_text("old log", encoding="utf-8")

            current_job = store.start_job(
                action="test",
                label="Trigger cleanup",
                command=[sys.executable, "-c", "print('done')"],
            )
            self._wait_for_finished(store, current_job["job_id"])
            for _ in range(50):
                if not store._job_path(old_job_id).exists():
                    break
                time.sleep(0.02)

            self.assertFalse(store._job_path(old_job_id).exists())
            self.assertFalse(store._log_path(old_job_id).exists())

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

    def test_concurrent_pipeline_jobs_attach_their_reported_phase1a_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = JobStore(root / "jobs")
            store._semaphore = threading.Semaphore(2)
            output_root = root / "phase1a-out"
            config_path = root / "phase1a.json"
            config_path.write_text(
                json.dumps({"output_dir": str(output_root), "auto_run_dir": True}),
                encoding="utf-8",
            )

            summaries = {}
            for run_name, generated_at in (
                ("run-a", "2026-07-18T10:00:00Z"),
                ("run-b", "2026-07-18T10:01:00Z"),
            ):
                run_dir = output_root / run_name
                run_dir.mkdir(parents=True)
                summary_path = run_dir / "phase1a_summary.json"
                summary_path.write_text(
                    json.dumps({"generated_at": generated_at, "artifacts": {"owner": run_name}}),
                    encoding="utf-8",
                )
                summaries[run_name] = summary_path

            jobs = {}
            for run_name, delay in (("run-a", 0.15), ("run-b", 0.05)):
                summary_path = summaries[run_name]
                report = {
                    "ok": True,
                    "status": "ok",
                    "data_report": {
                        "phase1a": {
                            "run_name": run_name,
                            "summary_path": str(summary_path),
                            "output_dir": str(summary_path.parent),
                            "payload_path": str(summary_path.parent / "odoo_backfill_payload.json"),
                        }
                    },
                }
                code = (
                    "import json, time; "
                    f"time.sleep({delay}); "
                    f"print(json.dumps({report!r}, ensure_ascii=False, indent=2))"
                )
                jobs[run_name] = store.start_job(
                    action="run_pipeline",
                    label=run_name,
                    command=[sys.executable, "-c", code],
                    metadata={"phase1a_config_path": str(config_path)},
                )

            completed = {
                run_name: self._wait_for_finished(store, job["job_id"])
                for run_name, job in jobs.items()
            }
            for run_name, current in completed.items():
                self.assertEqual(current["status"], "success")
                self.assertEqual(current["phase1a_run_name"], run_name)
                self.assertEqual(current["phase1a_summary_path"], str(summaries[run_name].resolve()))
                self.assertEqual(current["phase1a_artifacts"], {"owner": run_name})

    def test_legacy_pipeline_report_without_summary_path_falls_back_to_latest_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = JobStore(root / "jobs")
            output_root = root / "phase1a-out"
            run_dir = output_root / "legacy-run"
            run_dir.mkdir(parents=True)
            summary_path = run_dir / "phase1a_summary.json"
            summary_path.write_text(
                json.dumps({"generated_at": "2026-07-18T10:00:00Z", "artifacts": {"legacy": True}}),
                encoding="utf-8",
            )
            config_path = root / "phase1a.json"
            config_path.write_text(
                json.dumps({"output_dir": str(output_root), "auto_run_dir": True}),
                encoding="utf-8",
            )
            legacy_report = {
                "ok": True,
                "data_report": {"phase1a": {"run_name": "legacy-run"}},
            }
            code = f"import json; print(json.dumps({legacy_report!r}, indent=2))"
            job = store.start_job(
                action="run_pipeline",
                label="legacy",
                command=[sys.executable, "-c", code],
                metadata={"phase1a_config_path": str(config_path)},
            )

            current = self._wait_for_finished(store, job["job_id"])

            self.assertEqual(current["phase1a_run_name"], "legacy-run")
            self.assertEqual(current["phase1a_summary_path"], str(summary_path.resolve()))

    @unittest.skipIf(os.name == "nt", "fcntl owner locking is POSIX-only")
    def test_reconcile_does_not_recover_job_while_another_process_owns_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-cross-process-owner"
            payload = {
                "job_id": job_id,
                "action": "test",
                "label": "Owned job",
                "status": "running",
                "created_at": "2026-07-18T10:00:00Z",
                "started_at": "2026-07-18T10:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": 99999999,
                "metadata": {},
            }
            store._write_job(payload)
            locker_code = (
                "import fcntl, sys; "
                "handle=open(sys.argv[1], 'a+'); "
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); sys.stdin.read(1)"
            )
            locker = subprocess.Popen(
                [sys.executable, "-c", locker_code, str(store._owner_lock_path(job_id))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(locker.stdout.readline().strip(), "locked")
                with mock.patch("webapp.jobs.process_exists", return_value=False):
                    self.assertEqual(store.get_job(job_id)["status"], "running")
                    locker.stdin.write("x")
                    locker.stdin.flush()
                    locker.wait(timeout=5)
                    recovered = store.get_job(job_id)
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(recovered["exit_code"], -2)
            finally:
                if locker.stdin:
                    locker.stdin.close()
                if locker.stdout:
                    locker.stdout.close()
                if locker.poll() is None:
                    locker.terminate()
                    locker.wait(timeout=5)

    def test_reconcile_rereads_success_written_before_owner_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(Path(tmpdir) / "jobs")
            job_id = "job-owner-finished"
            stale = {
                "job_id": job_id,
                "action": "test",
                "status": "running",
                "started_at": "2026-07-18T10:00:01Z",
                "finished_at": None,
                "exit_code": None,
                "pid": 99999999,
            }
            store._write_job(stale)

            def acquire_after_owner_finishes(_job_id, *, blocking):
                completed = dict(stale, status="success", exit_code=0, pid=None)
                store._write_job(completed)
                return object()

            with (
                mock.patch.object(store, "_acquire_owner_lock", side_effect=acquire_after_owner_finishes),
                mock.patch.object(store, "_release_owner_lock"),
                mock.patch("webapp.jobs.process_exists", return_value=False) as exists,
            ):
                current = store.get_job(job_id)

            self.assertEqual(current["status"], "success")
            self.assertEqual(current["exit_code"], 0)
            exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()
