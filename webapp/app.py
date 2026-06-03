from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

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
    build_odoo_push_command,
    build_pipeline_command,
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
    build_odoo_missing_listing,
    load_odoo_missing_snapshot,
    refresh_odoo_missing_snapshot,
    resolve_odoo_missing_artifact,
)


def _get_odoo_missing_detail_url() -> str:
    """Read QueryDeluxe detail URL from config."""
    cfg_path = PROJECT_ROOT / "config.local.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    odoo_url = cfg.get("odoo", {}).get("url", "").rstrip("/")
    query_id = cfg.get("querydeluxe", {}).get("missing_tracking_query_id", 64)
    if not odoo_url:
        raise ValueError("Odoo URL not configured in config.local.json")
    return f"{odoo_url}/api/querydeluxe/{query_id}/data"
from .annual_pod_report import build_annual_pod_workbook
from .odoo_push_history import list_push_runs, get_log_content, delete_push_run
from .odoo_pull import fetch_and_save_index, load_saved_index
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
from .scheduler import Scheduler
from .signed_urls import is_valid_file_url_secret, verify_storage_path_token


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "webapp" / "templates"
STATIC_DIR = PROJECT_ROOT / "webapp" / "static"
JOB_DIR = PROJECT_ROOT / "runtime" / "jobs"

# Auth
import hmac
import hashlib
import base64
import json as _json_lib
import time
import os
import secrets

SESSION_COOKIE = "pod_sync_session"
CSRF_COOKIE = "pod_sync_csrf"
SESSION_MAX_AGE = 86400 * 7  # 7 days
COOKIE_SECURE = os.environ.get("POD_SYNC_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}
LOGIN_RATE_LIMIT_MAX = int(os.environ.get("POD_SYNC_LOGIN_RATE_LIMIT_MAX", "5"))
LOGIN_RATE_LIMIT_WINDOW = int(os.environ.get("POD_SYNC_LOGIN_RATE_LIMIT_WINDOW", "600"))
_LOGIN_FAILURES: dict[str, list[float]] = {}

# Read admin password: env var > config.local.json > default
ADMIN_PASSWORD = os.environ.get("POD_SYNC_ADMIN_PASSWORD", "")
ENABLE_PASSWORD_LOGIN = True
AUTH_CONFIGURATION_ERROR = ""
if not ADMIN_PASSWORD:
    try:
        _cfg_path = PROJECT_ROOT / "config.local.json"
        if _cfg_path.exists():
            _cfg = _json_lib.loads(_cfg_path.read_text(encoding="utf-8"))
            ADMIN_PASSWORD = _cfg.get("admin_password", "")
            ENABLE_PASSWORD_LOGIN = _cfg.get("enable_password_login", True)
    except Exception:
        pass
if ENABLE_PASSWORD_LOGIN and not ADMIN_PASSWORD:
    AUTH_CONFIGURATION_ERROR = "POD_SYNC_ADMIN_PASSWORD or config.local.json admin_password must be configured."

SESSION_SECRET = os.environ.get("POD_SYNC_SECRET_KEY", "")
if not SESSION_SECRET:
    secret_file = PROJECT_ROOT / "runtime" / ".session_secret"
    try:
        SESSION_SECRET = secret_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    if not SESSION_SECRET:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(secret_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, os.urandom(32).hex().encode())
            os.close(fd)
        except FileExistsError:
            pass
        SESSION_SECRET = secret_file.read_text(encoding="utf-8").strip()


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def _encode_session(data: dict, secret: str) -> str:
    payload = base64.urlsafe_b64encode(_json_lib.dumps(data).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload, secret)}"


def _decode_session(cookie: str, secret: str) -> dict | None:
    try:
        payload_b64, sig = cookie.split(".", 1)
        expected = _sign(payload_b64, secret)
        if not hmac.compare_digest(sig, expected):
            return None
        data = _json_lib.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def _is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    session = _decode_session(cookie, SESSION_SECRET)
    return bool(session and session.get("role") == "admin")


