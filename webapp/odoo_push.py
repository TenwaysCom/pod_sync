#!/usr/bin/env python3
"""Push POD links from Phase 1A payload back to Odoo via XML-RPC.

Correct target model: stock.tracking (from tw_stock_tracking module)
- Primary: update stock.tracking via tracking_number match
- Fallback: update stock.picking.x_studio_pod_link directly

Usage:
    python webapp/odoo_push.py --config config.local.json --payload phase1a/out/.../odoo_backfill_payload.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.actions import resolve_project_path


def load_config(config_path: str) -> dict:
    resolved = resolve_project_path(config_path, "config.local.json")
    return json.loads(resolved.read_text(encoding="utf-8"))


def get_odoo_cfg(full_cfg: dict) -> dict:
    return full_cfg.get("odoo", {})


def get_local_file_url(full_cfg: dict, local_path: str) -> str | None:
    """Build pod_sync local file URL from local_path.

    e.g. local_path=/home/rick/pod_sync/storage/pod/CMC/2025 POD/file.pdf
         base_dir=/home/rick/pod_sync/storage/pod
         base_url=http://host:port
    result: http://host:port/storage/files/CMC/2025%20POD/file.pdf
    """
    pod_sync_cfg = full_cfg.get("pod_sync", {})
    base_url = pod_sync_cfg.get("base_url", "").rstrip("/")
    if not base_url:
        return None

    download_cfg = full_cfg.get("download", {})
    base_dir = download_cfg.get("base_dir", "")
    if not base_dir:
        return None

    try:
        local = Path(local_path).resolve()
        base = Path(base_dir).resolve()
        rel = local.relative_to(base)
        # URL-encode path segments
        from urllib.parse import quote
        encoded = "/".join(quote(str(part), safe="") for part in rel.parts)
        return f"{base_url}/storage/files/{encoded}"
    except (ValueError, TypeError):
        return None


def connect_odoo(odoo_cfg: dict):
    url = odoo_cfg.get("url", "").rstrip("/")
    if not url:
        raise ValueError("Odoo URL not configured")
    common_proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    models_proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return common_proxy, models_proxy


def _utc_now_str() -> str:
    """Return current UTC time in Odoo Datetime format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_to_odoo_datetime(iso_str: str | None) -> str | None:
    """Convert ISO datetime (e.g. 2025-12-17T08:59:37Z) to Odoo Datetime format."""
    if not iso_str:
        return None
    try:
        # Parse ISO format and output as YYYY-MM-DD HH:MM:SS (UTC)
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def search_stock_tracking(models, odoo_cfg: dict, tracking_number: str):
    """Search stock.tracking by tracking_number. Returns list of records."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]

    res = models.execute_kw(
        db, uid, password,
        "stock.tracking", "web_search_read",
        [],
        {
            "domain": [("tracking_number", "=", tracking_number)],
            "specification": {
                "id": {},
                "tracking_number": {},
                "picking_id": {},
                "pod_link": {},
                "pod_state": {},
                "api_state": {},
            },
            "limit": 5,
            "order": "id DESC",
            "context": {"lang": "en_US", "tz": "Asia/Shanghai"},
        },
    )
    records = res.get("records", []) if isinstance(res, dict) else []
    return records


def search_picking(models, odoo_cfg: dict, key_type: str, key_value: str):
    """Search stock.picking by picking_name or tracking_number."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    model = odoo_cfg.get("push_model", "stock.picking")

    if key_type == "picking_name":
        field = odoo_cfg.get("picking_name_field", "name")
    else:
        field = odoo_cfg.get("tracking_number_field", "carrier_tracking_ref")

    res = models.execute_kw(
        db, uid, password,
        model, "web_search_read",
        [],
        {
            "domain": [(field, "=", key_value)],
            "specification": {"id": {}, "name": {}, field: {}, "x_studio_pod_link": {}},
            "limit": 5,
            "order": "id DESC",
            "context": {"lang": "en_US", "tz": "Asia/Shanghai"},
        },
    )
    records = res.get("records", []) if isinstance(res, dict) else []
    return records


