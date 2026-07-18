import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "webapp" / "app.py"
TEMPLATE_PATH = PROJECT_ROOT / "webapp" / "templates" / "sync.html"
SCHEDULER_TEMPLATE_PATH = PROJECT_ROOT / "webapp" / "templates" / "scheduler.html"


class WestermanWebRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = APP_PATH.read_text(encoding="utf-8")
        cls.app_tree = ast.parse(cls.app_source)
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        cls.scheduler_template = SCHEDULER_TEMPLATE_PATH.read_text(encoding="utf-8")

    def _async_function(self, name: str) -> ast.AsyncFunctionDef:
        for node in self.app_tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        self.fail(f"missing async route function: {name}")

    def test_run_pipeline_accepts_and_passes_westerman_options(self):
        function = self._async_function("action_run_pipeline")
        argument_names = {argument.arg for argument in function.args.args}
        self.assertTrue(
            {"westerman_limit", "westerman_workers", "skip_westerman"}.issubset(argument_names)
        )

        build_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_pipeline_command"
        ]
        self.assertEqual(len(build_calls), 1)
        keyword_names = {keyword.arg for keyword in build_calls[0].keywords}
        self.assertTrue(
            {"westerman_limit", "westerman_workers", "skip_westerman"}.issubset(keyword_names)
        )

    def test_scheduler_update_accepts_westerman_step_and_options(self):
        function = self._async_function("action_update_scheduler")
        argument_names = {argument.arg for argument in function.args.args}
        self.assertTrue(
            {
                "pipeline_westerman_limit",
                "pipeline_westerman_workers",
                "step_westerman_sync",
            }.issubset(argument_names)
        )
        constants = {
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue(
            {"westerman_limit", "westerman_workers", "westerman_sync"}.issubset(constants)
        )

    def test_manual_pool_action_uses_bounded_batch_command(self):
        function = self._async_function("action_westerman_pool")
        argument_names = {argument.arg for argument in function.args.args}
        self.assertTrue({"config_path", "limit", "workers"}.issubset(argument_names))
        constants = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        self.assertIn("sync.py", constants)
        self.assertIn("--limit", constants)
        self.assertIn("--workers", constants)
        self.assertIn("westerman_pool", constants)
        self.assertNotIn("api_token", constants)

    def test_scheduler_template_exposes_westerman_batch_and_workers(self):
        self.assertIn('name="step_{{ step.key }}"', self.scheduler_template)
        self.assertIn('name="pipeline_westerman_limit"', self.scheduler_template)
        self.assertIn('name="pipeline_westerman_workers"', self.scheduler_template)
        self.assertIn('name="use_saved_scheduler_config"', self.scheduler_template)

    def test_sync_template_exposes_pipeline_pool_and_atomic_controls(self):
        self.assertIn('(\"westerman_sync\", \"Westerman\")', self.template)
        self.assertIn('name="westerman_limit"', self.template)
        self.assertIn('name="westerman_workers"', self.template)
        self.assertIn("action_westerman_pool", self.template)
        self.assertIn("action_westerman_api", self.template)
        self.assertIn('name="shipment_reference"', self.template)


if __name__ == "__main__":
    unittest.main()
