from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .actions import (
    DEFAULT_PHASE1A_CONFIG_REL,
    DEFAULT_SYNC_CONFIG_REL,
    REMOTE_COUNT_SOURCE_OPTIONS,
    STATUS_SOURCE_OPTIONS,
    SYNC_MODE_OPTIONS,
    UI_DEFAULTS,
    VERIFY_SOURCE_OPTIONS,
    WESTERMAN_MODE_OPTIONS,
    build_cmc_onedrive_command,
    build_emtc_gdrive_command,
    build_phase1a_command,
    build_refresh_remote_counts_command,
    build_status_check_command,
    build_verify_integrity_command,
    build_westerman_api_command,
    load_runtime_config_snapshot,
    resolve_storage_dir,
)
from .file_browser import build_storage_listing, resolve_storage_file
from .jobs import JobStore
from .odoo_missing_report import (
    DEFAULT_TOTAL_DETAIL_URL as ODOO_MISSING_DETAIL_URL,
    build_odoo_missing_listing,
    load_odoo_missing_snapshot,
    refresh_odoo_missing_snapshot,
    resolve_odoo_missing_artifact,
)
from .odoo_pod_compare import (
    build_pod_odoo_compare_listing,
    load_pod_odoo_compare_snapshot,
    refresh_pod_odoo_compare_snapshot,
    resolve_pod_odoo_compare_artifact,
)
from .emtc_reconcile import (
    build_reconcile_export_workbook,
    build_reconcile_metric_export_workbook,
    build_reconcile_odoo_export_workbook,
    build_reconcile_odoo_to_pod_listing,
    build_reconcile_pod_export_workbook,
    build_reconcile_pod_to_odoo_listing,
    list_reconcile_sources,
    load_filtered_reconcile_dataset,
)
from .phase1a_runs import (
    build_phase1a_document_listing,
    delete_phase1a_run,
    discover_phase1a_runs,
    get_latest_phase1a_run,
    get_phase1a_run,
    resolve_artifact_path,
)
from .remote_counts import load_remote_count_snapshot
from .reporting import build_report_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "webapp" / "templates"
STATIC_DIR = PROJECT_ROOT / "webapp" / "static"
JOB_DIR = PROJECT_ROOT / "runtime" / "jobs"

JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
}

VALUE_LABELS = {
    "sync": "同步",
    "ops": "运维",
    "phase1a": "POD 识别",
    "verify_integrity": "完整性校验",
    "check_status": "状态检查",
    "all": "全部来源",
    "onedrive": "CMC OneDrive",
    "gdrive": "EMTC Google Drive",
    "westerman_api": "Westerman API",
    "list": "仅查看列表",
    "download_first_file": "下载首个文件",
    "download_all_files": "下载全部文件",
    "download_file_id": "下载指定文件 ID",
    "lookup": "仅查询",
    "download": "查询并下载",
    "parse_only": "仅解析直推",
    "scope_match": "Scope 匹配",
    "refresh_remote_counts": "刷新远端数量对比",
}

METADATA_LABELS = {
    "category": "分类",
    "source": "来源",
    "config_path": "配置文件路径",
    "mode": "模式",
    "recursive": "递归遍历",
    "limit": "数量上限",
    "output_dir": "输出目录",
    "workers": "并发数",
    "incremental": "增量模式",
    "export_mime_type": "导出 MIME 类型",
    "tracking_number": "tracking number",
    "skip_sha256": "跳过 SHA256",
    "quick": "快速模式",
    "action_type": "操作类型",
    "phase1a_config_path": "识别配置文件",
}

ARTIFACT_LABELS = {
    "parsed_documents_json": "解析结果 JSON",
    "matched_report_csv": "可推送报告 CSV",
    "matched_report_json": "可推送报告 JSON",
    "unmatched_report_csv": "待处理报告 CSV",
    "unmatched_report_json": "待处理报告 JSON",
    "payload_json": "Odoo 回传 Payload",
    "summary_json": "运行摘要 JSON",
    "dashboard_json": "巡检数据 JSON",
    "dashboard_html": "巡检页面 HTML",
}

PRIMARY_ARTIFACT_KEYS = (
    "payload_json",
    "matched_report_csv",
    "unmatched_report_csv",
)

