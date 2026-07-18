import multiprocessing
import re
import tempfile
import unittest
from pathlib import Path

from clients.common.sync_integrity import exclusive_file_lock
from webapp import odoo_push_history


def _configure_history(root_value: str):
    root = Path(root_value)
    history_dir = root / "runtime" / "odoo_push"
    odoo_push_history.PROJECT_ROOT = root
    odoo_push_history.HISTORY_DIR = history_dir
    odoo_push_history.HISTORY_FILE = history_dir / "history.json"
    return odoo_push_history


def _start_push_worker(root_value: str, barrier, worker_number: int):
    history = _configure_history(root_value)
    barrier.wait()
    push_id, log_path = history.start_push_run(
        config_path="config.local.json",
        payload_path=f"payload-{worker_number}.json",
        dry_run=False,
        limit=0,
        source_filter=None,
    )
    log_path.write_text(f"worker={worker_number}\n", encoding="utf-8")
    if not re.fullmatch(r"push-\d{8}-\d{6}-[0-9a-f]{32}", push_id):
        raise AssertionError(f"unexpected push id: {push_id}")


def _finish_push_worker(
    root_value: str,
    barrier,
    push_id: str,
    worker_number: int,
):
    history = _configure_history(root_value)
    barrier.wait()
    saved = history.finish_push_run(
        push_id,
        status="success",
        result={
            "total": 1,
            "attempted": 1,
            "completed": 1,
            "tracking_pushed": worker_number + 1,
        },
    )
    if saved is None:
        raise AssertionError(f"push record disappeared: {push_id}")


def _delete_push_worker(root_value: str, barrier, push_id: str):
    history = _configure_history(root_value)
    barrier.wait()
    ok, error = history.delete_push_run(push_id)
    if not ok:
        raise AssertionError(error or f"failed to delete {push_id}")


def _blocking_start_worker(root_value: str, ready, done):
    history = _configure_history(root_value)
    ready.set()
    history.start_push_run(
        config_path="config.local.json",
        payload_path="blocked.json",
        dry_run=True,
        limit=0,
        source_filter=None,
    )
    done.set()


class OdooPushHistoryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._original_paths = (
            odoo_push_history.PROJECT_ROOT,
            odoo_push_history.HISTORY_DIR,
            odoo_push_history.HISTORY_FILE,
        )

    def tearDown(self):
        (
            odoo_push_history.PROJECT_ROOT,
            odoo_push_history.HISTORY_DIR,
            odoo_push_history.HISTORY_FILE,
        ) = self._original_paths

    def _run_processes(self, processes):
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        for process in processes:
            self.assertFalse(process.is_alive(), "child process did not finish")
            self.assertEqual(process.exitcode, 0)

    def test_concurrent_start_finish_and_delete_preserve_all_updates(self):
        process_count = 6
        ctx = multiprocessing.get_context("spawn")

        with tempfile.TemporaryDirectory() as tmpdir:
            start_barrier = ctx.Barrier(process_count)
            start_processes = [
                ctx.Process(
                    target=_start_push_worker,
                    args=(tmpdir, start_barrier, worker_number),
                )
                for worker_number in range(process_count)
            ]
            self._run_processes(start_processes)

            history = _configure_history(tmpdir)
            started_records = history.load_history()
            push_ids = [record["push_id"] for record in started_records]
            self.assertEqual(len(push_ids), process_count)
            self.assertEqual(len(set(push_ids)), process_count)
            self.assertTrue(
                all(
                    re.fullmatch(r"push-\d{8}-\d{6}-[0-9a-f]{32}", push_id)
                    for push_id in push_ids
                )
            )

            finish_barrier = ctx.Barrier(process_count)
            finish_processes = [
                ctx.Process(
                    target=_finish_push_worker,
                    args=(tmpdir, finish_barrier, push_id, worker_number),
                )
                for worker_number, push_id in enumerate(push_ids)
            ]
            self._run_processes(finish_processes)

            finished_records = {
                record["push_id"]: record for record in history.load_history()
            }
            self.assertEqual(set(finished_records), set(push_ids))
            self.assertTrue(
                all(
                    record["status"] == "success"
                    for record in finished_records.values()
                )
            )

            delete_ids = push_ids[: process_count // 2]
            delete_barrier = ctx.Barrier(len(delete_ids))
            delete_processes = [
                ctx.Process(
                    target=_delete_push_worker,
                    args=(tmpdir, delete_barrier, push_id),
                )
                for push_id in delete_ids
            ]
            self._run_processes(delete_processes)

            remaining_ids = {
                record["push_id"] for record in history.load_history()
            }
            self.assertEqual(remaining_ids, set(push_ids) - set(delete_ids))
            for push_id in delete_ids:
                self.assertFalse((history.HISTORY_DIR / f"{push_id}.log").exists())
            self.assertFalse(
                any(
                    path.name.endswith(".tmp")
                    for path in history.HISTORY_DIR.iterdir()
                )
            )

    def test_start_waits_for_cross_process_history_lock(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            history = _configure_history(tmpdir)
            history.HISTORY_DIR.mkdir(parents=True)
            ready = ctx.Event()
            done = ctx.Event()
            process = ctx.Process(
                target=_blocking_start_worker,
                args=(tmpdir, ready, done),
            )

            with exclusive_file_lock(history._history_lock_path()):
                process.start()
                self.assertTrue(ready.wait(timeout=5))
                self.assertFalse(done.wait(timeout=0.5))

            self.assertTrue(done.wait(timeout=5))
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
