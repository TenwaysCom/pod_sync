#!/usr/bin/env python3
"""Check sync status: compare remote cloud storage file counts/sizes against local downloads.

Usage examples:
    # Quick count comparison (no hash verification)
    python ops/check_sync_status.py --config config.json --quick

    # Full check including SHA256 verification of existing local files
    python ops/check_sync_status.py --config config.json

    # Check only Google Drive
    python ops/check_sync_status.py --config config.json --source gdrive --quick

    # Cron-friendly: exits 1 if files are missing or integrity failures exist
    python ops/check_sync_status.py --config config.json --quick || echo "ALERT: sync issues detected"
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    load_manifest_entries,
    normalize_runtime_config,
    resolve_manifest_path,
    verify_manifest_entry,
)

# Re-use authentication and traversal from existing probes
from clients.emtc_gdrive.probe import (
    load_config as gdrive_load_config,
    get_headers as gdrive_get_headers,
    iter_items as gdrive_iter_items,
)
from clients.cmc_onedrive.probe import (
    load_config as onedrive_load_config,
    iter_all_items as onedrive_iter_all_items,
    RefreshingGraphSession,
    has_refresh_flow,
    resolve_shared_access,
    is_public_vroom_access,
    ensure_shared_link_access,
)


SOURCE_CONFIG_KEYS = {
    "onedrive": ("cmc_onedrive", "onedrive"),
    "gdrive": ("emtc_gdrive", "gdrive"),
}


def resolve_source_cfg(full_cfg, source_name):
    for key in SOURCE_CONFIG_KEYS[source_name]:
        if key in full_cfg:
            return full_cfg[key]
    return {}


def count_remote_gdrive(config_path):
    full_cfg, cfg, _download_cfg = gdrive_load_config(config_path)
    timeout = int(cfg.get("timeout_seconds", 30))
    count = 0
    total_size = 0
    with requests.Session() as session:
        headers = gdrive_get_headers(session, cfg, timeout, full_cfg=full_cfg, config_path=config_path)
        for item in gdrive_iter_items(
            session, cfg, headers, timeout, recursive=True, max_items=None, emit=False
        ):
            if not item.get("is_folder"):
                count += 1
                size = item.get("size")
                if size:
                    total_size += int(size)
    return {"count": count, "total_size": total_size}


def count_remote_onedrive(config_path):
    full_cfg, cfg, _download_cfg = onedrive_load_config(config_path)
    timeout = int(cfg.get("timeout_seconds", 30))
    count = 0
    total_size = 0
    with requests.Session() as raw_session:
        session = RefreshingGraphSession(raw_session, cfg, timeout, full_cfg, config_path)
        shared_access = (
            resolve_shared_access(session, cfg["shared_url"], timeout)
            if cfg.get("shared_url")
            else None
        )
        headers = session.prepare_headers({}, force_refresh=has_refresh_flow(cfg))
        if cfg.get("shared_url"):
            ensure_shared_link_access(shared_access, headers)
            if is_public_vroom_access(shared_access):
                headers = {}
        for item in onedrive_iter_all_items(
            session,
            cfg,
            headers,
            timeout,
            recursive=True,
            max_items=None,
            emit=False,
            shared_access=shared_access,
        ):
            if not item.get("is_folder"):
                count += 1
                size = item.get("size")
                if size:
                    total_size += int(size)
    return {"count": count, "total_size": total_size}


def count_local_files(directory):
    path = Path(directory)
    if not path.exists():
        return {"count": 0, "total_size": 0}
    count = 0
    total_size = 0
    for f in path.rglob("*"):
        if f.is_file():
            count += 1
            total_size += f.stat().st_size
    return {"count": count, "total_size": total_size}


def check_manifest(manifest_path, verify_hash=True):
    if not manifest_path.exists():
        return {
            "exists": False,
            "records": 0,
            "ok": 0,
            "failed": 0,
            "failed_samples": [],
        }
    entries = load_manifest_entries(manifest_path)
    results = [verify_manifest_entry(entry, verify_hash=verify_hash) for entry in entries]
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = len(results) - ok
    failed_samples = [
        {
            "file_name": r["file_name"],
            "local_path": r["local_path"],
            "checks": r["checks"],
            "actual_size": r["actual_size"],
        }
        for r in results
        if r["status"] != "ok"
    ][:10]
    return {
        "exists": True,
        "records": len(results),
        "ok": ok,
        "failed": failed,
        "failed_samples": failed_samples,
    }


def check_source(source_name, full_cfg, download_cfg, config_path, verify_hash=True):
    source_cfg = resolve_source_cfg(full_cfg, source_name)
    if not source_cfg.get("enabled", False):
        return {"source": source_name, "enabled": False}

    # Remote stats
    try:
        if source_name == "gdrive":
            remote = count_remote_gdrive(config_path)
        elif source_name == "onedrive":
            remote = count_remote_onedrive(config_path)
        else:
            remote = {"count": 0, "total_size": 0}
    except Exception as exc:
        remote = {"count": 0, "total_size": 0, "error": str(exc)}

    # Local stats
    base_dir = Path(download_cfg.get("base_dir", "."))
    warehouse = source_cfg.get("warehouse") or source_name
    local_dir = base_dir / warehouse
    local = count_local_files(local_dir)

    # Manifest stats
    manifest_path = resolve_manifest_path(source_name, source_cfg, download_cfg)
    manifest = check_manifest(manifest_path, verify_hash=verify_hash)

    remote_count = remote.get("count", 0)
    local_count = local["count"]
    diff = remote_count - local_count

    result = {
        "source": source_name,
        "enabled": True,
        "remote_files": remote_count,
        "remote_total_size": remote.get("total_size", 0),
        "local_files": local_count,
        "local_total_size": local["total_size"],
        "manifest_records": manifest["records"],
        "manifest_ok": manifest["ok"],
        "manifest_failed": manifest["failed"],
        "missing_local_files": diff if diff > 0 else 0,
        "extra_local_files": (-diff) if diff < 0 else 0,
        "manifest_exists": manifest["exists"],
    }

    if "error" in remote:
        result["remote_error"] = remote["error"]

    if manifest["failed"] > 0:
        result["manifest_failed_samples"] = manifest["failed_samples"]

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Compare remote cloud storage against local downloads."
    )
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument(
        "--source",
        choices=["onedrive", "gdrive", "all"],
        default="all",
        help="Which source to check (default: all enabled)",
    )
    parser.add_argument(
        "--skip-sha256",
        action="store_true",
        help="Skip SHA256 verification for faster execution",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Alias for --skip-sha256; only counts and sizes, no hash checks",
    )
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        full_cfg = normalize_runtime_config(json.load(fh), args.config)
    download_cfg = full_cfg.get("download", {})

    if args.source == "all":
        sources = [
            s
            for s in ("onedrive", "gdrive")
            if resolve_source_cfg(full_cfg, s).get("enabled", False)
        ]
    else:
        sources = [args.source]

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "config": args.config,
        "sources": [],
    }

    has_issues = False
    verify_hash = not (args.skip_sha256 or args.quick)

    for source in sources:
        result = check_source(source, full_cfg, download_cfg, args.config, verify_hash=verify_hash)
        report["sources"].append(result)
        if result.get("enabled"):
            if result.get("missing_local_files", 0) > 0:
                has_issues = True
            if result.get("manifest_failed", 0) > 0:
                has_issues = True
            if "remote_error" in result:
                has_issues = True

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if has_issues:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
