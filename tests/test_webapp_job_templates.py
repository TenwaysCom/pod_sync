import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "webapp" / "templates"


class _RequestStub:
    def __init__(self, path: str):
        self.url = SimpleNamespace(path=path)

    def url_for(self, name: str, **params):
        suffix = "/".join(str(value) for value in params.values())
        return f"/{name}" + (f"/{suffix}" if suffix else "")


class JobTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = Environment(
            loader=FileSystemLoader(TEMPLATE_ROOT),
            autoescape=select_autoescape(("html",)),
        )

    def _base_context(self, path: str):
        return {
            "request": _RequestStub(path),
            "page_id": "jobs",
            "flash": {},
            "csrf_token": "",
            "static_version": "test",
            "job_status_labels": {
                "queued": "排队中",
                "running": "运行中",
                "success": "成功",
                "failed": "失败",
            },
            "metadata_labels": {},
            "display_value": str,
        }

    def _job(self, *, status="success", report=None, job_id="job-test"):
        return {
            "job_id": job_id,
            "label": "完整流水线",
            "status": status,
            "created_at": "2026-07-18T01:00:00Z",
            "started_at": "2026-07-18T01:00:01Z",
            "finished_at": None if status in {"queued", "running"} else "2026-07-18T01:00:03Z",
            "exit_code": None if status in {"queued", "running"} else (0 if status == "success" else 1),
            "command_display": "python pipeline_runner.py --secret-command",
            "metadata": {"category": "sync"},
            "report": report,
        }

    def _render_detail(self, job):
        context = self._base_context(f"/jobs/{job['job_id']}")
        context.update(job=job, phase1a_run=None, log_text="technical-log-line")
        return self.environment.get_template("job_detail.html").render(**context)

    def _render_jobs(self, jobs):
        context = self._base_context("/jobs")
        context.update(jobs=jobs, recent_runs=[], push_runs=[])
        return self.environment.get_template("jobs.html").render(**context)

    def test_active_detail_auto_refreshes_but_completed_detail_does_not(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                active_html = self._render_detail(self._job(status=status))
                self.assertIn("本页每 5 秒自动刷新一次", active_html)
                self.assertIn("window.location.reload()", active_html)
                self.assertIn("暂停自动刷新", active_html)
                self.assertNotIn("查看总结对账", active_html)
                self.assertIn("返回任务中心", active_html)

        completed_html = self._render_detail(
            self._job(
                report={
                    "ok": True,
                    "status": "ok",
                    "elapsed_seconds": 2,
                    "data_report": {},
                    "steps": [],
                }
            )
        )
        self.assertNotIn("window.location.reload()", completed_html)
        self.assertIn("查看总结对账", completed_html)
        self.assertIn("返回任务中心", completed_html)

    def test_detail_renders_ok_partial_and_failed_as_distinct_results(self):
        cases = (
            ("ok", True, "success", "var(--success)", "成功"),
            ("partial", False, "partial", "var(--warn)", "部分完成"),
            ("failed", False, "failed", "var(--danger)", "失败"),
        )
        for report_status, ok, effective_status, color, label in cases:
            with self.subTest(report_status=report_status):
                html = self._render_detail(
                    self._job(
                        status="success" if report_status == "ok" else "failed",
                        report={
                            "ok": ok,
                            "status": report_status,
                            "elapsed_seconds": 2,
                            "failed_step": "westerman_sync" if not ok else None,
                            "data_report": {},
                            "steps": [],
                        },
                    )
                )
                self.assertIn(f'data-report-status="{report_status}"', html)
                self.assertIn(f'data-effective-status="{effective_status}"', html)
                self.assertIn(f'<h2 style="color: {color}">{label}</h2>', html)

        partial_html = self._render_detail(
            self._job(
                status="failed",
                report={
                    "ok": False,
                    "status": "partial",
                    "elapsed_seconds": 2,
                    "failed_step": "westerman_sync",
                    "data_report": {},
                    "steps": [],
                },
            )
        )
        self.assertIn("未完成步骤", partial_html)
        self.assertIn("先处理未完成步骤", partial_html)
        self.assertIn('href="#step-status">查看未完成步骤</a>', partial_html)
        self.assertNotIn('<h2 style="color: var(--danger)">失败</h2>', partial_html)

    def test_degraded_success_surfaces_report_warnings_before_reconcile(self):
        html = self._render_detail(
            self._job(
                status="success",
                report={
                    "ok": True,
                    "status": "ok",
                    "degraded": True,
                    "warnings": [
                        {"step": "refresh_odoo_missing", "error": "temporary report failure"}
                    ],
                    "elapsed_seconds": 2,
                    "data_report": {},
                    "steps": [],
                },
            )
        )

        self.assertIn('data-effective-status="degraded"', html)
        self.assertIn("成功但有警告", html)
        self.assertIn("temporary report failure", html)
        self.assertIn("先查看报告警告", html)
        self.assertIn('href="#report-warnings">查看警告</a>', html)

    def test_detail_renders_warning_value_from_pipeline_report(self):
        html = self._render_detail(
            self._job(
                status="success",
                report={
                    "ok": True,
                    "status": "ok",
                    "degraded": True,
                    "warnings": [
                        {
                            "step": "refresh_odoo_pod_index_coverage",
                            "warning": "incomplete",
                        }
                    ],
                    "elapsed_seconds": 2,
                    "data_report": {},
                    "steps": [],
                },
            )
        )

        self.assertIn("refresh_odoo_pod_index_coverage：incomplete", html)
        self.assertNotIn("refresh_odoo_pod_index_coverage：未完成", html)

    def test_detail_renders_westerman_results_and_step_label(self):
        html = self._render_detail(
            self._job(
                report={
                    "ok": True,
                    "status": "ok",
                    "elapsed_seconds": 2,
                    "data_report": {
                        "westerman_sync": {
                            "candidate_count": 71,
                            "checked_count": 12,
                            "waiting_pod_count": 8,
                            "downloaded_count": 3,
                            "retry_count": 4,
                            "failed_count": 1,
                        }
                    },
                    "steps": [
                        {"step": "westerman_sync", "ok": True, "detail": "checked=12"}
                    ],
                }
            )
        )
        self.assertIn("Westerman API 同步", html)
        for label in ("候选出库单", "本次检查", "等待 POD", "下载 POD", "待重试"):
            self.assertIn(label, html)
        for value in ("71", "12", "8", "3", "4"):
            self.assertIn(f"<h2>{value}</h2>", html)

    def test_business_summary_precedes_collapsed_technical_details(self):
        html = self._render_detail(
            self._job(
                report={
                    "ok": True,
                    "status": "ok",
                    "elapsed_seconds": 2,
                    "data_report": {},
                    "steps": [],
                }
            )
        )
        summary_position = html.index("任务执行成果摘要")
        details_position = html.index('<details id="technical-details" class="details-card details-card-block"')
        command_position = html.index("python pipeline_runner.py --secret-command")
        details_end = html.index("</details>", details_position)
        self.assertLess(summary_position, details_position)
        self.assertLess(details_position, command_position)
        self.assertLess(command_position, details_end)
        self.assertIn("技术详情：执行命令、上下文和日志", html)
        self.assertIn("technical-log-line", html)

    def test_failed_job_prioritizes_failure_details_over_reconciliation(self):
        html = self._render_detail(
            self._job(
                status="failed",
                report={
                    "ok": False,
                    "status": "failed",
                    "elapsed_seconds": 2,
                    "failed_step": "westerman_sync",
                    "data_report": {},
                    "steps": [],
                },
            )
        )
        self.assertIn("先排查失败步骤", html)
        self.assertIn('href="#technical-details">查看失败详情</a>', html)
        self.assertIn('class="details-card details-card-block" open', html)
        self.assertNotIn("查看总结对账", html)

    def test_jobs_list_prioritizes_status_and_result_action_over_command(self):
        html = self._render_jobs(
            [
                self._job(status="running", job_id="job-running"),
                self._job(
                    status="failed",
                    job_id="job-partial",
                    report={"ok": False, "status": "partial"},
                ),
            ]
        )
        self.assertNotIn("--secret-command", html)
        self.assertIn('data-effective-status="running"', html)
        self.assertIn('data-effective-status="partial"', html)
        self.assertIn("查看运行进度", html)
        self.assertIn("查看任务结果", html)
        self.assertIn("部分完成，请查看未完成步骤后再对账", html)
        self.assertIn('class="ghost-button"', html)
        self.assertIn("删除记录", html)

    def test_jobs_list_surfaces_degraded_success(self):
        html = self._render_jobs(
            [
                self._job(
                    status="success",
                    job_id="job-degraded",
                    report={"ok": True, "status": "ok", "degraded": True},
                )
            ]
        )

        self.assertIn('data-effective-status="degraded"', html)
        self.assertIn("成功但有警告", html)
        self.assertIn("报告刷新存在警告", html)


if __name__ == "__main__":
    unittest.main()
