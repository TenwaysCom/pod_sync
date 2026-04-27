from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .actions import (
    DEFAULT_PHASE1A_CONFIG_REL,
    DEFAULT_SYNC_CONFIG_REL,
    STATUS_SOURCE_OPTIONS,
    SYNC_MODE_OPTIONS,
    UI_DEFAULTS,
    VERIFY_SOURCE_OPTIONS,
    WESTERMAN_MODE_OPTIONS,
    build_cmc_onedrive_command,
    build_emtc_gdrive_command,
    build_phase1a_command,
    build_status_check_command,
    build_verify_integrity_command,
    build_westerman_api_command,
    load_runtime_config_snapshot,
)
from .jobs import JobStore
from .phase1a_runs import discover_phase1a_runs, get_latest_phase1a_run, get_phase1a_run, resolve_artifact_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "webapp" / "templates"
STATIC_DIR = PROJECT_ROOT / "webapp" / "static"
JOB_DIR = PROJECT_ROOT / "runtime" / "jobs"

app = FastAPI(title="POD Sync Console")
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
        "westerman_mode_options": WESTERMAN_MODE_OPTIONS,
    }
    base_context.update(context)
    return templates.TemplateResponse(request=request, name=template_name, context=base_context)


def redirect_with_error(path: str, message: str):
    return RedirectResponse(url=f"{path}?{urlencode({'error': message})}", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    latest_run = get_latest_phase1a_run()
    recent_runs = discover_phase1a_runs(limit=6)
    recent_jobs = job_store.list_jobs(limit=8)
    config_snapshot = load_runtime_config_snapshot()
    return render(
        request,
        "overview.html",
        page_id="overview",
        latest_run=latest_run,
        recent_runs=recent_runs,
        recent_jobs=recent_jobs,
        config_snapshot=config_snapshot,
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    recent_jobs = job_store.list_jobs(limit=10)
    config_snapshot = load_runtime_config_snapshot()
    return render(
        request,
        "sync.html",
        page_id="sync",
        recent_jobs=recent_jobs,
        config_snapshot=config_snapshot,
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
        raise HTTPException(status_code=404, detail="Job not found.")
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
        raise HTTPException(status_code=404, detail="Job not found.")
    return PlainTextResponse(job_store.read_log(job_id))


@app.get("/phase1a/runs/{run_name}", response_class=HTMLResponse)
async def phase1a_run_detail(request: Request, run_name: str):
    run = get_phase1a_run(run_name)
    if not run:
        raise HTTPException(status_code=404, detail="Phase 1A run not found.")
    recent_runs = discover_phase1a_runs(limit=10)
    return render(
        request,
        "phase1a_run.html",
        page_id="phase1a",
        run=run,
        recent_runs=recent_runs,
    )


@app.get("/phase1a/runs/{run_name}/artifacts/{artifact_key}")
async def phase1a_artifact(run_name: str, artifact_key: str):
    path = resolve_artifact_path(run_name, artifact_key)
    if not path:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(path)


@app.post("/actions/cmc-onedrive")
async def action_cmc_onedrive(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    limit: int = Form(20),
    recursive: bool = Form(False),
    mode: str = Form("list"),
    download_file_id: str = Form(""),
    output_dir: str = Form(""),
    workers: int = Form(1),
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
    limit: int = Form(20),
    recursive: bool = Form(False),
    mode: str = Form("list"),
    download_file_id: str = Form(""),
    output_dir: str = Form(""),
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


@app.post("/actions/phase1a")
async def action_phase1a(config_path: str = Form(DEFAULT_PHASE1A_CONFIG_REL)):
    try:
        label, command, metadata = build_phase1a_command(config_path=config_path)
    except ValueError as exc:
        return redirect_with_error("/phase1a", str(exc))
    job = job_store.start_job(action="phase1a", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)
