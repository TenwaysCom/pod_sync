#!/usr/bin/env python3
"""Audit and optionally repair Odoo stock.tracking POD source links.

The repair only rewrites source OneDrive/Google Drive URLs that can be matched
exactly to a locally downloaded POD file from the sync manifests or Phase 1A
payloads. Unmatched source links are reported, not guessed.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.odoo_push import connect_odoo, get_local_file_url, get_odoo_cfg, load_config


SOURCE_MARKERS = (
    "onedrive.live.com",
    "1drv.ms",
    "drive.google.com",
    "googleusercontent.com",
)

SOURCE_DOMAIN = [
    "|",
    "|",
    "|",
    ("pod_link", "ilike", "onedrive.live.com"),
    ("pod_link", "ilike", "1drv.ms"),
    ("pod_link", "ilike", "drive.google.com"),
    ("pod_link", "ilike", "googleusercontent.com"),
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_timestamp(value: str) -> str:
    return value.replace("-", "").replace(":", "").replace("T", "_").rstrip("Z")


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _source_kind(url: str) -> str:
    if "onedrive.live.com" in url or "1drv.ms" in url:
        return "onedrive_source"
    if "drive.google.com" in url or "googleusercontent.com" in url:
        return "gdrive_source"
    return "other_source"


def _default_local_storage_prefix(full_cfg: dict) -> str:
    base_url = full_cfg.get("pod_sync", {}).get("base_url", "").rstrip("/")
    return f"{base_url}/storage/files/" if base_url else ""


def _load_source_url_map(full_cfg: dict) -> dict[str, str]:
    """Map remote source URL to pod_sync local URL."""
    mapping: dict[str, str] = {}

    for manifest_path in sorted((PROJECT_ROOT / "storage" / "pod" / "_integrity").glob("**/*.jsonl*")):
        with manifest_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                source_url = item.get("source_url") or ""
                local_url = get_local_file_url(full_cfg, item.get("local_path") or "")
                if source_url and local_url:
                    mapping.setdefault(source_url, local_url)

    for payload_path in sorted((PROJECT_ROOT / "phase1a" / "out").glob("*/odoo_backfill_payload.json")):
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        for item in payload.get("items", []):
            source_url = item.get("file_url") or ""
            local_url = get_local_file_url(full_cfg, item.get("local_path") or "")
            if source_url and local_url:
                mapping.setdefault(source_url, local_url)

    return mapping


def _fetch_source_link_records(models, odoo_cfg: dict) -> list[dict]:
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    ids = models.execute_kw(
        db,
        uid,
        password,
        "stock.tracking",
        "search",
        [SOURCE_DOMAIN],
        {"order": "id ASC"},
    )
    records: list[dict] = []
    fields = [
        "id",
        "tracking_number",
        "picking_id",
        "pod_link",
        "pod_state",
        "api_state",
        "pod_time",
        "write_date",
        "create_date",
    ]
    for start in range(0, len(ids), 1000):
        batch = ids[start : start + 1000]
        records.extend(
            models.execute_kw(
                db,
                uid,
                password,
                "stock.tracking",
                "read",
                [batch],
                {"fields": fields},
            )
        )
    return records


def _read_tracking_pod_link(models, odoo_cfg: dict, tracking_id: int) -> str:
    records = models.execute_kw(
        odoo_cfg["db"],
        odoo_cfg["uid"],
        odoo_cfg["api_key"],
        "stock.tracking",
        "read",
        [[tracking_id]],
        {"fields": ["pod_link"]},
    )
    if not records:
        return ""
    return records[0].get("pod_link") or ""


def _write_repairs(models, odoo_cfg: dict, repairs: list[dict], limit: int = 0, verify_writes: bool = True) -> dict:
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    result = {
        "applied_count": 0,
        "skipped_apply_count": 0,
        "failed_apply_count": 0,
        "write_results": [],
    }
    for item in repairs[: limit or None]:
        row = {
            "id": item["id"],
            "tracking_number": item.get("tracking_number") or "",
            "picking_name": item.get("picking_name") or "",
            "source_pod_link": item["source_pod_link"],
            "local_pod_link": item["local_pod_link"],
        }
        try:
            current = _read_tracking_pod_link(models, odoo_cfg, item["id"])
            row["current_before"] = current
            if current != item["source_pod_link"]:
                row["status"] = "skipped_changed"
                row["error"] = "current pod_link no longer matches audited source link"
                result["skipped_apply_count"] += 1
                result["write_results"].append(row)
                continue

            ok = models.execute_kw(
                db,
                uid,
                password,
                "stock.tracking",
                "write",
                [[item["id"]], {"pod_link": item["local_pod_link"]}],
            )
            row["write_return"] = bool(ok)
            if not ok:
                row["status"] = "failed_write"
                row["error"] = "Odoo write returned false"
                result["failed_apply_count"] += 1
                result["write_results"].append(row)
                continue

            if verify_writes:
                current_after = _read_tracking_pod_link(models, odoo_cfg, item["id"])
                row["current_after"] = current_after
                if current_after != item["local_pod_link"]:
                    row["status"] = "failed_verify"
                    row["error"] = "post-write pod_link does not match local POD link"
                    result["failed_apply_count"] += 1
                    result["write_results"].append(row)
                    continue

            row["status"] = "applied"
            result["applied_count"] += 1
        except Exception as exc:
            row["status"] = "failed_exception"
            row["error"] = str(exc)
            result["failed_apply_count"] += 1
        result["write_results"].append(row)
    return result


def _count_safety_errors(
    *,
    source_link_count: int,
    repairable_exact_count: int,
    unresolved_count: int,
    expect_source_count: int | None,
    expect_repairable_count: int | None,
    expect_unresolved_count: int | None,
) -> list[str]:
    errors = []
    checks = [
        ("source_link_count", source_link_count, expect_source_count),
        ("repairable_exact_count", repairable_exact_count, expect_repairable_count),
        ("unresolved_count", unresolved_count, expect_unresolved_count),
    ]
    for label, actual, expected in checks:
        if expected is not None and actual != expected:
            errors.append(f"{label} expected {expected}, got {actual}")
    return errors


def _target_safety_errors(repairs: list[dict], required_local_prefix: str) -> list[str]:
    errors = []
    for item in repairs:
        item_id = item.get("id")
        source_url = item.get("source_pod_link") or ""
        local_url = item.get("local_pod_link") or ""
        if not item_id:
            errors.append("repair row is missing stock.tracking id")
        if not any(marker in source_url for marker in SOURCE_MARKERS):
            errors.append(f"repair {item_id} source URL is not a known source POD link")
        if not required_local_prefix or not local_url.startswith(required_local_prefix):
            errors.append(f"repair {item_id} local URL does not start with {required_local_prefix!r}")
        if len(errors) >= 20:
            break
    return errors


def audit_source_pod_links(
    *,
    config_path: str,
    apply: bool = False,
    limit: int = 0,
    sample_size: int = 10,
    expect_source_count: int | None = None,
    expect_repairable_count: int | None = None,
    expect_unresolved_count: int | None = None,
    required_local_prefix: str | None = None,
    verify_writes: bool = True,
    backup_dir: str | None = None,
    include_records: bool = False,
) -> dict:
    full_cfg = load_config(config_path)
    odoo_cfg = get_odoo_cfg(full_cfg)
    if not odoo_cfg.get("enabled", False):
        return {"ok": False, "error": "Odoo push is disabled in config"}

    required = ("url", "db", "uid", "api_key")
    missing = [key for key in required if not odoo_cfg.get(key)]
    if missing:
        return {"ok": False, "error": f"Missing Odoo config keys: {missing}"}

    _, models = connect_odoo(odoo_cfg)
    source_url_map = _load_source_url_map(full_cfg)
    records = _fetch_source_link_records(models, odoo_cfg)

    repairs = []
    unresolved = []
    by_kind = Counter()
    by_write_date = Counter()
    for record in records:
        pod_link = record.get("pod_link") or ""
        if not any(marker in pod_link for marker in SOURCE_MARKERS):
            continue
        by_kind[_source_kind(pod_link)] += 1
        write_date = str(record.get("write_date") or "")[:10] or "-"
        by_write_date[write_date] += 1
        local_url = source_url_map.get(pod_link)
        picking = record.get("picking_id")
        picking_name = picking[1] if isinstance(picking, list) and len(picking) > 1 else ""
        row = {
            "id": record.get("id"),
            "tracking_number": record.get("tracking_number") or "",
            "picking_name": picking_name,
            "source_pod_link": pod_link,
            "write_date": record.get("write_date") or "",
        }
        if local_url:
            repairs.append({**row, "local_pod_link": local_url})
        else:
            unresolved.append(row)

    required_local_prefix = required_local_prefix if required_local_prefix is not None else _default_local_storage_prefix(full_cfg)
    safety_errors = []
    safety_errors.extend(
        _count_safety_errors(
            source_link_count=len(records),
            repairable_exact_count=len(repairs),
            unresolved_count=len(unresolved),
            expect_source_count=expect_source_count,
            expect_repairable_count=expect_repairable_count,
            expect_unresolved_count=expect_unresolved_count,
        )
    )
    safety_errors.extend(_target_safety_errors(repairs, required_local_prefix))

    result = {
        "ok": True,
        "generated_at": _utc_now(),
        "apply": apply,
        "apply_limit": limit,
        "source_link_count": len(records),
        "repairable_exact_count": len(repairs),
        "unresolved_count": len(unresolved),
        "applied_count": 0,
        "skipped_apply_count": 0,
        "failed_apply_count": 0,
        "source_url_map_count": len(source_url_map),
        "required_local_prefix": required_local_prefix,
        "by_kind": dict(sorted(by_kind.items())),
        "by_write_date": dict(sorted(by_write_date.items())),
        "repairable_sample": repairs[:sample_size],
        "unresolved_sample": unresolved[:sample_size],
    }
    if include_records:
        result["repairable"] = repairs
        result["unresolved"] = unresolved

    if safety_errors:
        result["ok"] = False
        result["error"] = "safety checks failed"
        result["safety_errors"] = safety_errors
        return result

    if apply:
        if backup_dir:
            backup_payload = dict(result)
            backup_payload["repairable"] = repairs
            backup_payload["unresolved"] = unresolved
            backup_path = Path(backup_dir) / f"{_safe_timestamp(result['generated_at'])}_pre_apply_plan.json"
            result["backup_path"] = _write_json(backup_path, backup_payload)

        write_result = _write_repairs(models, odoo_cfg, repairs, limit=limit, verify_writes=verify_writes)
        result["applied_count"] = write_result["applied_count"]
        result["skipped_apply_count"] = write_result["skipped_apply_count"]
        result["failed_apply_count"] = write_result["failed_apply_count"]
        result["write_sample"] = write_result["write_results"][:sample_size]
        if include_records:
            result["write_results"] = write_result["write_results"]
        if write_result["failed_apply_count"]:
            result["ok"] = False
            result["error"] = "one or more writes failed"

    return result


def main():
    parser = argparse.ArgumentParser(description="Audit or repair source POD links in Odoo stock.tracking")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--apply", action="store_true", help="Write repairable local POD links back to Odoo")
    parser.add_argument("--limit", type=int, default=0, help="Maximum repair writes when using --apply")
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--expect-source-count", type=int, default=None)
    parser.add_argument("--expect-repairable-count", type=int, default=None)
    parser.add_argument("--expect-unresolved-count", type=int, default=None)
    parser.add_argument("--require-local-prefix", default=None)
    parser.add_argument("--no-verify-writes", action="store_true")
    parser.add_argument("--backup-dir", default="runtime/odoo_push/source_link_repair")
    parser.add_argument("--include-records", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    result = audit_source_pod_links(
        config_path=args.config,
        apply=args.apply,
        limit=args.limit,
        sample_size=args.sample_size,
        expect_source_count=args.expect_source_count,
        expect_repairable_count=args.expect_repairable_count,
        expect_unresolved_count=args.expect_unresolved_count,
        required_local_prefix=args.require_local_prefix,
        verify_writes=not args.no_verify_writes,
        backup_dir=args.backup_dir,
        include_records=args.include_records,
    )
    if args.output:
        result["output_path"] = _write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
