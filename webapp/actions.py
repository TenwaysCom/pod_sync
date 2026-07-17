from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYNC_CONFIG_REL = "config.local.json"
DEFAULT_PHASE1A_CONFIG_REL = "phase1a/examples/phase1a_from_sync.example.json"

SYNC_MODE_OPTIONS = (
    ("list", "仅查看列表"),
    ("download_first_file", "下载首个文件"),
    ("download_all_files", "下载全部文件"),
    ("download_file_id", "下载指定文件 ID"),
)

VERIFY_SOURCE_OPTIONS = (
    ("all", "全部来源"),
    ("onedrive", "CMC OneDrive"),
    ("gdrive", "EMTC Google Drive"),
    ("westerman_api", "Westerman API"),
)

STATUS_SOURCE_OPTIONS = (
    ("all", "全部来源"),
    ("onedrive", "CMC OneDrive"),
    ("gdrive", "EMTC Google Drive"),
)

REMOTE_COUNT_SOURCE_OPTIONS = (
    ("all", "全部可比对来源"),
)

WESTERMAN_MODE_OPTIONS = (
    ("lookup", "仅查询"),
    ("download", "查询并下载"),
)

UI_DEFAULTS = {
    "sync_config_path": DEFAULT_SYNC_CONFIG_REL,
    "phase1a_config_path": DEFAULT_PHASE1A_CONFIG_REL,
    "sync_mode": "download_all_files",
    "cmc_limit": 100,
    "emtc_limit": 100,
    "cmc_workers": 3,
    "emtc_workers": 3,
    "cmc_incremental": True,
    "emtc_incremental": True,
    "verify_source": "all",
    "status_source": "all",
}


def resolve_project_path(raw_path: str | None, default_relative: str) -> Path:
    text = (raw_path or default_relative).strip()
    path = Path(text)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def resolve_optional_project_path(raw_path: str | None) -> Path | None:
    if not raw_path or not raw_path.strip():
        return None
    return resolve_project_path(raw_path, raw_path)


def resolve_storage_dir(config_path: str | None = None) -> Path:
    resolved_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    if not resolved_config.exists():
        return (PROJECT_ROOT / "storage" / "pod").resolve()

    payload = json.loads(resolved_config.read_text(encoding="utf-8"))
    base_dir = payload.get("download", {}).get("base_dir")
    if not base_dir:
        return (PROJECT_ROOT / "storage" / "pod").resolve()

    base_path = Path(base_dir)
    if base_path.is_absolute():
        return base_path.resolve()
    return (resolved_config.parent / base_path).resolve()


def ensure_existing_file(path: Path, label: str):
    if not path.exists():
        raise ValueError(f"{label}不存在：{path}")
    return path


def validate_positive_int(value: int, field_name: str, minimum: int = 1) -> int:
    if value < minimum:
        raise ValueError(f"{field_name}必须大于等于 {minimum}。")
    return value


def build_cmc_onedrive_command(
    *,
    config_path: str,
    limit: int,
    recursive: bool,
    mode: str,
    download_file_id: str,
    output_dir: str,
    workers: int,
    incremental: bool,
):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    resolved_output_dir = resolve_optional_project_path(output_dir)
    validate_positive_int(workers, "并发数")

    if mode not in dict(SYNC_MODE_OPTIONS):
        raise ValueError("未知的 CMC OneDrive 模式。")
    if mode == "download_file_id" and not download_file_id.strip():
        raise ValueError("使用“下载指定文件 ID”模式时，必须填写文件 ID。")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "clients" / "cmc_onedrive" / "probe.py"),
        "--config",
        str(resolved_config),
        "--limit",
        str(limit),
    ]
    if recursive:
        command.append("--recursive")
    if mode == "download_first_file":
        command.append("--download-first-file")
    elif mode == "download_all_files":
        command.append("--download-all-files")
    elif mode == "download_file_id":
        command.extend(["--download-file-id", download_file_id.strip()])
    if resolved_output_dir is not None:
        command.extend(["--output-dir", str(resolved_output_dir)])
    if workers != 1:
        command.extend(["--workers", str(workers)])
    if incremental:
        command.append("--incremental")

    label = f"CMC OneDrive / {dict(SYNC_MODE_OPTIONS)[mode]}"
    metadata = {
        "category": "sync",
        "source": "onedrive",
        "config_path": str(resolved_config),
        "mode": mode,
        "recursive": recursive,
        "limit": limit,
        "output_dir": str(resolved_output_dir) if resolved_output_dir else None,
        "workers": workers,
        "incremental": incremental,
    }
    return label, command, metadata