def _cookie_secure(request: Request) -> bool:
    return COOKIE_SECURE or request.url.scheme == "https"


def _encode_csrf_token(token: str) -> str:
    return f"{token}.{_sign('csrf:' + token, SESSION_SECRET)}"


def _decode_csrf_cookie(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        token, sig = cookie.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign("csrf:" + token, SESSION_SECRET)):
        return None
    return token


def _csrf_token_for_response(request: Request) -> tuple[str, str | None]:
    token = _decode_csrf_cookie(request.cookies.get(CSRF_COOKIE))
    if token:
        return token, None
    token = secrets.token_urlsafe(32)
    return token, _encode_csrf_token(token)


async def _request_csrf_token(request: Request) -> str:
    header_token = request.headers.get("x-csrf-token")
    if header_token:
        return header_token
    try:
        form = await request.form()
    except Exception:
        return ""
    value = form.get("csrf_token")
    return str(value or "")


def _same_origin(request: Request) -> bool:
    host = request.headers.get("host") or request.url.netloc
    saw_origin_header = False
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        saw_origin_header = True
        parsed = urlsplit(value)
        if parsed.netloc and parsed.netloc != host:
            return False
    return saw_origin_header


async def _is_valid_csrf(request: Request) -> bool:
    cookie_token = _decode_csrf_cookie(request.cookies.get(CSRF_COOKIE))
    submitted_token = await _request_csrf_token(request)
    if cookie_token and submitted_token and hmac.compare_digest(cookie_token, submitted_token):
        return True
    return _same_origin(request)


def _get_storage_file_secret() -> str:
    env_secret = os.environ.get("POD_SYNC_FILE_URL_SECRET", "")
    if env_secret:
        return env_secret
    try:
        cfg_path = PROJECT_ROOT / "config.local.json"
        if cfg_path.exists():
            cfg = _json_lib.loads(cfg_path.read_text(encoding="utf-8"))
            return str((cfg.get("pod_sync") or {}).get("file_url_secret") or "")
    except Exception:
        return ""
    return ""


def _storage_relative_path_from_request(request: Request) -> str:
    prefix = "/storage/files/"
    path = request.url.path
    if path.startswith(prefix):
        return unquote(path[len(prefix):])
    return ""


def _has_valid_storage_signature(request: Request) -> bool:
    relative_path = _storage_relative_path_from_request(request)
    return verify_storage_path_token(relative_path, request.query_params.get("token"), _get_storage_file_secret())


def _storage_requires_signature() -> bool:
    return is_valid_file_url_secret(_get_storage_file_secret())


def _login_client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _recent_login_failures(request: Request) -> list[float]:
    now = time.time()
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW
    key = _login_client_key(request)
    failures = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if stamp >= cutoff]
    _LOGIN_FAILURES[key] = failures
    return failures


def _is_login_rate_limited(request: Request) -> bool:
    if LOGIN_RATE_LIMIT_MAX <= 0:
        return False
    return len(_recent_login_failures(request)) >= LOGIN_RATE_LIMIT_MAX


def _record_login_failure(request: Request):
    key = _login_client_key(request)
    failures = _recent_login_failures(request)
    failures.append(time.time())
    _LOGIN_FAILURES[key] = failures


def _clear_login_failures(request: Request):
    _LOGIN_FAILURES.pop(_login_client_key(request), None)


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
    "push_ready_xlsx": "POD 可推送进 Odoo Excel",
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


class TemplateRequestProxy:
    def __init__(self, request: Request):
        self._request = request

    def __getattr__(self, name):
        return getattr(self._request, name)

    def url_for(self, name: str, /, **path_params):
        # Render app-local paths instead of absolute URLs so reverse-proxy
        # scheme/header mismatches do not create mixed-content links.
        return str(self._request.app.url_path_for(name, **path_params))


