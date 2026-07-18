import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PROJECT_ROOT / "webapp" / "templates"
CSS_PATH = PROJECT_ROOT / "webapp" / "static" / "app.css"


class WorkflowTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
        cls.overview = (TEMPLATE_DIR / "overview.html").read_text(encoding="utf-8")
        cls.sync = (TEMPLATE_DIR / "sync.html").read_text(encoding="utf-8")
        cls.phase1a = (TEMPLATE_DIR / "phase1a.html").read_text(encoding="utf-8")
        cls.odoo_push = (TEMPLATE_DIR / "odoo_push.html").read_text(encoding="utf-8")
        cls.reconcile = (TEMPLATE_DIR / "emtc_reconcile.html").read_text(encoding="utf-8")
        cls.scheduler = (TEMPLATE_DIR / "scheduler.html").read_text(encoding="utf-8")
        cls.report = (TEMPLATE_DIR / "report.html").read_text(encoding="utf-8")
        cls.css = CSS_PATH.read_text(encoding="utf-8")

    def test_primary_navigation_is_grouped_and_accessible(self):
        for label in ("工作台", "识别结果", "Odoo 推送", "总结对账", "任务中心"):
            self.assertIn(f">{label}</a>", self.base)
        for route_name in ("sync_page", "files_page", "scheduler_page", "report_page"):
            self.assertIn(f"request.url_for('{route_name}')", self.base)
        self.assertIn('class="nav-tools"', self.base)
        self.assertIn('class="nav-tools-logout"', self.base)
        self.assertNotIn("nav-critical", self.base)
        self.assertIn('aria-current="page"', self.base)
        self.assertIn('<main id="main-content" tabindex="-1">', self.base)
        self.assertIn('aria-live="polite"', self.base)
        self.assertIn("activePrimaryNav.getBoundingClientRect()", self.base)
        self.assertIn("centerActivePrimaryNav();", self.base)
        self.assertIn("window.requestAnimationFrame(centerActivePrimaryNav)", self.base)

    def test_workbench_exposes_the_main_flow_and_reconciliation(self):
        self.assertIn('class="section workflow-home"', self.overview)
        self.assertIn("进入同步工作台", self.overview)
        self.assertIn("查看总结对账", self.overview)
        self.assertIn('class="workflow-rail"', self.overview)
        self.assertIn('class="section overview-activity-grid"', self.overview)

    def test_sync_page_states_saved_scope_in_plain_language(self):
        self.assertIn("按上述流程范围启动任务", self.sync)
        self.assertIn('{{ "将执行" if enabled else "将跳过" }}', self.sync)
        self.assertIn("修改已保存流程配置", self.sync)
        self.assertNotIn('<details class="details-card sync-tool-card" open>', self.sync)

    def test_phase_result_links_to_odoo_push(self):
        self.assertIn("下一步：去 Odoo 推送", self.phase1a)
        self.assertIn("request.url_for('odoo_push_page')", self.phase1a)
        self.assertIn("data-submit-state", self.phase1a)
        self.assertIn('data-loading-text="提交中..."', self.phase1a)

    def test_odoo_push_links_to_reconciliation(self):
        self.assertIn("下一步：查看总结对账", self.odoo_push)
        self.assertIn("request.url_for('reconcile_page')", self.odoo_push)

    def test_reconciliation_refreshes_through_the_report_job(self):
        self.assertIn("刷新对账数据", self.reconcile)
        self.assertIn("request.url_for('action_refresh_report')", self.reconcile)
        self.assertNotIn("reconcile-title-critical", self.reconcile)
        self.assertIn('id="reconcileRangeForm"', self.reconcile)
        self.assertIn("开始月份不能晚于结束月份", self.reconcile)
        self.assertIn("打开标准推送页", self.reconcile)
        self.assertNotIn("前往标准推送", self.reconcile)

    def test_scheduler_blocks_run_while_form_has_unsaved_changes(self):
        self.assertIn('id="schedulerConfigForm"', self.scheduler)
        self.assertIn('id="schedulerRunButton"', self.scheduler)
        self.assertIn("hasUnsavedChanges", self.scheduler)
        self.assertIn("runButton.disabled = hasUnsavedChanges", self.scheduler)
        self.assertIn("请先保存配置，再执行流水线", self.scheduler)

    def test_scheduler_manual_run_preserves_saved_sync_options(self):
        self.assertIn('name="use_saved_scheduler_config" value="on"', self.scheduler)
        self.assertNotIn('name="cmc_limit"', self.scheduler)
        self.assertNotIn('name="emtc_limit"', self.scheduler)

    def test_detailed_report_is_a_tool_and_uses_stock_tracking_wording(self):
        self.assertIn("工具 · 数据诊断", self.report)
        self.assertIn("返回总结对账", self.report)
        self.assertIn("EMTC / CMC / Westerman", self.report)
        self.assertIn("stock.tracking.pod_link", self.report)
        self.assertNotIn("x_studio_pod_link", self.report)

    def test_css_keeps_controls_and_responsive_tables_usable(self):
        self.assertIn("--text: var(--ink);", self.css)
        self.assertIn("repeat(auto-fit, minmax(205px, 1fr))", self.css)
        self.assertIn(".overview-activity-grid", self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn(".files-table-wrap {\n    overflow-x: auto;", self.css)
        self.assertIn(
            ".jobs-summary-grid {\n    grid-template-columns: repeat(3, minmax(0, 1fr));",
            self.css,
        )
        self.assertNotIn(
            ".files-table th:nth-child(3),\n  .files-table td:nth-child(3),",
            self.css,
        )


if __name__ == "__main__":
    unittest.main()
