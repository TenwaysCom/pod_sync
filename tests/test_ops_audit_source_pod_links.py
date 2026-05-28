import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops import audit_source_pod_links


class FakeModels:
    def __init__(self, pod_links):
        self.pod_links = dict(pod_links)
        self.writes = []

    def execute_kw(self, db, uid, password, model, method, args, kwargs=None):
        if model != "stock.tracking":
            raise AssertionError(model)
        if method == "read":
            tracking_id = args[0][0]
            return [{"id": tracking_id, "pod_link": self.pod_links.get(tracking_id, "")}]
        if method == "write":
            tracking_id = args[0][0]
            pod_link = args[1]["pod_link"]
            self.pod_links[tracking_id] = pod_link
            self.writes.append((tracking_id, pod_link))
            return True
        raise AssertionError(method)


class AuditSourcePodLinksTests(unittest.TestCase):
    def test_count_safety_errors_abort_on_expected_count_mismatch(self):
        errors = audit_source_pod_links._count_safety_errors(
            source_link_count=1282,
            repairable_exact_count=778,
            unresolved_count=504,
            expect_source_count=1282,
            expect_repairable_count=777,
            expect_unresolved_count=504,
        )

        self.assertEqual(errors, ["repairable_exact_count expected 777, got 778"])

    def test_write_repairs_skips_when_current_source_link_changed(self):
        models = FakeModels({10: "https://drive.google.com/uc?id=changed&export=download"})
        repair = {
            "id": 10,
            "tracking_number": "TN1",
            "picking_name": "WCB2B/OUT/1",
            "source_pod_link": "https://drive.google.com/uc?id=original&export=download",
            "local_pod_link": "https://pod.eleways.online/storage/files/CMC/proof.pdf",
        }

        result = audit_source_pod_links._write_repairs(
            models,
            {"db": "db", "uid": 1, "api_key": "key"},
            [repair],
        )

        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["skipped_apply_count"], 1)
        self.assertEqual(models.writes, [])
        self.assertEqual(result["write_results"][0]["status"], "skipped_changed")


if __name__ == "__main__":
    unittest.main()