app = FastAPI(title="POD 同步控制台")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_store = JobStore(JOB_DIR)
scheduler = Scheduler(job_store)
scheduler.start()


def render(request: Request, template_name: str, *, page_id: str, **context):
    csrf_token, csrf_cookie = _csrf_token_for_response(request)
    flash = {
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    }
    try:
        static_version = str(int((STATIC_DIR / "app.css").stat().st_mtime))
    except OSError:
        static_version = "1"
    base_context = {
        "request": TemplateRequestProxy(request),
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
        "csrf_token": csrf_token,
        "static_version": static_version,
    }
    base_context.update(context)
    response = templates.TemplateResponse(request=request, name=template_name, context=base_context)
    if csrf_cookie:
        response.set_cookie(
            CSRF_COOKIE,
            csrf_cookie,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
    return response


def redirect_with_error(path: str, message: str):
    return RedirectResponse(url=f"{path}?{urlencode({'error': message})}", status_code=303)


def redirect_with_notice(path: str, message: str):
    return RedirectResponse(url=f"{path}?{urlencode({'notice': message})}", status_code=303)


def normalize_redirect_target(target: str | None, fallback: str):
    if target:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            return fallback
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/storage/files" or path.startswith("/storage/files/"):
        if _is_authenticated(request) or _has_valid_storage_signature(request):
            return await call_next(request)
        return RedirectResponse(url=f"/login?{urlencode({'next': path})}", status_code=303)

    if not ENABLE_PASSWORD_LOGIN:
        return await call_next(request)

    if path == "/login" or path == "/favicon.ico" or path.startswith("/static/"):
        return await call_next(request)
    if AUTH_CONFIGURATION_ERROR:
        return PlainTextResponse(AUTH_CONFIGURATION_ERROR, status_code=503)
    if not _is_authenticated(request):
        next_target = path
        if request.url.query:
            next_target = f"{next_target}?{request.url.query}"
        return RedirectResponse(url=f"/login?{urlencode({'next': next_target})}", status_code=303)
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        if not await _is_valid_csrf(request):
            return PlainTextResponse("Invalid CSRF token.", status_code=403)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse, name="login")
async def login_page(request: Request, next: str = "/", error: str = ""):
    safe_next = normalize_redirect_target(next, "/")
    if AUTH_CONFIGURATION_ERROR:
        return PlainTextResponse(AUTH_CONFIGURATION_ERROR, status_code=503)
    if not ENABLE_PASSWORD_LOGIN:
        return RedirectResponse(url=safe_next, status_code=303)
    if _is_authenticated(request):
        return RedirectResponse(url=safe_next, status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": TemplateRequestProxy(request), "next": safe_next, "error": error},
    )


