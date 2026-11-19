from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from clients.common.sync_integrity import (
    append_manifest_entry,
    build_manifest_entry,
    load_manifest_skip_index,
    resolve_manifest_path,
    utc_now,
)
from phase1a.phase1a_pod.parsing import normalize_picking_name, normalize_tracking
from phase1a.phase1a_pod.pipeline import build_payload_item_for_record
from webapp.actions import (
    DEFAULT_SYNC_CONFIG_REL,
    resolve_project_path,
    resolve_storage_dir,
)
from webapp.odoo_push import push_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BURGER_SOURCE_NAME = "Burger"
BURGER_SOURCE_TYPE = "burger_api"
BURGER_RUNTIME_DIR = PROJECT_ROOT / "runtime" / "burger_ingest"
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
DEFAULT_ALLOWED_EXTENSIONS = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".webp",
    ".bmp",
    ".gif",
)
PICKING_PREFIXES = ("WBB2BOUT", "WBB2COUT")
PUSH_SUCCESS_STATUSES = {
    "created_tracking",
    "pushed_tracking",
    "skipped_same",
    "pod_state_repaired",
    "pod_link_replaced",
}


class BurgerIngestError(Exception):
    """Reject one Burger ingest request with an explicit HTTP status."""

    def __init__(self, message: str, *, status_code: int = 400, error_code: str = "INVALID_REQUEST"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = str(error_code or "INVALID_REQUEST")


def _bounded_positive_int(value, *, default: int, maximum: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BurgerIngestError(f"{label} 必须是整数", status_code=503, error_code="INVALID_CONFIG") from exc
    if parsed <= 0 or parsed > maximum:
        raise BurgerIngestError(
            f"{label} 必须在 1..{maximum} 之间",
            status_code=503,
            error_code="INVALID_CONFIG",
        )
    return parsed


def _allowed_extensions(value) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_ALLOWED_EXTENSIONS
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise BurgerIngestError(
            "burger_ingest.allowed_extensions 必须是字符串或数组",
            status_code=503,
            error_code="INVALID_CONFIG",
        )
    normalized = []
    for item in raw_items:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = f".{text}"
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _settings(full_cfg: dict) -> dict:
    section = full_cfg.get("burger_ingest")
    if not isinstance(section, dict):
        raise BurgerIngestError(
            "config.local.json 未配置 burger_ingest，Burger POD 接口已关闭",
            status_code=503,
            error_code="SOURCE_NOT_CONFIGURED",
        )
    warehouse = str(section.get("warehouse") or BURGER_SOURCE_NAME).strip() or BURGER_SOURCE_NAME
    odoo_url = str(
        os.environ.get("POD_SYNC_BURGER_ODOO_URL") or section.get("odoo_url") or ""
    ).strip().rstrip("/")
    if odoo_url and not re.match(r"^https?://", odoo_url):
        raise BurgerIngestError(
            "burger_ingest.odoo_url 必须是 http(s) 地址",
            status_code=503,
            error_code="INVALID_CONFIG",
        )
    raw_uid = section.get("odoo_uid")
    odoo_uid = None
    if raw_uid not in (None, ""):
        try:
            odoo_uid = int(raw_uid)
        except (TypeError, ValueError) as exc:
            raise BurgerIngestError(
                "burger_ingest.odoo_uid 必须是整数",
                status_code=503,
                error_code="INVALID_CONFIG",
            ) from exc
    return {
        "enabled": section.get("enabled", True) is True,
        "warehouse": warehouse,
        "odoo_url": odoo_url,
        "odoo_overrides": {
            "url": odoo_url,
            "db": str(section.get("odoo_db") or "").strip(),
            "uid": odoo_uid,
            "api_key": str(
                os.environ.get("POD_SYNC_BURGER_ODOO_API_KEY")
                or section.get("odoo_api_key")
                or ""
            ).strip(),
        },
        "api_token": str(section.get("api_token") or "").strip(),
        "max_upload_bytes": _bounded_positive_int(
            section.get("max_upload_bytes"),
            default=DEFAULT_MAX_UPLOAD_BYTES,
            maximum=1024 * 1024 * 1024,
            label="burger_ingest.max_upload_bytes",
        ),
        "allowed_extensions": _allowed_extensions(section.get("allowed_extensions")),
        "push_immediately": section.get("push_immediately", True) is True,
    }


def _require_token(settings: dict, provided_token: str) -> None:
    expected = (os.environ.get("POD_SYNC_BURGER_TOKEN") or settings["api_token"]).strip()
    if not expected:
        raise BurgerIngestError(
            "burger_ingest.api_token 未配置（或设置 POD_SYNC_BURGER_TOKEN 环境变量）",
            status_code=503,
            error_code="TOKEN_NOT_CONFIGURED",
        )
    provided = str(provided_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise BurgerIngestError(
            "Burger X-Burger-Token 校验失败",
            status_code=401,
            error_code="INVALID_TOKEN",
        )


def _parse_picking_name(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BurgerIngestError(
            "picking_name 必填",
            status_code=400,
            error_code="PICKING_NAME_REQUIRED",
        )
    canonical = normalize_picking_name(raw)
    if not canonical or not canonical.startswith(PICKING_PREFIXES):
        raise BurgerIngestError(
            "picking_name 必须是 Burger 出库单号（WBB2B/OUT/... 或 WBB2C/OUT/...）",
            status_code=400,
            error_code="INVALID_PICKING_NAME",
        )
    return canonical


def _display_picking_name(canonical: str) -> str:
    if canonical.startswith("WBB2BOUT"):
        return f"WBB2B/OUT/{canonical[len('WBB2BOUT'):]}"
    return f"WBB2C/OUT/{canonical[len('WBB2COUT'):]}"


def _safe_file_name(value, canonical: str, mime_type: str | None) -> str:
    raw = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip().strip(".")
    if not raw:
        guess = mimetypes.guess_extension(str(mime_type or "").split(";", 1)[0].strip()) or ".pdf"
        raw = f"POD_{canonical}{guess}"
    if len(raw) > 160:
        stem, suffix = os.path.splitext(raw)
        raw = f"{stem[:120]}{suffix[:20]}"
    return raw


def _decode_base64(value, max_bytes: int) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise BurgerIngestError(
            "content_base64 必填",
            status_code=400,
            error_code="CONTENT_REQUIRED",
        )
    text = re.sub(r"\s+", "", value)
    if text.startswith("data:"):
        _, _, text = text.partition(",")
        text = re.sub(r"\s+", "", text)
    if not text:
        raise BurgerIngestError(
            "content_base64 为空",
            status_code=400,
            error_code="CONTENT_REQUIRED",
        )
    estimated_size = (len(text) * 3) // 4
    if estimated_size > max_bytes + 2:
        raise BurgerIngestError(
            f"文件超过上限 {max_bytes} 字节",
            status_code=413,
            error_code="FILE_TOO_LARGE",
        )
    try:
        content = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BurgerIngestError(
            "content_base64 不是合法的 Base64",
            status_code=400,
            error_code="INVALID_BASE64",
        ) from exc
    if not content:
        raise BurgerIngestError(
            "文件内容为空",
            status_code=400,
            error_code="EMPTY_FILE",
        )
    if len(content) > max_bytes:
        raise BurgerIngestError(
            f"文件超过上限 {max_bytes} 字节",
            status_code=413,
            error_code="FILE_TOO_LARGE",
        )
    return content


def _parse_pod_time(value, fallback: str) -> str:
    if value in (None, ""):
        return fallback
    text = str(value).strip()
    parsed = None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise BurgerIngestError(
                "pod_time 必须是 ISO8601 或 YYYY-MM-DD HH:MM:SS",
                status_code=400,
                error_code="INVALID_POD_TIME",
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _optional_tracking_number(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = normalize_tracking(text)
    if not normalized:
        raise BurgerIngestError(
            "tracking_number 不合法",
            status_code=400,
            error_code="INVALID_TRACKING_NUMBER",
        )
    return normalized


def _compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_bytes_atomic(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.part")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _existing_file_matches(path: Path, content: bytes, sha256: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != len(content):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest() == sha256
    except OSError:
        return False


def _summarize_push_result(result: dict, payload_path: Path | None) -> dict:
    details = result.get("details") if isinstance(result, dict) else None
    detail = details[0] if isinstance(details, list) and details else {}
    status = detail.get("status")
    if not status:
        status = "failed" if (isinstance(result, dict) and result.get("ok") is False) else "unknown"
    summary = {
        "status": status,
        "target_model": detail.get("target_model"),
        "target_id": detail.get("target_id"),
        "target_name": detail.get("target_name"),
        "error": detail.get("error"),
        "retryable": detail.get("retryable") is True,
        "completed": int((result or {}).get("completed") or 0),
        "business_pending": int((result or {}).get("business_pending") or 0),
        "technical_failure_count": int((result or {}).get("technical_failure_count") or 0),
    }
    if not detail and isinstance(result, dict) and result.get("error"):
        summary["error"] = str(result.get("error"))
    if payload_path is not None:
        summary["payload_path"] = str(payload_path)
    return summary


def _push_item(
    *,
    config_path: Path,
    item: dict,
    request_id: str,
    received_at: str,
    runtime_dir: Path,
    full_cfg: dict | None = None,
    odoo_overrides: dict | None = None,
) -> dict:
    payload_dir = runtime_dir / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9TZ]", "", received_at)
    payload_path = payload_dir / f"{stamp}-{request_id[:8]}.json"
    payload_path.write_text(
        json.dumps(
            {
                "job_id": f"burger-ingest-{request_id}",
                "generated_at": received_at,
                "payload_origin": "burger_ingest",
                "source": BURGER_SOURCE_NAME,
                "items": [item],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # A per-source Odoo endpoint keeps a feature-branch pilot isolated from the
    # production database without changing CMC/EMTC/Westerman routing.
    overrides = {
        key: value
        for key, value in (odoo_overrides or {}).items()
        if value not in (None, "")
    }
    push_config_path = config_path
    override_path = None
    if overrides:
        override_cfg = json.loads(json.dumps(full_cfg or {}, ensure_ascii=False))
        odoo_section = override_cfg.get("odoo")
        if not isinstance(odoo_section, dict):
            odoo_section = {}
        odoo_section.update(overrides)
        override_cfg["odoo"] = odoo_section
        override_dir = runtime_dir / "push_config"
        override_dir.mkdir(parents=True, exist_ok=True)
        override_path = override_dir / f"config-{request_id}.json"
        override_path.write_text(
            json.dumps(override_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(override_path, 0o600)
        push_config_path = override_path

    try:
        result = push_payload(
            config_path=str(push_config_path),
            payload_path=str(payload_path),
            incremental_pool=False,
        )
    except Exception as exc:  # push_payload normally returns structured failures
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "retryable": True,
            "payload_path": str(payload_path),
            "odoo_url": overrides.get("url"),
        }
    finally:
        # The override copy carries the API key; never leave it behind.
        if override_path is not None:
            try:
                override_path.unlink()
            except OSError:
                pass

    summary = _summarize_push_result(result, payload_path)
    summary["odoo_url"] = overrides.get("url")
    return summary


def _append_audit(runtime_dir: Path, record: dict) -> None:
    try:
        append_manifest_entry(runtime_dir / "ingest.jsonl", record)
    except OSError:
        # Auditing must never turn a successfully stored POD into an error.
        pass


def _business_status(push_summary: dict) -> str:
    status = str(push_summary.get("status") or "")
    if status in PUSH_SUCCESS_STATUSES:
        return "complete"
    if status == "skipped":
        # Deliberately deferred (Odoo push disabled globally or per source).
        # The POD is stored and the daily Phase 1A run will pick it up.
        return "stored"
    return "attention"


def ingest_burger_pod(
    *,
    payload: dict,
    provided_token: str,
    config_path: str = DEFAULT_SYNC_CONFIG_REL,
    runtime_dir: Path | str | None = None,
) -> dict:
    """Store one Odoo-pushed Burger POD and optionally push it to Odoo.

    Storage, manifest, parsing, and the Odoo mutation all reuse the same
    Phase 1A / ``odoo_push`` code paths as the three pull-based warehouses.
    """

    received_at = utc_now()
    request_id = uuid.uuid4().hex
    resolved_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    if not resolved_config.is_file():
        raise BurgerIngestError(
            f"配置文件不存在：{resolved_config}",
            status_code=503,
            error_code="CONFIG_NOT_FOUND",
        )
    try:
        full_cfg = json.loads(resolved_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BurgerIngestError(
            "配置文件不是有效的 JSON",
            status_code=503,
            error_code="INVALID_CONFIG",
        ) from exc

    settings = _settings(full_cfg)
    if not settings["enabled"]:
        raise BurgerIngestError(
            "burger_ingest.enabled 为 false，Burger POD 接口已关闭",
            status_code=503,
            error_code="SOURCE_DISABLED",
        )
    _require_token(settings, provided_token)

    if not isinstance(payload, dict):
        raise BurgerIngestError(
            "请求体必须是 JSON 对象",
            status_code=400,
            error_code="INVALID_BODY",
        )

    canonical_picking = _parse_picking_name(payload.get("picking_name"))
    display_picking = _display_picking_name(canonical_picking)
    content = _decode_base64(payload.get("content_base64"), settings["max_upload_bytes"])

    file_name = _safe_file_name(
        payload.get("file_name"),
        canonical_picking,
        payload.get("mime_type"),
    )
    extension = Path(file_name).suffix.lower()
    if extension not in settings["allowed_extensions"]:
        raise BurgerIngestError(
            f"不支持的文件类型：{extension or '(无扩展名)'}",
            status_code=400,
            error_code="UNSUPPORTED_FILE_TYPE",
        )

    pod_time = _parse_pod_time(payload.get("pod_time"), received_at)
    tracking_number = _optional_tracking_number(payload.get("tracking_number"))
    sha256 = _compute_sha256(content)

    storage_root = resolve_storage_dir(str(resolved_config))
    stored_name = f"{canonical_picking}_{sha256[:8]}{extension}"
    relative_path = Path(settings["warehouse"]) / canonical_picking / stored_name
    destination = storage_root / relative_path
    manifest_path = resolve_manifest_path(
        BURGER_SOURCE_TYPE,
        {"warehouse": settings["warehouse"]},
        {"base_dir": str(storage_root)},
    )
    source_file_id = f"burger:{canonical_picking}:{file_name}"

    duplicate = False
    entry = None
    skip_index = load_manifest_skip_index(manifest_path)
    previous = skip_index.get(source_file_id)
    if isinstance(previous, dict) and previous.get("sha256") == sha256:
        previous_path = Path(str(previous.get("local_path") or ""))
        if _existing_file_matches(previous_path, content, sha256):
            duplicate = True
            entry = previous
            destination = previous_path

    if entry is None:
        if not _existing_file_matches(destination, content, sha256):
            _write_bytes_atomic(destination, content)
        entry = build_manifest_entry(
            source_name=settings["warehouse"],
            source_cfg={"warehouse": settings["warehouse"]},
            destination=destination,
            write_result={"local_size": len(content), "sha256": sha256},
            source_file_id=source_file_id,
            source_path=relative_path.as_posix(),
            file_name=file_name,
            mime_type=str(payload.get("mime_type") or "").strip()
            or mimetypes.guess_type(file_name)[0],
            source_size=len(content),
            verify_remote_size=True,
            source_modified_time=pod_time,
            source_created_time=None,
            source_url=None,
            downloaded_via="burger_ingest",
            extra={
                "source_type": BURGER_SOURCE_TYPE,
                "shipment_reference": display_picking,
                "picking_name": display_picking,
                "tracking_number": tracking_number,
                "ingest_request_id": request_id,
                "ingest_mode": "odoo_push",
            },
        )
        append_manifest_entry(manifest_path, entry)

    decision, payload_item = build_payload_item_for_record(
        entry,
        source_name=settings["warehouse"],
        source_type=BURGER_SOURCE_TYPE,
    )

    if decision.get("status") != "ready":
        push_summary = {
            "status": "needs_review",
            "reason": decision.get("reason_code"),
            "error": decision.get("reason_message"),
            "retryable": False,
        }
    elif not settings["push_immediately"]:
        push_summary = {
            "status": "skipped",
            "reason": "push_immediately_disabled",
            "retryable": False,
        }
    elif (full_cfg.get("odoo") or {}).get("enabled") is not True:
        push_summary = {
            "status": "skipped",
            "reason": "odoo_push_disabled",
            "retryable": False,
        }
    else:
        push_summary = _push_item(
            config_path=resolved_config,
            item=payload_item,
            request_id=request_id,
            received_at=received_at,
            runtime_dir=Path(runtime_dir) if runtime_dir is not None else BURGER_RUNTIME_DIR,
            full_cfg=full_cfg,
            odoo_overrides=settings["odoo_overrides"],
        )

    result = {
        "ok": True,
        "accepted": True,
        "request_id": request_id,
        "received_at": received_at,
        "source": settings["warehouse"],
        "picking_name": display_picking,
        "canonical_picking_name": canonical_picking,
        "file_name": file_name,
        "stored_file_name": destination.name,
        "sha256": sha256,
        "local_path": str(destination),
        "manifest_path": str(manifest_path),
        "duplicate": duplicate,
        "stored": True,
        "tracking_number": tracking_number,
        "pod_time": pod_time,
        "push": push_summary,
        "business_status": _business_status(push_summary),
    }
    _append_audit(
        Path(runtime_dir) if runtime_dir is not None else BURGER_RUNTIME_DIR,
        {
            "received_at": received_at,
            "request_id": request_id,
            "picking_name": display_picking,
            "canonical_picking_name": canonical_picking,
            "file_name": file_name,
            "stored_file_name": destination.name,
            "sha256": sha256,
            "local_path": str(destination),
            "manifest_path": str(manifest_path),
            "duplicate": duplicate,
            "tracking_number": tracking_number,
            "pod_time": pod_time,
            "push_status": push_summary.get("status"),
            "push_target_id": push_summary.get("target_id"),
            "push_odoo_url": push_summary.get("odoo_url"),
            "push_error": push_summary.get("error"),
        },
    )
    return result
