#!/usr/bin/env python3
"""Push POD links from Phase 1A payload back to Odoo via XML-RPC.

All POD data is written to stock.tracking (from tw_stock_tracking module).
The stock.picking.x_studio_pod_link field is no longer used as the POD target.

Usage:
    python webapp/odoo_push.py --config config.local.json --payload phase1a/out/.../odoo_backfill_payload.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.actions import resolve_project_path
from webapp.signed_urls import build_signed_storage_url, is_valid_file_url_secret


POD_INDEX_SUCCESS_STATUSES = {"pushed_tracking", "created_tracking", "skipped_same"}


class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: int):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: int):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


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
    except (ValueError, TypeError):
        return None
    if not local.is_file():
        return None

    file_url_secret = pod_sync_cfg.get("file_url_secret", "")
    if is_valid_file_url_secret(file_url_secret):
        return build_signed_storage_url(base_url, rel.as_posix(), file_url_secret)

    encoded = "/".join(quote(part, safe="") for part in rel.as_posix().split("/"))
    return f"{base_url}/storage/files/{encoded}"


def explain_local_file_url_failure(full_cfg: dict, local_path: str | None) -> str:
    """Return a concise, non-secret reason local POD URL generation would fail."""
    if not local_path:
        return "missing local_path; refusing to push source POD link"

    pod_sync_cfg = full_cfg.get("pod_sync", {})
    base_url = pod_sync_cfg.get("base_url", "").strip()
    if not base_url:
        return "missing pod_sync.base_url; refusing to push source POD link"

    download_cfg = full_cfg.get("download", {})
    base_dir = download_cfg.get("base_dir", "")
    if not base_dir:
        return "missing download.base_dir; refusing to push source POD link"

    try:
        local = Path(local_path).resolve()
        base = Path(base_dir).resolve()
        local.relative_to(base)
    except (ValueError, TypeError):
        return f"local_path is outside download.base_dir ({base_dir}); refusing to push source POD link"
    if not local.is_file():
        return "local_path does not exist; refusing to push source POD link"

    return "unable to build local POD URL; refusing to push source POD link"


def resolve_push_file_url(
    full_cfg: dict,
    *,
    local_path: str | None,
    source_file_url: str | None,
) -> tuple[str, str | None, str]:
    """Resolve the POD URL to write to Odoo.

    Local pod_sync URLs are the only supported push target. Source URLs from
    OneDrive/GDrive are useful for comparison pages, but must not be written back
    to Odoo.
    """
    local_file_url = get_local_file_url(full_cfg, local_path or "")
    if local_file_url:
        return local_file_url, None, "local"

    return "", explain_local_file_url_failure(full_cfg, local_path), "missing_local"


def connect_odoo(odoo_cfg: dict):
    url = odoo_cfg.get("url", "").rstrip("/")
    if not url:
        raise ValueError("Odoo URL not configured")
    timeout = int(odoo_cfg.get("timeout_seconds", 30))
    transport_cls = TimeoutSafeTransport if url.startswith("https://") else TimeoutTransport
    common_proxy = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/common",
        allow_none=True,
        transport=transport_cls(timeout),
    )
    models_proxy = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object",
        allow_none=True,
        transport=transport_cls(timeout),
    )
    return common_proxy, models_proxy


def _utc_now_str() -> str:
    """Return current UTC time in Odoo Datetime format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_successful_single_push_in_index(
    *,
    result: dict,
    match_key: str,
    match_key_type: str,
    file_url: str,
):
    if result.get("status") not in POD_INDEX_SUCCESS_STATUSES or not file_url:
        return
    try:
        try:
            from .odoo_pull import ExistingPodIndex, load_saved_index, save_index
        except ImportError:
            from webapp.odoo_pull import ExistingPodIndex, load_saved_index, save_index

        index = load_saved_index() or ExistingPodIndex()
        tracking_number = match_key if match_key_type == "tracking_number" else None
        picking_name = match_key if match_key_type == "picking_name" else None
        target_name = result.get("target_name")
        if target_name and match_key_type == "picking_name":
            tracking_number = target_name
        elif target_name and match_key_type == "tracking_number":
            tracking_number = tracking_number or target_name

        index.add_row(
            tracking_number=tracking_number,
            picking_name=picking_name,
            pod_link=file_url,
            source_model="local_push",
        )
        index.updated_at = _utc_now_iso()
        save_index(index)
    except Exception as exc:
        result["index_update_error"] = str(exc)


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


