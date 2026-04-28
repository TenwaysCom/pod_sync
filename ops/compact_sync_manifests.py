#!/usr/bin/env python3
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    load_manifest_entries,
    normalize_runtime_config,
    resolve_manifest_path,
)


SOURCE_DEFINITIONS = (
    ("onedrive", "cmc_onedrive"),
    ("gdrive", "emtc_gdrive"),
    ("westerman_api", "westerman_api"),
)


def load_sync_config(config_path):
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return normalize_runtime_config(payload, config_path)


def iter_manifest_targets(sync_config, source_filter):
    download_cfg = sync_config.get("download", {})
    for source_name, config_key in SOURCE_DEFINITIONS:
        if source_filter != "all" and source_filter != source_name:
            continue
        source_cfg = sync_config.get(config_key, {})
        if not source_cfg or not source_cfg.get("enabled", True):
            continue
        yield {
            "source_name": source_name,
            "config_key": config_key,
            "manifest_path": resolve_manifest_path(source_name, source_cfg, download_cfg),
        }


def record_identity(record):
    return (
        record.get("source"),
        record.get("warehouse"),
        record.get("source_file_id") or record.get("source_path") or record.get("local_path") or record.get("file_name"),
    )


def record_recency_key(record):
    return (
        record.get("downloaded_at") or "",
        record.get("source_modified_time") or "",
        record.get("_manifest_line") or 0,
    )


def dedupe_entries(entries):
    latest_by_identity = {}
    for entry in entries:
        identity = record_identity(entry)
        current = latest_by_identity.get(identity)
        if current is None or record_recency_key(entry) >= record_recency_key(current):
            latest_by_identity[identity] = entry
    deduped = list(latest_by_identity.values())
    deduped.sort(key=lambda row: (row.get("downloaded_at") or "", row.get("source_modified_time") or "", row.get("_manifest_line") or 0))
    return deduped


def strip_internal_keys(entry):
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def write_manifest(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(strip_internal_keys(entry), ensure_ascii=False))
            fh.write("\n")


def compact_manifest(manifest_path, backup_suffix, dry_run=False):
    entries = load_manifest_entries(manifest_path)
    deduped_entries = dedupe_entries(entries)
    duplicate_count = len(entries) - len(deduped_entries)

    summary = {
        "manifest_path": str(manifest_path),
        "records_before": len(entries),
        "records_after": len(deduped_entries),
        "duplicates_removed": duplicate_count,
        "backup_path": None,
        "status": "unchanged" if duplicate_count == 0 else "compacted",
    }

    if duplicate_count <= 0 or dry_run:
        return summary

    backup_path = manifest_path.with_name(f"{manifest_path.name}.{backup_suffix}.bak")
    shutil.copy2(manifest_path, backup_path)
    write_manifest(manifest_path, deduped_entries)
    summary["backup_path"] = str(backup_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Compact sync manifest history by keeping only the newest record per source file.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", default="all", choices=["all", "onedrive", "gdrive", "westerman_api"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    sync_config = load_sync_config(config_path)
    backup_suffix = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")

    summaries = []
    for item in iter_manifest_targets(sync_config, args.source):
        manifest_path = Path(item["manifest_path"])
        if not manifest_path.exists():
            summaries.append(
                {
                    "source": item["source_name"],
                    "manifest_path": str(manifest_path),
                    "status": "missing_manifest",
                    "records_before": 0,
                    "records_after": 0,
                    "duplicates_removed": 0,
                    "backup_path": None,
                }
            )
            continue

        summary = compact_manifest(manifest_path, backup_suffix=backup_suffix, dry_run=args.dry_run)
        summary["source"] = item["source_name"]
        summaries.append(summary)

    print(
        json.dumps(
            {
                "config_path": str(config_path),
                "dry_run": args.dry_run,
                "summaries": summaries,
                "total_duplicates_removed": sum(item["duplicates_removed"] for item in summaries),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