def build_emtc_gdrive_command(
    *,
    config_path: str,
    limit: int,
    recursive: bool,
    mode: str,
    download_file_id: str,
    output_dir: str,
    workers: int,
    export_mime_type: str,
    incremental: bool,
):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    resolved_output_dir = resolve_optional_project_path(output_dir)
    validate_positive_int(workers, "并发数")

    if mode not in dict(SYNC_MODE_OPTIONS):
        raise ValueError("未知的 EMTC Google Drive 模式。")
    if mode == "download_file_id" and not download_file_id.strip():
        raise ValueError("使用“下载指定文件 ID”模式时，必须填写文件 ID。")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "clients" / "emtc_gdrive" / "probe.py"),
        "--config",
        str(resolved_config),
        "--limit",
        str(limit),
    ]
    if recursive:
        command.append("--recursive")
    if mode == "download_first_file":
        command.append("--download-first-file")
    elif mode == "download_all_files":
        command.append("--download-all-files")
    elif mode == "download_file_id":
        command.extend(["--download-file-id", download_file_id.strip()])
    if export_mime_type.strip():
        command.extend(["--download-export-mime-type", export_mime_type.strip()])
    if resolved_output_dir is not None:
        command.extend(["--output-dir", str(resolved_output_dir)])
    if workers != 1:
        command.extend(["--workers", str(workers)])
    if incremental:
        command.append("--incremental")

    label = f"EMTC Google Drive / {dict(SYNC_MODE_OPTIONS)[mode]}"
    metadata = {
        "category": "sync",
        "source": "gdrive",
        "config_path": str(resolved_config),
        "mode": mode,
        "recursive": recursive,
        "limit": limit,
        "output_dir": str(resolved_output_dir) if resolved_output_dir else None,
        "workers": workers,
        "export_mime_type": export_mime_type.strip() or None,
        "incremental": incremental,
    }
    return label, command, metadata


def build_westerman_api_command(
    *,
    config_path: str,
    mode: str,
    output_dir: str,
    shipment_reference: str = "",
    tracking_number: str = "",
):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    resolved_output_dir = resolve_optional_project_path(output_dir)
    reference = (shipment_reference or tracking_number).strip()
    if not reference:
        raise ValueError("Westerman API 操作必须填写 shipment reference。")
    if mode not in dict(WESTERMAN_MODE_OPTIONS):
        raise ValueError("未知的 Westerman API 模式。")
    if mode == "download" and resolved_output_dir is not None:
        if ".." in Path(output_dir.strip()).parts:
            raise ValueError("Westerman 输出目录不能包含 .. 路径段。")
        storage_dir = resolve_storage_dir(str(resolved_config))
        try:
            resolved_output_dir.relative_to(storage_dir)
        except ValueError as exc:
            raise ValueError(
                "Westerman 输出目录必须位于 download.base_dir 内。"
            ) from exc

    command = [
        sys.executable,
        str(PROJECT_ROOT / "clients" / "westerman_api" / "probe.py"),
        "--config",
        str(resolved_config),
        "--shipment-reference",
        reference,
    ]
    if mode == "download":
        command.append("--download")
    if resolved_output_dir is not None:
        command.extend(["--output-dir", str(resolved_output_dir)])

    label = f"Westerman API / {dict(WESTERMAN_MODE_OPTIONS)[mode]}"
    metadata = {
        "category": "sync",
        "source": "westerman_api",
        "config_path": str(resolved_config),
        "mode": mode,
        "shipment_reference": reference,
        "output_dir": str(resolved_output_dir) if resolved_output_dir else None,
    }
    return label, command, metadata


def build_verify_integrity_command(*, config_path: str, source: str, skip_sha256: bool):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    if source not in dict(VERIFY_SOURCE_OPTIONS):
        raise ValueError("未知的完整性校验来源。")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "ops" / "verify_sync_integrity.py"),
        "--config",
        str(resolved_config),
        "--source",
        source,
    ]
    if skip_sha256:
        command.append("--skip-sha256")
    label = f"完整性校验 / {dict(VERIFY_SOURCE_OPTIONS)[source]}"
    metadata = {
        "category": "ops",
        "action_type": "verify_integrity",
        "config_path": str(resolved_config),
        "source": source,
        "skip_sha256": skip_sha256,
    }
    return label, command, metadata


