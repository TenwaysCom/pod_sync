import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.odoo_reconcile_cache import resolve_source_delivery_types


class BurgerIngestTests(unittest.TestCase):
    def test_burger_is_not_part_of_the_odoo_index_refresh(self):
        self.assertNotIn("burger", resolve_source_delivery_types({}))


if __name__ == "__main__":
    unittest.main()
