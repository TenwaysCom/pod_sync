#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    load_manifest_entries,
    normalize_runtime_config,
    resolve_manifest_path,
    verify_manifest_entry,
)


SOURCE_KEYS = ("onedrive", "gdrive", "westerman_api")
SOURCE_CONFIG_KEYS = {
    "onedrive": ("cmc_onedrive", "onedrive"),
    "gdrive": ("emtc_gdrive", "gdrive"),
    "westerman_api": ("westerman_api",),
}


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return normalize_runtime_config(json.load(fh), path)


def resolve_source_cfg(full_cfg, source):
    for key in SOURCE_CONFIG_KEYS[source]:
        if key in full_cfg:
            return full_cfg[key]
    return {}


def resolve_sources(full_cfg, selected_source):
    if selected_source != "all":
        return [selected_source]
    return [source for source in SOURCE_KEYS if resolve_source_cfg(full_cfg, source).get("enabled", True)]


def verify_manifest(manifest_path, verify_hash):
    entries = load_manifest_entries(manifest_path)
    results = [verify_manifest_entry(entry, verify_hash=verify_hash) for entry in entries]
    ok = sum(1 for item in results if item["status"] == "ok")
    failed = len(results) - ok
    return {
        "manifest_path": str(manifest_path),
        "records": len(results),
        "ok": ok,
        "failed": failed,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify local download integrity from sync manifests.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", choices=SOURCE_KEYS + ("all",), default="all")
    parser.add_argument("--manifest-path")
    parser.add_argument("--skip-sha256", action="store_true")
    args = parser.parse_args()

    full_cfg = load_config(args.config)
    download_cfg = full_cfg.get("download", {})
    verify_hash = not args.skip_sha256
    failures = 0

    if args.manifest_path:
        manifests = [{"source": args.source, "manifest_path": Path(args.manifest_path)}]
    else:
        manifests = []
        for source in resolve_sources(full_cfg, args.source):
            source_cfg = resolve_source_cfg(full_cfg, source)
            manifests.append(
                {
                    "source": source,
                    "manifest_path": resolve_manifest_path(source, source_cfg, download_cfg),
                }
            )

    summary = {
        "verify_sha256": verify_hash,
        "sources": [],
    }

    for item in manifests:
        manifest_path = item["manifest_path"]
        source_summary = {
            "source": item["source"],
            "manifest_path": str(manifest_path),
        }
        if not manifest_path.exists():
            source_summary["status"] = "missing_manifest"
            source_summary["records"] = 0
            summary["sources"].append(source_summary)
            continue

        verified = verify_manifest(manifest_path, verify_hash=verify_hash)
        source_summary.update(verified)
        source_summary["status"] = "ok" if verified["failed"] == 0 else "failed"
        failures += verified["failed"]
        summary["sources"].append(source_summary)

    summary["failed_records"] = failures
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