def build_status_check_command(*, config_path: str, source: str, quick: bool):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    if source not in dict(STATUS_SOURCE_OPTIONS):
        raise ValueError("未知的状态检查来源。")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "ops" / "check_sync_status.py"),
        "--config",
        str(resolved_config),
        "--source",
        source,
    ]
    if quick:
        command.append("--quick")
    label = f"状态检查 / {dict(STATUS_SOURCE_OPTIONS)[source]}"
    metadata = {
        "category": "ops",
        "action_type": "check_status",
        "config_path": str(resolved_config),
        "source": source,
        "quick": quick,
    }
    return label, command, metadata


def build_phase1a_command(*, config_path: str):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_PHASE1A_CONFIG_REL), "识别配置文件")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "phase1a" / "run_phase1a.py"),
        "--config",
        str(resolved_config),
    ]
    label = "运行 POD 识别"
    metadata = {
        "category": "phase1a",
        "action_type": "phase1a",
        "phase1a_config_path": str(resolved_config),
    }
    return label, command, metadata


def build_refresh_remote_counts_command(*, config_path: str):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "ops" / "refresh_remote_counts.py"),
        "--config",
        str(resolved_config),
    ]
    label = "刷新远端数量对比"
    metadata = {
        "category": "ops",
        "action_type": "refresh_remote_counts",
        "config_path": str(resolved_config),
    }
    return label, command, metadata


def build_pipeline_command(
    *,
    config_path: str,
    cmc_incremental: bool,
    emtc_incremental: bool,
    phase1a_config_path: str,
    cmc_limit: int,
    emtc_limit: int,
    cmc_workers: int,
    emtc_workers: int,
    westerman_limit: int = 0,
    westerman_workers: int = 3,
    skip_cmc: bool = False,
    skip_emtc: bool = False,
    skip_westerman: bool = False,
    skip_phase1a: bool = False,
    skip_reports: bool = False,
    skip_odoo_push: bool = False,
    odoo_push_limit: int = 0,
):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    resolved_phase1a = ensure_existing_file(resolve_project_path(phase1a_config_path, DEFAULT_PHASE1A_CONFIG_REL), "识别配置文件")
    try:
        runtime_config = json.loads(resolved_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("同步配置文件不是有效的 JSON。") from exc
    westerman_cfg = runtime_config.get("westerman_api")
    westerman_opted_in = (
        isinstance(westerman_cfg, dict)
        and westerman_cfg.get("enabled") is True
    )
    skip_westerman = skip_westerman or not westerman_opted_in
    validate_positive_int(westerman_workers, "Westerman 并发数")
    if westerman_limit < 0:
        raise ValueError("Westerman 拉取上限必须大于等于 0。")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "webapp" / "pipeline_runner.py"),
        "--config", str(resolved_config),
        "--phase1a-config", str(resolved_phase1a),
        "--cmc-workers", str(cmc_workers),
        "--emtc-workers", str(emtc_workers),
        "--westerman-limit", str(westerman_limit),
        "--westerman-workers", str(westerman_workers),
    ]
    if not cmc_incremental:
        command.append("--full-sync")
    if cmc_limit > 0:
        command.extend(["--cmc-limit", str(cmc_limit)])
    if emtc_limit > 0:
        command.extend(["--emtc-limit", str(emtc_limit)])
    if skip_cmc:
        command.append("--skip-cmc")
    if skip_emtc:
        command.append("--skip-emtc")
    if skip_westerman:
        command.append("--skip-westerman")
    if skip_phase1a:
        command.append("--skip-phase1a")
    if skip_reports:
        command.append("--skip-reports")
    if skip_odoo_push:
        command.append("--skip-odoo-push")
    if odoo_push_limit > 0:
        command.extend(["--odoo-push-limit", str(odoo_push_limit)])

    parts = []
    if not skip_westerman:
        parts.append("Westerman")
    if not skip_cmc:
        parts.append("CMC")
    if not skip_emtc:
        parts.append("EMTC")
    if not skip_phase1a:
        parts.append("识别")
    if not skip_reports:
        parts.append("报告")
    if not skip_odoo_push:
        parts.append("推送")
    mode_label = "增量" if cmc_incremental and emtc_incremental else "全量"
    label = f"流水线 / {mode_label} / {' + '.join(parts)}" if parts else "流水线 / 无步骤"
    metadata = {
        "category": "pipeline",
        "action_type": "run_pipeline",
        "config_path": str(resolved_config),
        "phase1a_config_path": str(resolved_phase1a),
        "cmc_incremental": cmc_incremental,
        "emtc_incremental": emtc_incremental,
        "cmc_limit": cmc_limit,
        "emtc_limit": emtc_limit,
        "cmc_workers": cmc_workers,
        "emtc_workers": emtc_workers,
        "westerman_limit": westerman_limit,
        "westerman_workers": westerman_workers,
        "skip_cmc": skip_cmc,
        "skip_emtc": skip_emtc,
        "skip_westerman": skip_westerman,
        "skip_phase1a": skip_phase1a,
        "skip_reports": skip_reports,
        "skip_odoo_push": skip_odoo_push,
        "odoo_push_limit": odoo_push_limit,
    }
    return label, command, metadata