@app.post("/login", name="login_post")
async def login_post(request: Request, password: str = Form(""), next: str = Form("/")):
    safe_next = normalize_redirect_target(next, "/")
    if AUTH_CONFIGURATION_ERROR:
        return PlainTextResponse(AUTH_CONFIGURATION_ERROR, status_code=503)
    if not ENABLE_PASSWORD_LOGIN:
        return RedirectResponse(url=safe_next, status_code=303)
    if _is_login_rate_limited(request):
        params = {"next": safe_next, "error": "登录失败次数过多，请稍后再试"}
        return RedirectResponse(url=f"/login?{urlencode(params)}", status_code=303)
    if hmac.compare_digest(password, ADMIN_PASSWORD):
        _clear_login_failures(request)
        session = {"role": "admin", "exp": time.time() + SESSION_MAX_AGE}
        response = RedirectResponse(url=safe_next, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            _encode_session(session, SESSION_SECRET),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
        return response
    _record_login_failure(request)
    params = {"next": safe_next, "error": "密码错误"}
    return RedirectResponse(url=f"/login?{urlencode(params)}", status_code=303)


@app.post("/logout", name="logout")
async def logout_post():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse(url="/static/favicon.svg", status_code=307)


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
        odoo_missing_detail_url=_get_odoo_missing_detail_url(),
        odoo_missing_artifact_labels=ODOO_MISSING_ARTIFACT_LABELS,
        pod_compare_artifact_labels=POD_COMPARE_ARTIFACT_LABELS,
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    recent_jobs = job_store.list_jobs(limit=10)
    config_snapshot = load_runtime_config_snapshot()
    remote_count_snapshot = load_remote_count_snapshot()
    running_jobs = [job for job in recent_jobs if job.get("status") in {"queued", "running"}]
    enabled_sources = [source for source in config_snapshot.get("sources", []) if source.get("enabled")]
    remote_sources = remote_count_snapshot.get("sources", [])
    remote_status_counts = {
        "ok": sum(1 for source in remote_sources if source.get("status") == "ok"),
        "mismatch": sum(1 for source in remote_sources if source.get("status") == "mismatch"),
        "error": sum(1 for source in remote_sources if source.get("status") == "error"),
    }
    if not remote_count_snapshot.get("exists"):
        remote_health = {"label": "未刷新", "badge": "muted"}
    elif remote_status_counts["error"]:
        remote_health = {"label": "部分异常", "badge": "warn"}
    elif remote_status_counts["mismatch"]:
        remote_health = {"label": "有差异", "badge": "warn"}
    else:
        remote_health = {"label": "一致", "badge": "ok"}

    sync_dashboard = {
        "running_jobs": running_jobs,
        "running_count": len(running_jobs),
        "last_job": recent_jobs[0] if recent_jobs else None,
        "enabled_source_count": len(enabled_sources),
        "source_count": len(config_snapshot.get("sources", [])),
        "remote_health": remote_health,
        "remote_status_counts": remote_status_counts,
    }
    return render(
        request,
        "sync.html",
        page_id="sync",
        recent_jobs=recent_jobs,
        config_snapshot=config_snapshot,
        remote_count_snapshot=remote_count_snapshot,
        scheduler_status=scheduler.status(),
        sync_dashboard=sync_dashboard,
    )


@app.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(request: Request):
    return render(
        request,
        "scheduler.html",
        page_id="scheduler",
        scheduler_status=scheduler.status(),
    )


@app.get("/odoo-push", response_class=HTMLResponse)
async def odoo_push_page(request: Request):
    from .odoo_push_history import list_push_runs
    from .phase1a_runs import discover_phase1a_runs
    history = list_push_runs(limit=50)
    last_push = history[0] if history else None
    latest_run = discover_phase1a_runs(limit=1)
    latest_payload = None
    latest_payload_items = 0
    comparison = None
    if latest_run:
        payload_path = PROJECT_ROOT / "phase1a" / "out" / latest_run[0]["run_name"] / "odoo_backfill_payload.json"
        if payload_path.exists():
            latest_payload = str(payload_path.relative_to(PROJECT_ROOT))
            try:
                payload = _json_lib.loads(payload_path.read_text(encoding="utf-8"))
                latest_payload_items = len(payload.get("items", []))
                # Compare with saved index
                saved_index = load_saved_index()
                if saved_index:
                    from .odoo_push import _display_picking_name_for_search
                    existing_items = []
                    missing_items = []
                    for item in payload.get("items", []):
                        if item.get("identifier_status") != "ready":
                            continue
                        key_value = item.get("preferred_key", "")
                        key_type = item.get("preferred_key_type", "")
                        # For picking_name, convert canonical format back to display format
                        # so it matches the picking names stored in the Odoo index
                        lookup_tracking = key_value if key_type == "tracking_number" else None
                        lookup_picking = key_value if key_type == "picking_name" else None
                        if lookup_picking:
                            display_picking = _display_picking_name_for_search(lookup_picking)
                            if display_picking:
                                lookup_picking = display_picking
                        if saved_index.has_pod(
                            tracking_number=lookup_tracking,
                            picking_name=lookup_picking,
                        ):
                            existing_items.append(item)
                        else:
                            missing_items.append(item)
                    comparison = {
                        "total_ready": len(existing_items) + len(missing_items),
                        "existing_count": len(existing_items),
                        "missing_count": len(missing_items),
                        "index_fetched_at": saved_index.fetched_at,
                        "index_total": saved_index.total_loaded,
                    }
            except Exception:
                pass
    return render(
        request,
        "odoo_push.html",
        page_id="odoo_push",
        history=history,
        history_count=len(history),
        last_push_status=last_push.get("status") if last_push else None,
        last_push_at=last_push.get("finished_at") if last_push else None,
        latest_payload_name=latest_payload,
        latest_payload_items=latest_payload_items,
        comparison=comparison,
    )


@app.get("/odoo-push/logs/{push_id}")
async def odoo_push_log(push_id: str):
    log = get_log_content(push_id)
    if log is None:
        raise HTTPException(status_code=404, detail="未找到推送日志。")
    return PlainTextResponse(log, media_type="text/plain; charset=utf-8")


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
        odoo_missing_detail_url=_get_odoo_missing_detail_url(),
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


@app.get("/report/export/annual-pod-coverage")
async def report_export_annual_pod_coverage():
    payload = build_annual_pod_workbook()
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有可生成的年度 POD 提供率数据。")
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


@app.post("/api/reconcile/push-to-odoo")
async def api_reconcile_push_to_odoo(
    request: Request,
    match_key: str = Form(""),
    match_key_type: str = Form(""),
    file_url: str = Form(""),
    local_path: str = Form(""),
    source_name: str = Form(""),
):
    if not match_key or not match_key_type or not file_url:
        raise HTTPException(status_code=400, detail="缺少必要参数：match_key, match_key_type, file_url")

    from .odoo_push import push_single_item
    from .actions import resolve_storage_dir

    # local_path from reconcile is a relative path; resolve to absolute
    absolute_local_path = None
    if local_path:
        storage = resolve_storage_dir()
        absolute_local_path = str(storage / local_path)

    result = push_single_item(
        config_path="config.local.json",
        match_key=match_key,
        match_key_type=match_key_type,
        file_url=file_url,
        local_path=absolute_local_path,
        source_name=source_name or "unknown",
        dry_run=False,
    )
    if result.get("status") in {"pushed_tracking", "created_tracking", "skipped_same"}:
        from . import emtc_reconcile
        emtc_reconcile._load_reconcile_dataset_cached.cache_clear()
    return JSONResponse(result)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    jobs = job_store.list_jobs(limit=10)
    recent_runs = discover_phase1a_runs(limit=30)
    push_runs = list_push_runs(limit=30)
    running_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]
    return render(
        request,
        "jobs.html",
        page_id="jobs",
        jobs=jobs,
        recent_runs=recent_runs,
        push_runs=push_runs,
        running_job_count=len(running_jobs),
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


@app.post("/odoo-push/history/{push_id}/delete")
async def delete_odoo_push_history(push_id: str, redirect_to: str = Form("/jobs")):
    ok, error = delete_push_run(push_id)
    target = normalize_redirect_target(redirect_to, "/jobs")
    if not ok:
        return redirect_with_error(target, error or "删除推送记录失败。")
    return redirect_with_notice(target, f"已删除推送记录 {push_id}")


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
async def storage_file(request: Request, relative_path: str):
    if _storage_requires_signature() and not _is_authenticated(request) and not _has_valid_storage_signature(request):
        raise HTTPException(status_code=403, detail="文件访问未授权。")
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
    detail_url: str = Form(_get_odoo_missing_detail_url()),
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
    detail_url: str = Form(_get_odoo_missing_detail_url()),
):
    try:
        refresh_odoo_missing_snapshot(detail_url=detail_url)
        refresh_pod_odoo_compare_snapshot()
    except Exception as exc:
        return redirect_with_error("/report", f"统一报告刷新失败：{exc}")
    return redirect_with_notice("/report", "统一报告已刷新。")


