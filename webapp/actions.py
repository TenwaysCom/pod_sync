from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYNC_CONFIG_REL = "config.local.json"
DEFAULT_PHASE1A_CONFIG_REL = "phase1a/examples/phase1a_from_sync.example.json"

SYNC_MODE_OPTIONS = (
    ("list", "List only"),
    ("download_first_file", "Download first file"),
    ("download_all_files", "Download all files"),
    ("download_file_id", "Download a specific file id"),
)

VERIFY_SOURCE_OPTIONS = (
    ("all", "All"),
    ("onedrive", "CMC OneDrive"),
    ("gdrive", "EMTC Google Drive"),
    ("westerman_api", "Westerman API"),
)

STATUS_SOURCE_OPTIONS = (
    ("all", "All"),
    ("onedrive", "CMC OneDrive"),
    ("gdrive", "EMTC Google Drive"),
)

WESTERMAN_MODE_OPTIONS = (
    ("lookup", "Lookup only"),
    ("download", "Lookup and download"),
)

UI_DEFAULTS = {
    "sync_config_path": DEFAULT_SYNC_CONFIG_REL,
    "phase1a_config_path": DEFAULT_PHASE1A_CONFIG_REL,
    "cmc_limit": 20,
    "emtc_limit": 20,
    "cmc_workers": 1,
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


def ensure_existing_file(path: Path, label: str):
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    return path


def validate_positive_int(value: int, field_name: str, minimum: int = 1) -> int:
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}.")
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
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "Sync config")
    resolved_output_dir = resolve_optional_project_path(output_dir)
    validate_positive_int(workers, "Workers")

    if mode not in dict(SYNC_MODE_OPTIONS):
        raise ValueError("Unknown CMC OneDrive mode.")
    if mode == "download_file_id" and not download_file_id.strip():
        raise ValueError("Download file id is required when using 'download a specific file id'.")

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
    export_mime_type: str,
    incremental: bool,
):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "Sync config")
    resolved_output_dir = resolve_optional_project_path(output_dir)

    if mode not in dict(SYNC_MODE_OPTIONS):
        raise ValueError("Unknown EMTC Google Drive mode.")
    if mode == "download_file_id" and not download_file_id.strip():
        raise ValueError("Download file id is required when using 'download a specific file id'.")

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
        "export_mime_type": export_mime_type.strip() or None,
        "incremental": incremental,
    }
    return label, command, metadata


def build_westerman_api_command(*, config_path: str, tracking_number: str, mode: str, output_dir: str):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "Sync config")
    resolved_output_dir = resolve_optional_project_path(output_dir)
    tracking = tracking_number.strip()
    if not tracking:
        raise ValueError("Tracking number is required for Westerman API actions.")
    if mode not in dict(WESTERMAN_MODE_OPTIONS):
        raise ValueError("Unknown Westerman API mode.")

    command = [
        sys.executable,
        str(PROJECT_ROOT / "clients" / "westerman_api" / "probe.py"),
        "--config",
        str(resolved_config),
        "--tracking-number",
        tracking,
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
        "tracking_number": tracking,
        "output_dir": str(resolved_output_dir) if resolved_output_dir else None,
    }
    return label, command, metadata


def build_verify_integrity_command(*, config_path: str, source: str, skip_sha256: bool):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "Sync config")
    if source not in dict(VERIFY_SOURCE_OPTIONS):
        raise ValueError("Unknown integrity verification source.")
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
    label = f"Verify Integrity / {dict(VERIFY_SOURCE_OPTIONS)[source]}"
    metadata = {
        "category": "ops",
        "action_type": "verify_integrity",
        "config_path": str(resolved_config),
        "source": source,
        "skip_sha256": skip_sha256,
    }
    return label, command, metadata


def build_status_check_command(*, config_path: str, source: str, quick: bool):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL), "Sync config")
    if source not in dict(STATUS_SOURCE_OPTIONS):
        raise ValueError("Unknown status source.")
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
    label = f"Check Status / {dict(STATUS_SOURCE_OPTIONS)[source]}"
    metadata = {
        "category": "ops",
        "action_type": "check_status",
        "config_path": str(resolved_config),
        "source": source,
        "quick": quick,
    }
    return label, command, metadata


def build_phase1a_command(*, config_path: str):
    resolved_config = ensure_existing_file(resolve_project_path(config_path, DEFAULT_PHASE1A_CONFIG_REL), "Phase 1A config")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "phase1a" / "run_phase1a.py"),
        "--config",
        str(resolved_config),
    ]
    label = "Run Phase 1A"
    metadata = {
        "category": "phase1a",
        "action_type": "phase1a",
        "phase1a_config_path": str(resolved_config),
    }
    return label, command, metadata


def load_runtime_config_snapshot(config_path: str | None = None):
    resolved_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    if not resolved_config.exists():
        return {
            "exists": False,
            "path": str(resolved_config),
            "storage_dir": str((PROJECT_ROOT / "storage" / "pod").resolve()),
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
        "storage_dir": payload.get("download", {}).get("base_dir"),
        "sources": sources,
    }
