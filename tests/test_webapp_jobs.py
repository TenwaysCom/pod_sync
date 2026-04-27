import tempfile
import time
import unittest
from pathlib import Path
import sys

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


if __name__ == "__main__":
    unittest.main()