SECONDARY_ARTIFACT_KEYS = (
    "parsed_documents_json",
    "matched_report_json",
    "unmatched_report_json",
    "summary_json",
    "dashboard_json",
    "dashboard_html",
)

ODOO_MISSING_ARTIFACT_LABELS = {
    "summary_csv": "汇总 CSV",
    "total_detail_csv": "总明细 CSV",
    "missing_detail_csv": "缺失明细 CSV",
    "snapshot_json": "快照 JSON",
    "report_md": "Markdown 报告",
}

POD_COMPARE_ARTIFACT_LABELS = {
    # "summary_csv": "来源汇总 CSV",
    "matched_csv": "匹配成功 CSV",
    "matched_cancelled_csv": "匹配到已取消单 CSV",
    "matched_other_carrier_csv": "命中其他承运商 CSV",
    "not_found_csv": "按匹配键未命中 Odoo CSV",
    "ambiguous_csv": "Odoo 命中多条 CSV",
    # "snapshot_json": "快照 JSON",
    # "report_md": "Markdown 报告",
}


def display_value(value):
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value in (None, ""):
        return "无"
    if isinstance(value, str):
        return VALUE_LABELS.get(value, value)
    return value


app = FastAPI(title="POD 同步控制台")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_store = JobStore(JOB_DIR)


def render(request: Request, template_name: str, *, page_id: str, **context):
    flash = {
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    }
    base_context = {
        "request": request,
        "page_id": page_id,
        "flash": flash,
        "ui_defaults": UI_DEFAULTS,
        "sync_mode_options": SYNC_MODE_OPTIONS,
        "verify_source_options": VERIFY_SOURCE_OPTIONS,
        "status_source_options": STATUS_SOURCE_OPTIONS,
        "remote_count_source_options": REMOTE_COUNT_SOURCE_OPTIONS,
        "westerman_mode_options": WESTERMAN_MODE_OPTIONS,
        "job_status_labels": JOB_STATUS_LABELS,
        "metadata_labels": METADATA_LABELS,
        "artifact_labels": ARTIFACT_LABELS,
        "primary_artifact_keys": PRIMARY_ARTIFACT_KEYS,
        "secondary_artifact_keys": SECONDARY_ARTIFACT_KEYS,
        "display_value": display_value,
    }
    base_context.update(context)
    return templates.TemplateResponse(request=request, name=template_name, context=base_context)


def redirect_with_error(path: str, message: str):
    return RedirectResponse(url=f"{path}?{urlencode({'error': message})}", status_code=303)


def redirect_with_notice(path: str, message: str):
    return RedirectResponse(url=f"{path}?{urlencode({'notice': message})}", status_code=303)