@app.post("/api/refresh-report")
async def api_refresh_report(
    detail_url: str = Form(_get_odoo_missing_detail_url()),
):
    try:
        refresh_odoo_missing_snapshot(detail_url=detail_url)
        refresh_pod_odoo_compare_snapshot()
        return JSONResponse(
            content={"ok": True, "message": "统一报告已刷新。"}
        )
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": f"统一报告刷新失败：{exc}"},
            status_code=500,
        )


@app.post("/actions/run-pipeline")
async def action_run_pipeline(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    phase1a_config_path: str = Form(DEFAULT_PHASE1A_CONFIG_REL),
    cmc_incremental: bool = Form(False),
    emtc_incremental: bool = Form(False),
    cmc_limit: int = Form(0),
    emtc_limit: int = Form(0),
    cmc_workers: int = Form(3),
    emtc_workers: int = Form(3),
    redirect_to: str = Form("/scheduler"),
):
    safe_redirect_to = normalize_redirect_target(redirect_to, "/scheduler")
    # Read current scheduler steps config
    scheduler_cfg = scheduler.load_config()
    steps = scheduler_cfg.get("steps", {})
    try:
        label, command, metadata = build_pipeline_command(
            config_path=config_path,
            cmc_incremental=cmc_incremental,
            emtc_incremental=emtc_incremental,
            phase1a_config_path=phase1a_config_path,
            cmc_limit=cmc_limit,
            emtc_limit=emtc_limit,
            cmc_workers=cmc_workers,
            emtc_workers=emtc_workers,
            skip_cmc=not steps.get("cmc_sync", True),
            skip_emtc=not steps.get("emtc_sync", True),
            skip_phase1a=not steps.get("phase1a", True),
            skip_reports=not steps.get("refresh_reports", True),
            skip_odoo_push=not steps.get("odoo_push", False),
        )
    except ValueError as exc:
        return redirect_with_error(safe_redirect_to, str(exc))
    job = job_store.start_job(action="run_pipeline", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/update-scheduler")
async def action_update_scheduler(
    enabled: str = Form("0"),
    interval_hours: int = Form(24),
    pipeline_config_path: str = Form("config.local.json"),
    pipeline_phase1a_config_path: str = Form("phase1a/examples/phase1a_from_sync.example.json"),
    pipeline_cmc_incremental: bool = Form(False),
    pipeline_emtc_incremental: bool = Form(False),
    pipeline_cmc_limit: int = Form(0),
    pipeline_emtc_limit: int = Form(0),
    pipeline_cmc_workers: int = Form(3),
    pipeline_emtc_workers: int = Form(3),
    step_cmc_sync: bool = Form(False),
    step_emtc_sync: bool = Form(False),
    step_phase1a: bool = Form(False),
    step_refresh_reports: bool = Form(False),
    step_odoo_push: bool = Form(False),
):
    patch = {
        "enabled": enabled in ("1", "on", "true", "yes"),
        "interval_hours": max(1, min(168, interval_hours)),
        "steps": {
            "cmc_sync": step_cmc_sync,
            "emtc_sync": step_emtc_sync,
            "phase1a": step_phase1a,
            "refresh_reports": step_refresh_reports,
            "odoo_push": step_odoo_push,
        },
        "pipeline_options": {
            "config_path": pipeline_config_path,
            "phase1a_config_path": pipeline_phase1a_config_path,
            "cmc_incremental": pipeline_cmc_incremental,
            "emtc_incremental": pipeline_emtc_incremental,
            "cmc_limit": pipeline_cmc_limit,
            "emtc_limit": pipeline_emtc_limit,
            "cmc_workers": pipeline_cmc_workers,
            "emtc_workers": pipeline_emtc_workers,
        },
    }
    scheduler.update_config(patch)
    return redirect_with_notice("/scheduler", "定时任务配置已保存。")


@app.post("/actions/phase1a")
async def action_phase1a(config_path: str = Form(DEFAULT_PHASE1A_CONFIG_REL)):
    try:
        label, command, metadata = build_phase1a_command(config_path=config_path)
    except ValueError as exc:
        return redirect_with_error("/phase1a", str(exc))
    job = job_store.start_job(action="phase1a", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/actions/refresh-odoo-pod-index")
async def action_refresh_odoo_pod_index(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
):
    try:
        fetch_and_save_index(config_path)
        return redirect_with_notice("/odoo-push", "Odoo POD 索引已刷新。")
    except Exception as exc:
        return redirect_with_error("/odoo-push", f"刷新失败: {exc}")


@app.post("/api/refresh-odoo-pod-index")
async def api_refresh_odoo_pod_index(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
):
    try:
        index = fetch_and_save_index(config_path)
        return JSONResponse(
            content={
                "ok": True,
                "message": "Odoo POD 索引已刷新。",
                "index": index.summary(),
            }
        )
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": f"刷新失败: {exc}"},
            status_code=500,
        )


@app.post("/actions/odoo-push")
async def action_odoo_push(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    payload_path: str = Form(""),
    dry_run: bool = Form(False),
    limit: int = Form(0),
    missing_only: bool = Form(False),
    source_filter: str = Form(""),
):
    try:
        label, command, metadata = build_odoo_push_command(
            config_path=config_path,
            payload_path=payload_path or None,
            dry_run=dry_run,
            limit=limit,
            missing_only=missing_only,
            source_filter=source_filter or None,
        )
    except ValueError as exc:
        return redirect_with_error("/odoo-push", str(exc))
    job = job_store.start_job(action="odoo_push", label=label, command=command, metadata=metadata)
    return RedirectResponse(url=f"/jobs/{job['job_id']}", status_code=303)


@app.post("/api/odoo-push/preflight")
async def api_odoo_push_preflight(
    config_path: str = Form(DEFAULT_SYNC_CONFIG_REL),
    payload_path: str = Form(""),
    limit: int = Form(0),
    missing_only: bool = Form(False),
    source_filter: str = Form(""),
):
    from .odoo_push import preflight_payload
    try:
        result = preflight_payload(
            config_path=config_path,
            payload_path=payload_path,
            limit=limit,
            missing_only=missing_only,
            source_filter=source_filter or None,
        )
        return JSONResponse(content=result)
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": str(exc)},
            status_code=500,
        )