def _display_picking_name_for_search(value: str | None) -> str | None:
    """Convert canonical picking name back to display format for Odoo search.

    e.g. WCB2BOUT33271 -> WCB2B/OUT/33271
    """
    if not value:
        return value
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if normalized.startswith("WCB2BOUT"):
        return f"WCB2B/OUT/{normalized[len('WCB2BOUT'):]}"
    if normalized.startswith("WCB2COUT"):
        return f"WCB2C/OUT/{normalized[len('WCB2COUT'):]}"
    if normalized.startswith("WBB2BRET"):
        return f"WBB2B/RET/{normalized[len('WBB2BRET'):]}"
    if normalized.startswith("WCB2BRET"):
        return f"WCB2B/RET/{normalized[len('WCB2BRET'):]}"
    if normalized.startswith("WRB2BRET"):
        return f"WRB2B/RET/{normalized[len('WRB2BRET'):]}"
    if normalized.startswith("OTHER"):
        return f"Other/{normalized[len('OTHER'):]}"
    if normalized.startswith("WHOUT"):
        return f"WH/OUT/{normalized[len('WHOUT'):]}"
    return value


def _payload_lookup_key(item: dict) -> tuple[str, str]:
    key_type = item.get("preferred_key_type", "")
    key_value = item.get("preferred_key", "")
    if key_type == "picking_name":
        display_key = _display_picking_name_for_search(key_value)
        if display_key:
            key_value = display_key
    return key_type, key_value


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

    # Build domain: try both canonical and display formats for picking_name
    domain = [(field, "=", key_value)]
    if key_type == "picking_name":
        display_value = _display_picking_name_for_search(key_value)
        if display_value and display_value != key_value:
            domain = ["|", (field, "=", key_value), (field, "=", display_value)]

    res = models.execute_kw(
        db, uid, password,
        model, "web_search_read",
        [],
        {
            "domain": domain,
            "specification": {"id": {}, "name": {}, field: {}},
            "limit": 5,
            "order": "id DESC",
            "context": {"lang": "en_US", "tz": "Asia/Shanghai"},
        },
    )
    records = res.get("records", []) if isinstance(res, dict) else []
    return records