def normalize_redirect_target(target: str | None, fallback: str):
    if target and target.startswith("/"):
        return target
    return fallback


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    latest_run = get_latest_phase1a_run()
    recent_runs = discover_phase1a_runs(limit=6)
    recent_jobs = job_store.list_jobs(limit=8)
    config_snapshot = load_runtime_config_snapshot()
    remote_count_snapshot = load_remote_count_snapshot()
    return render(
        request,
        "overview.html",
        page_id="overview",
        latest_run=latest_run,
        recent_runs=recent_runs,
        recent_jobs=recent_jobs,
        config_snapshot=config_snapshot,
        remote_count_snapshot=remote_count_snapshot,
    )


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    report_context = build_report_context()
    return render(
        request,
        "report.html",
        page_id="report",
        **report_context,
        odoo_missing_detail_url=ODOO_MISSING_DETAIL_URL,
        odoo_missing_artifact_labels=ODOO_MISSING_ARTIFACT_LABELS,
        pod_compare_artifact_labels=POD_COMPARE_ARTIFACT_LABELS,
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    recent_jobs = job_store.list_jobs(limit=10)
    config_snapshot = load_runtime_config_snapshot()
    remote_count_snapshot = load_remote_count_snapshot()
    return render(
        request,
        "sync.html",
        page_id="sync",
        recent_jobs=recent_jobs,
        config_snapshot=config_snapshot,
        remote_count_snapshot=remote_count_snapshot,
    )


@app.get("/phase1a", response_class=HTMLResponse)
async def phase1a_page(request: Request):
    latest_run = get_latest_phase1a_run()
    recent_runs = discover_phase1a_runs(limit=10)
    recent_jobs = [job for job in job_store.list_jobs(limit=20) if job.get("action") == "phase1a"][:8]
    return render(
        request,
        "phase1a.html",
        page_id="phase1a",
        latest_run=latest_run,
        recent_runs=recent_runs,
        recent_jobs=recent_jobs,
    )


@app.get("/files", response_class=HTMLResponse)
async def files_page(request: Request):
    config_snapshot = load_runtime_config_snapshot()
    return render(
        request,
        "files.html",
        page_id="files",
        config_snapshot=config_snapshot,
    )


@app.get("/odoo-missing", response_class=HTMLResponse)
async def odoo_missing_page(request: Request):
    snapshot = load_odoo_missing_snapshot()
    return render(
        request,
        "odoo_missing.html",
        page_id="odoo_missing",
        snapshot=snapshot,
        odoo_missing_detail_url=ODOO_MISSING_DETAIL_URL,
        odoo_missing_artifact_labels=ODOO_MISSING_ARTIFACT_LABELS,
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request):
    snapshot = load_pod_odoo_compare_snapshot()
    return render(
        request,
        "compare.html",
        page_id="compare",
        snapshot=snapshot,
        pod_compare_artifact_labels=POD_COMPARE_ARTIFACT_LABELS,
    )


@app.get("/reconcile", response_class=HTMLResponse)
async def reconcile_page(request: Request, kind: str = "emtc", start_month: str = ""):
    selected_source = next(
        (item for item in list_reconcile_sources() if item["slug"] == (kind or "").strip().lower()),
        list_reconcile_sources()[0],
    )
    dataset = load_filtered_reconcile_dataset(selected_source["slug"], start_month)
    return render(
        request,
        "emtc_reconcile.html",
        page_id="reconcile",
        dataset=dataset,
        selected_source=selected_source,
        reconcile_sources=list_reconcile_sources(),
        selected_start_month=(dataset or {}).get("selected_start_month", ""),
        start_month_options=(dataset or {}).get("start_month_options", []),
    )


@app.get("/emtc-reconcile")
async def emtc_reconcile_redirect():
    return RedirectResponse(url="/reconcile?kind=emtc", status_code=303)


@app.get("/reconcile/export")
async def reconcile_export(kind: str = "emtc", start_month: str = ""):
    payload = build_reconcile_export_workbook(kind, start_month)
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可导出的对账数据。")
    return Response(
        content=payload["content"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{payload["filename"]}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.get("/reconcile/export/odoo")
async def reconcile_odoo_export(kind: str = "emtc", start_month: str = ""):
    payload = build_reconcile_odoo_export_workbook(kind, start_month)
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可导出的 Odoo -> POD 明细。")
    return Response(
        content=payload["content"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{payload["filename"]}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.get("/reconcile/export/pod")
async def reconcile_pod_export(kind: str = "emtc", start_month: str = ""):
    payload = build_reconcile_pod_export_workbook(kind, start_month)
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可导出的 POD -> Odoo 明细。")
    return Response(
        content=payload["content"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{payload["filename"]}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.get("/reconcile/export/metric")
async def reconcile_metric_export(
    kind: str = "emtc",
    start_month: str = "",
    scope: str = "odoo",
    metric: str = "total",
):
    try:
        payload = build_reconcile_metric_export_workbook(
            kind,
            start_month,
            scope=scope,
            metric=metric,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可导出的对账卡片明细。")
    return Response(
        content=payload["content"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{payload["filename"]}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.get("/api/files")
async def api_files(
    source: str = "",
    q: str = "",
    include_internal: bool = False,
    page: int = 1,
    page_size: int = 25,
):
    payload = build_storage_listing(
        resolve_storage_dir(),
        source=source.strip(),
        query=q,
        include_internal=include_internal,
        page=page,
        page_size=page_size,
    )
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/odoo-missing/details")
async def api_odoo_missing_details(
    q: str = "",
    carrier: str = "",
    tracking_status: str = "missing_tracking",
    state: str = "",
    pod_link_status: str = "",
    page: int = 1,
    page_size: int = 25,
):
    payload = build_odoo_missing_listing(
        query=q,
        carrier=carrier,
        tracking_status=tracking_status,
        state=state,
        pod_link_status=pod_link_status,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有 Odoo 缺失报告快照。")
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/compare/details")
async def api_compare_details(
    q: str = "",
    source: str = "",
    status: str = "odoo_not_found",
    page: int = 1,
    page_size: int = 25,
):
    payload = build_pod_odoo_compare_listing(
        query=q,
        source=source,
        status=status,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有 POD 对比快照。")
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/reconcile/odoo-to-pod")
async def api_reconcile_odoo_to_pod(
    kind: str = "emtc",
    start_month: str = "",
    q: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 25,
):
    payload = build_reconcile_odoo_to_pod_listing(
        kind=kind,
        start_month=start_month,
        query=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可用的对账数据。")
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/reconcile/pod-to-odoo")
async def api_reconcile_pod_to_odoo(
    kind: str = "emtc",
    start_month: str = "",
    q: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 25,
):
    payload = build_reconcile_pod_to_odoo_listing(
        kind=kind,
        start_month=start_month,
        query=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可用的对账数据。")
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    jobs = job_store.list_jobs(limit=50)
    return render(
        request,
        "jobs.html",
        page_id="jobs",
        jobs=jobs,
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未找到任务。")
    log_text = job_store.read_log(job_id, max_chars=60000)
    phase1a_run = None
    if job.get("phase1a_run_name"):
        phase1a_run = get_phase1a_run(job["phase1a_run_name"])
    return render(
        request,
        "job_detail.html",
        page_id="jobs",
        job=job,
        log_text=log_text,
        phase1a_run=phase1a_run,
    )


@app.get("/jobs/{job_id}/log")
async def job_log(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未找到任务。")
    return PlainTextResponse(job_store.read_log(job_id))


@app.post("/jobs/{job_id}/delete")
async def delete_job(job_id: str, redirect_to: str = Form("/jobs")):
    ok, error = job_store.delete_job(job_id)
    target = normalize_redirect_target(redirect_to, "/jobs")
    if not ok:
        return redirect_with_error(target, error or "删除任务失败。")
    return redirect_with_notice(target, f"已删除任务 {job_id}")


@app.get("/phase1a/runs/{run_name}", response_class=HTMLResponse)
async def phase1a_run_detail(request: Request, run_name: str):
    run = get_phase1a_run(run_name)
    if not run:
        raise HTTPException(status_code=404, detail="未找到识别运行记录。")
    return render(
        request,
        "phase1a_run.html",
        page_id="phase1a",
        run=run,
    )


@app.get("/api/phase1a/runs/{run_name}/documents")
async def api_phase1a_run_documents(
    run_name: str,
    q: str = "",
    status: str = "",
    source: str = "",
    reason: str = "",
    page: int = 1,
    page_size: int = 25,
):
    payload = build_phase1a_document_listing(
        run_name,
        query=q,
        status=status,
        source=source,
        reason=reason,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="未找到识别运行记录。")
    return JSONResponse(payload, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.post("/phase1a/runs/{run_name}/delete")
async def delete_phase1a_run_action(run_name: str, redirect_to: str = Form("/phase1a")):
    ok, error = delete_phase1a_run(run_name)
    target = normalize_redirect_target(redirect_to, "/phase1a")
    if not ok:
        return redirect_with_error(target, error or "删除运行失败。")
    return redirect_with_notice(target, f"已删除运行 {run_name}")


@app.get("/phase1a/runs/{run_name}/artifacts/{artifact_key}")
async def phase1a_artifact(run_name: str, artifact_key: str):
    path = resolve_artifact_path(run_name, artifact_key)
    if not path:
        raise HTTPException(status_code=404, detail="未找到产物文件。")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/storage/files/{relative_path:path}")
async def storage_file(relative_path: str):
    path = resolve_storage_file(resolve_storage_dir(), relative_path)
    if not path:
        raise HTTPException(status_code=404, detail="未找到文件。")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/odoo-missing/artifacts/{artifact_key}")
async def odoo_missing_artifact(artifact_key: str):
    path = resolve_odoo_missing_artifact(artifact_key)
    if not path:
        raise HTTPException(status_code=404, detail="未找到 Odoo 缺失报告产物。")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/compare/artifacts/{artifact_key}")
async def compare_artifact(artifact_key: str):
    path = resolve_pod_odoo_compare_artifact(artifact_key)
    if not path:
        raise HTTPException(status_code=404, detail="未找到 POD 对比产物。")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/actions/cmc-onedrive")
async def action_cmc_onedrive(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    limit: int = Form(100),
    recursive: bool = Form(False),
    mode: str = Form("download_all_files"),
    download_file_id: str = Form(""),
    output_dir: str = Form(""),
    workers: int = Form(3),
    incremental: bool = Form(False),
):
    try:
        label, command, metadata = build_cmc_onedrive_command(
            config_path=config_path,
            limit=limit,
            recursive=recursive,
            mode=mode,
            download_file_id=download_file_id,
            output_dir=output_dir,
            workers=workers,
            incremental=incremental,
        )
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="cmc_onedrive", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/emtc-gdrive")
async def action_emtc_gdrive(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    limit: int = Form(100),
    recursive: bool = Form(False),
    mode: str = Form("download_all_files"),
    download_file_id: str = Form(""),
    output_dir: str = Form(""),
    workers: int = Form(3),
    export_mime_type: str = Form(""),
    incremental: bool = Form(False),
):
    try:
        label, command, metadata = build_emtc_gdrive_command(
            config_path=config_path,
            limit=limit,
            recursive=recursive,
            mode=mode,
            download_file_id=download_file_id,
            output_dir=output_dir,
            workers=workers,
            export_mime_type=export_mime_type,
            incremental=incremental,
        )
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="emtc_gdrive", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/westerman-api")
async def action_westerman_api(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    tracking_number: str = Form(""),
    mode: str = Form("lookup"),
    output_dir: str = Form(""),
):
    try:
        label, command, metadata = build_westerman_api_command(
            config_path=config_path,
            tracking_number=tracking_number,
            mode=mode,
            output_dir=output_dir,
        )
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="westerman_api", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/verify-integrity")
async def action_verify_integrity(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    source: str = Form("all"),
    skip_sha256: bool = Form(False),
):
    try:
        label, command, metadata = build_verify_integrity_command(
            config_path=config_path,
            source=source,
            skip_sha256=skip_sha256,
        )
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="verify_integrity", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/check-status")
async def action_check_status(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    source: str = Form("all"),
    quick: bool = Form(False),
):
    try:
        label, command, metadata = build_status_check_command(
            config_path=config_path,
            source=source,
            quick=quick,
        )
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="check_status", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/refresh-remote-counts")
async def action_refresh_remote_counts(config_path: str = Form(DEFAULT_SYNC_CONFIG_REL)):
    try:
        label, command, metadata = build_refresh_remote_counts_command(config_path=config_path)
    except ValueError as exc:
        return redirect_with_error("/sync", str(exc))
    job = job_store.start_job(action="refresh_remote_counts", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/refresh-odoo-missing")
async def action_refresh_odoo_missing(
    detail_url: str = Form(ODOO_MISSING_DETAIL_URL),
):
    try:
        refresh_odoo_missing_snapshot(detail_url=detail_url)
    except Exception as exc:
        return redirect_with_error("/odoo-missing", f"Odoo 缺失报告刷新失败：{exc}")
    return redirect_with_notice("/odoo-missing", "Odoo 缺失报告已刷新。")


@app.post("/actions/refresh-compare")
async def action_refresh_compare():
    try:
        refresh_pod_odoo_compare_snapshot()
    except Exception as exc:
        return redirect_with_error("/compare", f"POD 对比报告刷新失败：{exc}")
    return redirect_with_notice("/compare", "POD 对比报告已刷新。")


@app.post("/actions/refresh-report")
async def action_refresh_report(
    detail_url: str = Form(ODOO_MISSING_DETAIL_URL),
):
    try:
        refresh_odoo_missing_snapshot(detail_url=detail_url)
        refresh_pod_odoo_compare_snapshot()
    except Exception as exc:
        return redirect_with_error("/report", f"统一报告刷新失败：{exc}")
    return redirect_with_notice("/report", "统一报告已刷新。")


@app.post("/actions/phase1a")
async def action_phase1a(config_path: str = Form(DEFAULT_PHASE1A_CONFIG_REL)):
    try:
        label, command, metadata = build_phase1a_command(config_path=config_path)
    except ValueError as exc:
        return redirect_with_error("/phase1a", str(exc))
    job = job_store.start_job(action="phase1a", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)