@app.get("/api/odoo-status")
async def api_odoo_status(config_path: str = DEFAULT_SYNC_CONFIG_REL):
    try:
        from .odoo_push import load_config, get_odoo_cfg, connect_odoo
        full_cfg = load_config(config_path)
        odoo_cfg = get_odoo_cfg(full_cfg)
        if not odoo_cfg.get("enabled", False):
            return {"ok": False, "error": "Odoo push is disabled in config"}
        missing = [k for k in ("url", "db", "uid", "api_key") if not odoo_cfg.get(k)]
        if missing:
            return {"ok": False, "error": f"Missing Odoo config keys: {missing}"}
        common, models = connect_odoo(odoo_cfg)
        version = common.version()
        # Verify DB actually exists by doing a lightweight query
        try:
            models.execute_kw(
                odoo_cfg["db"], odoo_cfg["uid"], odoo_cfg["api_key"],
                "res.users", "search_count", [[]]
            )
        except Exception as db_exc:
            err = str(db_exc)
            if "KeyError" in err or "database" in err.lower():
                return {"ok": False, "error": f"Database '{odoo_cfg['db']}' not found on server"}
            raise
        return {
            "ok": True,
            "url": odoo_cfg.get("url"),
            "db": odoo_cfg.get("db"),
            "server_version": version.get("server_version", "unknown"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
