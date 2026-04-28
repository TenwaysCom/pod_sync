#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.cmc_onedrive import probe as cmc_probe
from clients.common.sync_integrity import load_manifest_entries, normalize_runtime_config, resolve_manifest_path
from clients.emtc_gdrive import probe as gdrive_probe
from webapp.remote_counts import write_remote_count_snapshot


SOURCE_DEFS = (
    {
        "key": "cmc_onedrive",
        "label": "CMC OneDrive",
        "source_name": "onedrive",
        "supports_remote_count": True,
    },
    {
        "key": "emtc_gdrive",
        "label": "EMTC Google Drive",
        "source_name": "gdrive",
        "supports_remote_count": True,
    },
    {
        "key": "westerman_api",
        "label": "Westerman API",
        "source_name": "westerman_api",
        "supports_remote_count": False,
    },
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_runtime_config(config_path: str):
    resolved_config = Path(config_path).resolve()
    payload = json.loads(resolved_config.read_text(encoding="utf-8"))
    return resolved_config, normalize_runtime_config(payload, str(resolved_config))


def count_local_files(base_dir: Path, warehouse: str | None):
    warehouse_dir = base_dir / (warehouse or "")
    if not warehouse_dir.exists():
        return 0
    return sum(1 for path in warehouse_dir.rglob("*") if path.is_file())


def get_manifest_stats(source_name: str, source_cfg: dict, download_cfg: dict):
    manifest_path = resolve_manifest_path(source_name, source_cfg, download_cfg)
    if not manifest_path.exists():
        return {
            "manifest_path": str(manifest_path),
            "manifest_record_count": 0,
            "manifest_unique_local_count": 0,
            "manifest_missing_local_count": 0,
        }

    entries = load_manifest_entries(manifest_path)
    unique_local_paths = {entry.get("local_path") for entry in entries if entry.get("local_path")}
    missing_local_count = sum(1 for path in unique_local_paths if path and not Path(path).exists())
    return {
        "manifest_path": str(manifest_path),
        "manifest_record_count": len(entries),
        "manifest_unique_local_count": len(unique_local_paths),
        "manifest_missing_local_count": missing_local_count,
    }


def count_cmc_remote(config_path: str):
    full_cfg, cfg, _download_cfg = cmc_probe.load_config(config_path)
    timeout = int(cfg.get("timeout_seconds", 30))
    with requests.Session() as raw_session:
        session = cmc_probe.RefreshingGraphSession(raw_session, cfg, timeout, full_cfg, config_path)
        shared_access = cmc_probe.resolve_shared_access(session, cfg["shared_url"], timeout) if cfg.get("shared_url") else None
        headers = session.prepare_headers({}, force_refresh=cmc_probe.has_refresh_flow(cfg))
        if cfg.get("shared_url"):
            cmc_probe.ensure_shared_link_access(shared_access, headers)
            if cmc_probe.is_public_vroom_access(shared_access):
                headers = {}
        items = cmc_probe.iter_all_items(
            session=session,
            cfg=cfg,
            headers=headers,
            timeout=timeout,
            recursive=True,
            max_items=cmc_probe.normalize_limit(0),
            emit=False,
            shared_access=shared_access,
        )
        file_count = 0
        folder_count = 0
        for item in items:
            if item.get("is_folder"):
                folder_count += 1
            else:
                file_count += 1
    return {"remote_file_count": file_count, "remote_folder_count": folder_count}


def count_gdrive_remote(config_path: str):
    full_cfg, cfg, _download_cfg = gdrive_probe.load_config(config_path)
    timeout = int(cfg.get("timeout_seconds", 30))
    with requests.Session() as session:
        headers = gdrive_probe.get_headers(session, cfg, timeout, full_cfg=full_cfg, config_path=config_path)
        items = gdrive_probe.iter_items(
            session=session,
            cfg=cfg,
            headers=headers,
            timeout=timeout,
            recursive=True,
            max_items=gdrive_probe.normalize_limit(0),
            emit=False,
        )
        file_count = 0
        folder_count = 0
        for item in items:
            if item.get("is_folder"):
                folder_count += 1
            else:
                file_count += 1
    return {"remote_file_count": file_count, "remote_folder_count": folder_count}


def build_source_snapshot(source_def: dict, config_payload: dict, config_path: Path):
    source_cfg = config_payload.get(source_def["key"], {})
    download_cfg = config_payload.get("download", {})
    base_dir = Path(download_cfg.get("base_dir", PROJECT_ROOT / "storage" / "pod"))
    warehouse = source_cfg.get("warehouse")
    manifest_stats = get_manifest_stats(source_def["source_name"], source_cfg, download_cfg)
    local_file_count = count_local_files(base_dir, warehouse)

    snapshot = {
        "key": source_def["key"],
        "label": source_def["label"],
        "source_name": source_def["source_name"],
        "warehouse": warehouse,
        "enabled": bool(source_cfg.get("enabled", False)),
        "config_path": str(config_path),
        "local_file_count": local_file_count,
        **manifest_stats,
        "remote_file_count": None,
        "remote_folder_count": None,
        "delta_local_vs_remote": None,
        "status": "disabled" if not source_cfg.get("enabled", False) else "pending",
        "note": None,
    }

    if not snapshot["enabled"]:
        snapshot["note"] = "当前来源未启用。"
        return snapshot

    if not source_def["supports_remote_count"]:
        snapshot["status"] = "unsupported"
        snapshot["note"] = "当前来源不支持目录级远端数量比对。"
        return snapshot

    try:
        if source_def["source_name"] == "onedrive":
            remote_stats = count_cmc_remote(str(config_path))
        elif source_def["source_name"] == "gdrive":
            remote_stats = count_gdrive_remote(str(config_path))
        else:
            remote_stats = {"remote_file_count": None, "remote_folder_count": None}
        snapshot.update(remote_stats)
        if snapshot["remote_file_count"] is not None:
            snapshot["delta_local_vs_remote"] = snapshot["local_file_count"] - snapshot["remote_file_count"]
            snapshot["status"] = "ok" if snapshot["delta_local_vs_remote"] == 0 else "mismatch"
        else:
            snapshot["status"] = "unsupported"
    except Exception as exc:
        snapshot["status"] = "error"
        snapshot["note"] = str(exc)

    return snapshot


def main():
    parser = argparse.ArgumentParser(description="Refresh remote vs local source counts for the web console.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config_path, config_payload = load_runtime_config(args.config)
    snapshot = {
        "generated_at": utc_now(),
        "config_path": str(config_path),
        "sources": [build_source_snapshot(source_def, config_payload, config_path) for source_def in SOURCE_DEFS],
    }
    write_remote_count_snapshot(snapshot)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