def write_tracking_pod(models, odoo_cfg: dict, tracking_id: int, file_url: str, pod_time: str | None):
    """Update stock.tracking with POD info."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    vals = {
        "pod_link": file_url,
        "pod_state": "pod",
        "api_state": "completed",
    }
    if pod_time:
        vals["pod_time"] = pod_time
    return models.execute_kw(db, uid, password, "stock.tracking", "write", [[tracking_id], vals])


def write_picking_pod_field(models, odoo_cfg: dict, picking_id: int, file_url: str):
    """Fallback: write x_studio_pod_link directly on stock.picking."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    field = odoo_cfg.get("push_field", "x_studio_pod_link")
    return models.execute_kw(db, uid, password, "stock.picking", "write", [[picking_id], {field: file_url}])


def create_stock_tracking(models, odoo_cfg: dict, picking_id: int, tracking_number: str, file_url: str, pod_time: str | None):
    """Create a new stock.tracking record when none exists."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]
    vals = {
        "picking_id": picking_id,
        "tracking_number": tracking_number,
        "pod_link": file_url,
        "pod_state": "pod",
        "api_state": "completed",
    }
    if pod_time:
        vals["pod_time"] = pod_time
    return models.execute_kw(db, uid, password, "stock.tracking", "create", [vals])


def push_payload(
    *,
    config_path: str,
    payload_path: str,
    dry_run: bool = False,
    limit: int = 0,
    source_filter: str | None = None,
    skip_existing: bool = True,
    missing_only: bool = False,
):
    full_cfg = load_config(config_path)
    odoo_cfg = get_odoo_cfg(full_cfg)
    if not odoo_cfg.get("enabled", False):
        return {"ok": False, "error": "Odoo push is disabled in config"}

    required_keys = ("url", "db", "uid", "api_key")
    missing = [k for k in required_keys if not odoo_cfg.get(k)]
    if missing:
        return {"ok": False, "error": f"Missing Odoo config keys: {missing}"}

    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    items = [item for item in payload.get("items", []) if item.get("identifier_status") == "ready"]
    if source_filter:
        items = [item for item in items if item.get("source_name") == source_filter]

    # Pre-filter to missing-only if requested
    if missing_only:
        try:
            try:
                from .odoo_pull import get_index
            except ImportError:
                from webapp.odoo_pull import get_index
            existing_index = get_index(config_path)
            items = [
                item for item in items
                if not existing_index.has_pod(
                    tracking_number=item.get("preferred_key") if item.get("preferred_key_type") == "tracking_number" else None,
                    picking_name=item.get("preferred_key") if item.get("preferred_key_type") == "picking_name" else None,
                )
            ]
            print(f"[MISSING_ONLY] Filtered to {len(items)} missing records")
        except Exception as exc:
            print(f"[MISSING_ONLY] Warning: failed to filter: {exc}")

    if limit > 0:
        items = items[:limit]

    # Load existing POD index for deduplication
    existing_index = None
    skipped_existing = 0
    if skip_existing:
        try:
            try:
                from .odoo_pull import get_index
            except ImportError:
                from webapp.odoo_pull import get_index
            print("[DEDUP] Loading existing POD index ...")
            existing_index = get_index(config_path)
            print(f"[DEDUP] Indexed: {existing_index.summary()}")
        except Exception as exc:
            print(f"[DEDUP] Warning: failed to load index: {exc}")

    common, models = connect_odoo(odoo_cfg)

    # Verify connectivity
    try:
        version = common.version()
        print(f"Connected to Odoo: server_version={version.get('server_version', 'unknown')}")
    except Exception as exc:
        return {"ok": False, "error": f"Odoo connection failed: {exc}"}

    results = {
        "total": len(items),
        "skipped_existing": 0,
        "tracking_pushed": 0,
        "tracking_skipped_same": 0,
        "tracking_not_found": 0,
        "picking_pushed": 0,
        "picking_skipped_same": 0,
        "picking_not_found": 0,
        "failed": 0,
        "by_source": defaultdict(lambda: {"tracking_pushed": 0, "picking_pushed": 0, "not_found": 0, "failed": 0}),
        "details": [],
    }

    for item in items:
        key_type = item.get("preferred_key_type", "")
        key_value = item.get("preferred_key", "")
        source_file_url = item.get("file_url", "")
        local_path = item.get("local_path", "")
        source = item.get("source_name", "unknown")
        payload_item_id = item.get("payload_item_id", "")
        source_modified_at = item.get("source_modified_at", "")

        # Prefer local file interface URL over source file URL
        local_file_url = get_local_file_url(full_cfg, local_path)
        file_url = local_file_url if local_file_url else source_file_url

        # Derive pod_time from file metadata or use current time
        pod_time = _iso_to_odoo_datetime(source_modified_at) or _utc_now_str()

        detail = {
            "payload_item_id": payload_item_id,
            "source": source,
            "key_type": key_type,
            "key_value": key_value,
            "file_url": file_url,
            "status": "pending",
            "target_model": None,
            "target_id": None,
            "target_name": None,
            "error": None,
        }

        # Deduplication: skip if Odoo already has POD for this key
        if existing_index and existing_index.has_pod(
            tracking_number=key_value if key_type == "tracking_number" else None,
            picking_name=key_value if key_type == "picking_name" else None,
        ):
            existing_info = existing_index.get_pod_info(
                tracking_number=key_value if key_type == "tracking_number" else None,
                picking_name=key_value if key_type == "picking_name" else None,
            )
            detail["status"] = "skipped_existing"
            detail["target_model"] = existing_info.get("source_model") if existing_info else None
            detail["error"] = f"already has pod: {existing_info.get('pod_link', '')[:60]}..." if existing_info else "already has pod"
            results["skipped_existing"] += 1
            results["details"].append(detail)
            continue

        if not key_value or not file_url:
            detail["status"] = "skipped"
            detail["error"] = "missing key or file_url"
            results["details"].append(detail)
            continue

        # ---------------------------------------------------------
        # PRIMARY: Try stock.tracking by tracking_number
        # ---------------------------------------------------------
        if key_type == "tracking_number" or key_value:
            # Prefer tracking_number search on stock.tracking
            search_tn = key_value if key_type == "tracking_number" else key_value
            try:
                tracking_records = search_stock_tracking(models, odoo_cfg, search_tn)
            except Exception as exc:
                detail["status"] = "failed"
                detail["error"] = f"stock.tracking search error: {exc}"
                results["failed"] += 1
                results["by_source"][source]["failed"] += 1
                results["details"].append(detail)
                continue

            if tracking_records:
                track = tracking_records[0]
                tracking_id = track.get("id")
                current_pod_link = track.get("pod_link")
                picking_info = track.get("picking_id")
                detail["target_model"] = "stock.tracking"
                detail["target_id"] = tracking_id
                detail["target_name"] = track.get("tracking_number")

                if current_pod_link and str(current_pod_link) == file_url:
                    detail["status"] = "skipped_same"
                    results["tracking_skipped_same"] += 1
                    results["details"].append(detail)
                    continue

                if dry_run:
                    detail["status"] = "dry_run_tracking"
                    results["tracking_pushed"] += 1
                    results["by_source"][source]["tracking_pushed"] += 1
                    results["details"].append(detail)
                    continue

                try:
                    write_tracking_pod(models, odoo_cfg, tracking_id, file_url, pod_time)
                    detail["status"] = "pushed_tracking"
                    results["tracking_pushed"] += 1
                    results["by_source"][source]["tracking_pushed"] += 1
                except Exception as exc:
                    detail["status"] = "failed"
                    detail["error"] = f"write tracking error: {exc}"
                    results["failed"] += 1
                    results["by_source"][source]["failed"] += 1

                results["details"].append(detail)
                continue

        # ---------------------------------------------------------
        # FALLBACK: find stock.picking, then create stock.tracking
        # ---------------------------------------------------------
        try:
            pickings = search_picking(models, odoo_cfg, key_type, key_value)
        except Exception as exc:
            detail["status"] = "failed"
            detail["error"] = f"stock.picking search error: {exc}"
            results["failed"] += 1
            results["by_source"][source]["failed"] += 1
            results["details"].append(detail)
            continue

        if not pickings:
            detail["status"] = "not_found"
            results["tracking_not_found"] += 1 if key_type == "tracking_number" else 0
            results["picking_not_found"] += 1 if key_type == "picking_name" else 0
            results["by_source"][source]["not_found"] += 1
            results["details"].append(detail)
            continue

        pick = pickings[0]
        picking_id = pick.get("id")
        picking_name = pick.get("name")
        detail["target_model"] = "stock.tracking"
        detail["target_id"] = picking_id
        detail["target_name"] = picking_name

        if dry_run:
            detail["status"] = "dry_run_create_tracking"
            results["tracking_pushed"] += 1
            results["by_source"][source]["tracking_pushed"] += 1
            results["details"].append(detail)
            continue

        try:
            new_tracking_id = create_stock_tracking(
                models, odoo_cfg, picking_id, key_value, file_url, pod_time
            )
            detail["status"] = "created_tracking"
            detail["target_id"] = new_tracking_id
            results["tracking_pushed"] += 1
            results["by_source"][source]["tracking_pushed"] += 1
        except Exception as exc:
            detail["status"] = "failed"
            detail["error"] = f"create tracking error: {exc}"
            results["failed"] += 1
            results["by_source"][source]["failed"] += 1

        results["details"].append(detail)

    # Convert defaultdict to regular dict for JSON serialization
    results["by_source"] = dict(results["by_source"])
    return {"ok": True, **results}


def main():
    parser = argparse.ArgumentParser(description="Push POD links to Odoo")
    parser.add_argument("--config", default="config.local.json", help="Path to sync config JSON")
    parser.add_argument("--payload", required=True, help="Path to odoo_backfill_payload.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max items to process (0=unlimited)")
    parser.add_argument("--missing-only", action="store_true", help="Only push records not already in Odoo")
    parser.add_argument("--source", default=None, help="Filter by source name (CMC/EMTC)")
    parser.add_argument("--no-history", action="store_true", help="Skip writing to push history")
    args = parser.parse_args()

    from webapp.odoo_push_history import start_push_run, finish_push_run

    push_id = None
    log_fh = None
    if not args.no_history:
        push_id, log_path = start_push_run(
            config_path=args.config,
            payload_path=args.payload,
            dry_run=args.dry_run,
            limit=args.limit,
            source_filter=args.source,
        )
        log_fh = log_path.open("a", encoding="utf-8")
        # Tee stdout to log file
        class Tee:
            def __init__(self, stdout, fh):
                self.stdout = stdout
                self.fh = fh
            def write(self, data):
                self.stdout.write(data)
                self.fh.write(data)
                self.fh.flush()
            def flush(self):
                self.stdout.flush()
                self.fh.flush()
        sys.stdout = Tee(sys.stdout, log_fh)
        print(f"[PUSH START] {push_id}")
        print(f"Config: {args.config}")
        print(f"Payload: {args.payload}")
        print(f"Dry run: {args.dry_run}")
        print(f"Limit: {args.limit}")
        print(f"Source filter: {args.source}")
        print("-" * 60)

    result = push_payload(
        config_path=args.config,
        payload_path=args.payload,
        dry_run=args.dry_run,
        limit=args.limit,
        missing_only=args.missing_only,
        source_filter=args.source,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if push_id:
        status = "success" if result.get("ok") else "failed"
        finish_push_run(push_id, status=status, result=result)
        print("-" * 60)
        print(f"[PUSH FINISH] {push_id} -> {status}")
        if log_fh:
            log_fh.close()
        # Restore stdout before exit to avoid flush error on closed file
        if hasattr(sys.stdout, "stdout"):
            sys.stdout = sys.stdout.stdout

    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