def build_odoo_push_command(*, config_path: str, payload_path: str | None = None, dry_run: bool = False, limit: int = 0, missing_only: bool = False, source_filter: str | None = None):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "同步配置文件")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "webapp" / "odoo_push.py"),
        "--config", str(resolved_config),
    ]
    if payload_path:
        command.extend(["--payload", str(payload_path)])
    if dry_run:
        command.append("--dry-run")
    if limit > 0:
        command.extend(["--limit", str(limit)])
    if missing_only:
        command.append("--missing-only")
    if source_filter:
        command.extend(["--source", source_filter])
    label = "推送 POD 链接到 Odoo" + (" (预览)" if dry_run else "")
    metadata = {
        "category": "odoo_push",
        "action_type": "odoo_push",
        "config_path": str(resolved_config),
        "payload_path": payload_path,
        "dry_run": dry_run,
        "limit": limit,
        "missing_only": missing_only,
        "source_filter": source_filter,
    }
    return label, command, metadata


def load_runtime_config_snapshot(config_path: str | None = None):
    resolved_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    if not resolved_config.exists():
        return {
            "exists": False,
            "path": str(resolved_config),
            "storage_dir": str(resolve_storage_dir(config_path)),
            "sources": [],
        }

    payload = json.loads(resolved_config.read_text(encoding="utf-8"))
    sources = []
    for key, label, secret_field in (
        ("cmc_onedrive", "CMC OneDrive", "refresh_token"),
        ("emtc_gdrive", "EMTC Google Drive", "refresh_token"),
        ("westerman_api", "Westerman API", "api_key"),
    ):
        source_cfg = payload.get(key, {})
        sources.append(
            {
                "key": key,
                "label": label,
                "enabled": bool(source_cfg.get("enabled", False)),
                "warehouse": source_cfg.get("warehouse"),
                "has_access_token": bool(source_cfg.get("access_token")),
                "has_secret": bool(source_cfg.get(secret_field)),
                "has_shared_url": bool(source_cfg.get("shared_url")),
                "has_folder_id": bool(source_cfg.get("folder_id")),
                "has_root_item_id": bool(source_cfg.get("root_item_id")),
            }
        )

    return {
        "exists": True,
        "path": str(resolved_config),
        "storage_dir": str(resolve_storage_dir(config_path)),
        "odoo_read_backend": (payload.get("odoo") or {}).get("read_backend", "querydeluxe"),
        "sources": sources,
    }


def load_westerman_pool_snapshot(config_path: str | None = None):
    """Return a secret-free, read-only summary for the sync dashboard."""

    resolved_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    default_pool_path = PROJECT_ROOT / "runtime" / "westerman" / "pod_pull_pool.sqlite3"
    if not resolved_config.exists():
        return {"exists": False, "path": str(default_pool_path), "error_code": None}
    try:
        payload = json.loads(resolved_config.read_text(encoding="utf-8"))
        raw_pool_path = (payload.get("westerman_api") or {}).get("pool_path")
        if raw_pool_path:
            candidate = Path(raw_pool_path)
            pool_path = candidate if candidate.is_absolute() else resolved_config.parent / candidate
            pool_path = pool_path.resolve()
        else:
            pool_path = default_pool_path
        if not pool_path.exists():
            return {"exists": False, "path": str(pool_path), "error_code": None}

        from clients.westerman_api.pool import WestermanPodPool

        summary = WestermanPodPool(pool_path, initialize=False).summary()
        return {
            "exists": True,
            "path": str(pool_path),
            "error_code": None,
            **summary,
        }
    except Exception:
        return {
            "exists": True,
            "path": str(default_pool_path),
            "error_code": "POOL_SUMMARY_UNAVAILABLE",
        }