def search_stock_tracking_by_picking_id(models, odoo_cfg: dict, picking_id: int):
    """Search stock.tracking records linked to a given stock.picking."""
    db = odoo_cfg["db"]
    uid = odoo_cfg["uid"]
    password = odoo_cfg["api_key"]

    res = models.execute_kw(
        db, uid, password,
        "stock.tracking", "web_search_read",
        [],
        {
            "domain": [("picking_id", "=", picking_id)],
            "specification": {
                "id": {},
                "tracking_number": {},
                "picking_id": {},
                "pod_link": {},
                "pod_state": {},
                "api_state": {},
            },
            "limit": 50,
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


def _pod_link_overwrite_error(current_pod_link: str | None, target_pod_link: str) -> str | None:
    if current_pod_link and str(current_pod_link) != target_pod_link:
        return "stock.tracking already has a different pod_link; skip automatic overwrite"
    return None


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
            filtered_items = []
            for item in items:
                key_type, key_value = _payload_lookup_key(item)
                lookup_tracking = key_value if key_type == "tracking_number" else None
                lookup_picking = key_value if key_type == "picking_name" else None
                if not existing_index.has_pod(
                    tracking_number=lookup_tracking,
                    picking_name=lookup_picking,
                ):
                    filtered_items.append(item)
            items = filtered_items
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
        "ambiguous": 0,
        "needs_review": 0,
        "failed": 0,
        "by_source": defaultdict(
            lambda: {
                "tracking_pushed": 0,
                "picking_pushed": 0,
                "not_found": 0,
                "ambiguous": 0,
                "needs_review": 0,
                "failed": 0,
            }
        ),
        "details": [],
    }

    for item in items:
        key_type, key_value = _payload_lookup_key(item)
        source_file_url = item.get("file_url", "")
        local_path = item.get("local_path", "")
        source = item.get("source_name", "unknown")
        payload_item_id = item.get("payload_item_id", "")
        source_modified_at = item.get("source_modified_at", "")

        file_url, file_url_error, file_url_source = resolve_push_file_url(
            full_cfg,
            local_path=local_path,
            source_file_url=source_file_url,
        )

        # Derive pod_time from file metadata or use current time
        pod_time = _iso_to_odoo_datetime(source_modified_at) or _utc_now_str()

        detail = {
            "payload_item_id": payload_item_id,
            "source": source,
            "key_type": key_type,
            "key_value": key_value,
            "file_url": file_url,
            "file_url_source": file_url_source,
            "source_file_url": source_file_url,
            "local_path": local_path,
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
            detail["error"] = "missing key" if not key_value else file_url_error or "missing local file_url"
            results["details"].append(detail)
            continue

        # ---------------------------------------------------------
        # PRIMARY: Try stock.tracking by tracking_number
        # ---------------------------------------------------------
        if key_type == "tracking_number":
            try:
                tracking_records = search_stock_tracking(models, odoo_cfg, key_value)
            except Exception as exc:
                detail["status"] = "failed"
                detail["error"] = f"stock.tracking search error: {exc}"
                results["failed"] += 1
                results["by_source"][source]["failed"] += 1
                results["details"].append(detail)
                continue

            if tracking_records:
                if len(tracking_records) > 1:
                    detail["status"] = "multiple_tracking_matches"
                    detail["error"] = f"tracking_number matched {len(tracking_records)} stock.tracking records"
                    detail["target_model"] = "stock.tracking"
                    detail["target_id"] = [record.get("id") for record in tracking_records]
                    results["ambiguous"] += 1
                    results["by_source"][source]["ambiguous"] += 1
                    results["details"].append(detail)
                    continue
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
                overwrite_error = _pod_link_overwrite_error(current_pod_link, file_url)
                if overwrite_error:
                    detail["status"] = "needs_review"
                    detail["error"] = overwrite_error
                    detail["existing_pod_link"] = str(current_pod_link)
                    results["needs_review"] += 1
                    results["by_source"][source]["needs_review"] += 1
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
        # FALLBACK: find stock.picking, then update/create stock.tracking
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

        if len(pickings) > 1:
            detail["status"] = "multiple_picking_matches"
            detail["error"] = f"match key found {len(pickings)} stock.picking records"
            detail["target_model"] = odoo_cfg.get("push_model", "stock.picking")
            detail["target_id"] = [record.get("id") for record in pickings]
            results["ambiguous"] += 1
            results["by_source"][source]["ambiguous"] += 1
            results["details"].append(detail)
            continue

        pick = pickings[0]
        picking_id = pick.get("id")
        picking_name = pick.get("name")
        detail["target_model"] = "stock.tracking"
        detail["target_name"] = picking_name

        # Check for existing stock.tracking records linked to this picking
        try:
            picking_trackings = search_stock_tracking_by_picking_id(models, odoo_cfg, picking_id)
        except Exception as exc:
            detail["status"] = "failed"
            detail["error"] = f"search tracking by picking error: {exc}"
            results["failed"] += 1
            results["by_source"][source]["failed"] += 1
            results["details"].append(detail)
            continue

        if picking_trackings:
            if len(picking_trackings) > 1:
                detail["status"] = "multiple_tracking_records"
                detail["error"] = f"picking has {len(picking_trackings)} stock.tracking records; skip automatic POD write"
                detail["target_id"] = [track.get("id") for track in picking_trackings]
                results["ambiguous"] += 1
                results["by_source"][source]["ambiguous"] += 1
                results["details"].append(detail)
                continue

            track = picking_trackings[0]
            tracking_id = track.get("id")
            current_pod_link = track.get("pod_link")
            detail["target_id"] = tracking_id

            if current_pod_link and str(current_pod_link) == file_url:
                detail["status"] = "skipped_same"
                results["tracking_skipped_same"] += 1
                results["details"].append(detail)
                continue
            overwrite_error = _pod_link_overwrite_error(current_pod_link, file_url)
            if overwrite_error:
                detail["status"] = "needs_review"
                detail["error"] = overwrite_error
                detail["existing_pod_link"] = str(current_pod_link)
                results["needs_review"] += 1
                results["by_source"][source]["needs_review"] += 1
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
                detail["error"] = f"write tracking {tracking_id} error: {exc}"
                results["failed"] += 1
                results["by_source"][source]["failed"] += 1

            results["details"].append(detail)
            continue

        if key_type != "tracking_number":
            detail["status"] = "needs_review"
            detail["error"] = "picking matched but no stock.tracking exists; tracking_number is required before creating stock.tracking"
            detail["target_id"] = picking_id
            results["needs_review"] += 1
            results["by_source"][source]["needs_review"] += 1
            results["details"].append(detail)
            continue

        if dry_run:
            detail["status"] = "dry_run_create_tracking"
            detail["target_id"] = picking_id
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


def push_single_item(
    *,
    config_path: str,
    match_key: str,
    match_key_type: str,
    file_url: str,
    local_path: str | None = None,
    source_name: str = "unknown",
    dry_run: bool = False,
):
    """Push a single POD link to Odoo stock.tracking (or create via stock.picking fallback)."""
    full_cfg = load_config(config_path)
    odoo_cfg = get_odoo_cfg(full_cfg)
    if not odoo_cfg.get("enabled", False):
        return {"ok": False, "error": "Odoo push is disabled in config"}

    required_keys = ("url", "db", "uid", "api_key")
    missing = [k for k in required_keys if not odoo_cfg.get(k)]
    if missing:
        return {"ok": False, "error": f"Missing Odoo config keys: {missing}"}

    final_url, file_url_error, file_url_source = resolve_push_file_url(
        full_cfg,
        local_path=local_path,
        source_file_url=file_url,
    )
    if not final_url:
        return {
            "ok": False,
            "match_key": match_key,
            "match_key_type": match_key_type,
            "file_url": "",
            "file_url_source": file_url_source,
            "source_file_url": file_url,
            "local_path": local_path,
            "status": "skipped",
            "target_model": None,
            "target_id": None,
            "target_name": None,
            "error": file_url_error or "missing local file_url",
        }

    common, models = connect_odoo(odoo_cfg)
    try:
        version = common.version()
    except Exception as exc:
        return {"ok": False, "error": f"Odoo connection failed: {exc}"}

    key_type = match_key_type
    key_value = match_key
    pod_time = _utc_now_str()

    result = {
        "ok": True,
        "match_key": key_value,
        "match_key_type": key_type,
        "file_url": final_url,
        "file_url_source": file_url_source,
        "source_file_url": file_url,
        "local_path": local_path,
        "status": "pending",
        "target_model": None,
        "target_id": None,
        "target_name": None,
        "error": None,
        "server_version": version.get("server_version", "unknown"),
    }

    def finish():
        _record_successful_single_push_in_index(
            result=result,
            match_key=key_value,
            match_key_type=key_type,
            file_url=final_url,
        )
        return result

    # PRIMARY: Try stock.tracking by tracking_number
    if key_type == "tracking_number":
        try:
            tracking_records = search_stock_tracking(models, odoo_cfg, key_value)
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = f"stock.tracking search error: {exc}"
            return finish()

        if tracking_records:
            if len(tracking_records) > 1:
                result["status"] = "multiple_tracking_matches"
                result["target_model"] = "stock.tracking"
                result["target_id"] = [record.get("id") for record in tracking_records]
                result["error"] = f"tracking_number matched {len(tracking_records)} stock.tracking records"
                return finish()
            track = tracking_records[0]
            tracking_id = track.get("id")
            current_pod_link = track.get("pod_link")
            result["target_model"] = "stock.tracking"
            result["target_id"] = tracking_id
            result["target_name"] = track.get("tracking_number")

            if current_pod_link and str(current_pod_link) == final_url:
                result["status"] = "skipped_same"
                return finish()
            overwrite_error = _pod_link_overwrite_error(current_pod_link, final_url)
            if overwrite_error:
                result["status"] = "needs_review"
                result["error"] = overwrite_error
                result["existing_pod_link"] = str(current_pod_link)
                return finish()

            if dry_run:
                result["status"] = "dry_run_tracking"
                return finish()

            try:
                write_tracking_pod(models, odoo_cfg, tracking_id, final_url, pod_time)
                result["status"] = "pushed_tracking"
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = f"write tracking error: {exc}"
            return finish()

    # FALLBACK: find stock.picking, then update/create stock.tracking
    try:
        pickings = search_picking(models, odoo_cfg, key_type, key_value)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"stock.picking search error: {exc}"
        return finish()

    if not pickings:
        result["status"] = "not_found"
        result["error"] = "No matching stock.tracking or stock.picking found"
        return finish()

    if len(pickings) > 1:
        result["status"] = "multiple_picking_matches"
        result["target_model"] = odoo_cfg.get("push_model", "stock.picking")
        result["target_id"] = [record.get("id") for record in pickings]
        result["error"] = f"match key found {len(pickings)} stock.picking records"
        return finish()

    pick = pickings[0]
    picking_id = pick.get("id")
    result["target_model"] = "stock.tracking"
    result["target_name"] = pick.get("name")

    # Check for existing stock.tracking records linked to this picking
    try:
        picking_trackings = search_stock_tracking_by_picking_id(models, odoo_cfg, picking_id)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"search tracking by picking error: {exc}"
        return finish()

    if picking_trackings:
        if len(picking_trackings) > 1:
            result["status"] = "multiple_tracking_records"
            result["target_id"] = [track.get("id") for track in picking_trackings]
            result["error"] = f"picking has {len(picking_trackings)} stock.tracking records; skip automatic POD write"
            return finish()

        primary_track = picking_trackings[0]
        result["target_id"] = primary_track.get("id")
        result["target_name"] = primary_track.get("tracking_number")

        current_pod_link = primary_track.get("pod_link")
        if current_pod_link and str(current_pod_link) == final_url:
            result["status"] = "skipped_same"
            return finish()
        overwrite_error = _pod_link_overwrite_error(current_pod_link, final_url)
        if overwrite_error:
            result["status"] = "needs_review"
            result["error"] = overwrite_error
            result["existing_pod_link"] = str(current_pod_link)
            return finish()

        if dry_run:
            result["status"] = "dry_run_tracking"
            return finish()

        try:
            write_tracking_pod(models, odoo_cfg, primary_track.get("id"), final_url, pod_time)
            result["status"] = "pushed_tracking"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = f"write tracking {primary_track.get('id')} error: {exc}"
        return finish()

    result["target_id"] = picking_id
    if key_type != "tracking_number":
        result["status"] = "needs_review"
        result["error"] = "picking matched but no stock.tracking exists; tracking_number is required before creating stock.tracking"
        return finish()

    if dry_run:
        result["status"] = "dry_run_create_tracking"
        return finish()

    try:
        new_tracking_id = create_stock_tracking(
            models, odoo_cfg, picking_id, key_value, final_url, pod_time
        )
        result["status"] = "created_tracking"
        result["target_id"] = new_tracking_id
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"create tracking error: {exc}"

    return finish()


def preflight_payload(
    *,
    config_path: str,
    payload_path: str,
    limit: int = 0,
    source_filter: str | None = None,
    missing_only: bool = False,
):
    """Check which payload items are actually findable in Odoo before pushing.

    Returns categories:
        - ready_to_push: items that exist in Odoo and don't already have POD
        - already_has_pod: items that already have a POD link in Odoo
        - not_found: items whose key cannot be found in Odoo
        - invalid: items missing key or file_url
    """
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
            filtered_items = []
            for item in items:
                key_type, key_value = _payload_lookup_key(item)
                if not existing_index.has_pod(
                    tracking_number=key_value if key_type == "tracking_number" else None,
                    picking_name=key_value if key_type == "picking_name" else None,
                ):
                    filtered_items.append(item)
            items = filtered_items
        except Exception as exc:
            print(f"[MISSING_ONLY] Warning: failed to filter: {exc}")

    if limit > 0:
        items = items[:limit]

    # Load existing POD index for deduplication
    existing_index = None
    try:
        try:
            from .odoo_pull import get_index
        except ImportError:
            from webapp.odoo_pull import get_index
        existing_index = get_index(config_path)
    except Exception as exc:
        print(f"[PREFLIGHT] Warning: failed to load index: {exc}")

    common, models = connect_odoo(odoo_cfg)
    try:
        version = common.version()
    except Exception as exc:
        return {"ok": False, "error": f"Odoo connection failed: {exc}"}

    ready_to_push = []
    already_has_pod = []
    not_found = []
    invalid = []

    for item in items:
        key_type, key_value = _payload_lookup_key(item)
        source = item.get("source_name", "unknown")
        payload_item_id = item.get("payload_item_id", "")
        source_file_url = item.get("file_url", "")
        local_path = item.get("local_path", "")
        file_url, file_url_error, file_url_source = resolve_push_file_url(
            full_cfg,
            local_path=local_path,
            source_file_url=source_file_url,
        )

        entry = {
            "payload_item_id": payload_item_id,
            "source": source,
            "key_type": key_type,
            "key_value": key_value,
            "picking_name": item.get("picking_name"),
            "tracking_number": item.get("tracking_number"),
            "file_name": item.get("file_name"),
            "file_url": file_url,
            "file_url_source": file_url_source,
        }

        if not key_value or not file_url:
            if not key_value:
                entry["error"] = "missing key"
            else:
                entry["error"] = file_url_error or "missing local file_url"
            invalid.append(entry)
            continue

        # Check existing POD
        if existing_index and existing_index.has_pod(
            tracking_number=key_value if key_type == "tracking_number" else None,
            picking_name=key_value if key_type == "picking_name" else None,
        ):
            already_has_pod.append(entry)
            continue

        # Check Odoo existence
        found = False
        if key_type == "tracking_number" and key_value:
            try:
                tracking_records = search_stock_tracking(models, odoo_cfg, key_value)
                if tracking_records:
                    found = True
            except Exception:
                pass

        if not found:
            try:
                pickings = search_picking(models, odoo_cfg, key_type, key_value)
                if pickings:
                    found = True
            except Exception:
                pass

        if found:
            ready_to_push.append(entry)
        else:
            not_found.append(entry)

    return {
        "ok": True,
        "server_version": version.get("server_version", "unknown"),
        "total": len(items),
        "ready_to_push_count": len(ready_to_push),
        "already_has_pod_count": len(already_has_pod),
        "not_found_count": len(not_found),
        "invalid_count": len(invalid),
        "ready_to_push": ready_to_push,
        "already_has_pod": already_has_pod,
        "not_found": not_found,
        "invalid": invalid,
    }


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
