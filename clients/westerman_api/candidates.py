"""Read-only Odoo candidate discovery for the Westerman POD pull pool."""

from __future__ import annotations

import xmlrpc.client
from datetime import datetime, timedelta, timezone


DEFAULT_STATES = ("waiting", "assigned", "done")
DEFAULT_WMS_STATUSES = ("sent", "partial_ref_back", "ref_back", "pod")


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


def _clean_string_list(value, default):
    if value in (None, ""):
        return list(default)
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        raise ValueError("Westerman candidate state configuration must be a list or comma-separated string")
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    if not cleaned:
        raise ValueError("Westerman candidate state configuration must not be empty")
    return list(dict.fromkeys(cleaned))


def _bounded_positive_int(value, *, default, minimum=1, maximum=10000, label):
    try:
        parsed = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def build_candidate_domain(source_cfg: dict, *, now: datetime | None = None):
    """Build the stable, ID-free Westerman stock.picking domain."""
    now = now or datetime.now(timezone.utc)
    lookback_days = _bounded_positive_int(
        source_cfg.get("candidate_lookback_days"),
        default=180,
        maximum=3650,
        label="westerman_api.candidate_lookback_days",
    )
    states = _clean_string_list(source_cfg.get("candidate_states"), DEFAULT_STATES)
    wms_statuses = _clean_string_list(
        source_cfg.get("candidate_wms_statuses"), DEFAULT_WMS_STATUSES
    )
    delivery_type = str(source_cfg.get("carrier_delivery_type") or "westman").strip()
    if not delivery_type:
        raise ValueError("westerman_api.carrier_delivery_type must not be empty")
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    return [
        ("picking_type_id.code", "=", "outgoing"),
        ("carrier_id.delivery_type", "=", delivery_type),
        ("state", "in", states),
        ("wms_status", "in", wms_statuses),
        ("create_date", ">=", cutoff),
    ]


def connect_models(odoo_cfg: dict):
    url = str(odoo_cfg.get("url") or "").strip().rstrip("/")
    if not url:
        raise ValueError("Odoo URL not configured")
    if not odoo_cfg.get("db") or not odoo_cfg.get("uid") or not odoo_cfg.get("api_key"):
        raise ValueError("Odoo db, uid and api_key are required for Westerman candidate discovery")
    timeout = _bounded_positive_int(
        odoo_cfg.get("timeout_seconds"),
        default=30,
        maximum=600,
        label="odoo.timeout_seconds",
    )
    transport_cls = TimeoutSafeTransport if url.startswith("https://") else TimeoutTransport
    return xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object",
        allow_none=True,
        transport=transport_cls(timeout),
    )


def _read_candidate_pickings(models, odoo_cfg, source_cfg, *, now=None):
    db, uid, password = odoo_cfg["db"], odoo_cfg["uid"], odoo_cfg["api_key"]
    page_size = _bounded_positive_int(
        source_cfg.get("candidate_page_size"),
        default=500,
        maximum=2000,
        label="westerman_api.candidate_page_size",
    )
    scan_limit = _bounded_positive_int(
        source_cfg.get("candidate_scan_limit"),
        default=5000,
        maximum=50000,
        label="westerman_api.candidate_scan_limit",
    )
    domain = build_candidate_domain(source_cfg, now=now)
    rows = []
    offset = 0
    # Fetch one sentinel row beyond the configured cap. This distinguishes an
    # exact-size complete result from a genuinely truncated scan.
    target_count = scan_limit + 1
    fields = ["id", "name", "state", "wms_status", "create_date", "write_date", "carrier_tracking_ref"]
    while len(rows) < target_count:
        limit = min(page_size, target_count - len(rows))
        page = models.execute_kw(
            db,
            uid,
            password,
            "stock.picking",
            "search_read",
            [domain],
            {
                "fields": fields,
                "offset": offset,
                "limit": limit,
                # Prefer recent outbound orders if the safety cap is reached;
                # a truncated scan is degraded/fail-closed by the batch layer.
                "order": "create_date desc, id desc",
            },
        )
        if not isinstance(page, list):
            raise ValueError("Odoo stock.picking search_read returned an unexpected response")
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < limit:
            break
        offset += len(page)
    return rows[:scan_limit], len(rows) > scan_limit


def _existing_pod_picking_ids(models, odoo_cfg, picking_ids, *, chunk_size=500):
    if not picking_ids:
        return set()
    db, uid, password = odoo_cfg["db"], odoo_cfg["uid"], odoo_cfg["api_key"]
    result = set()
    for start in range(0, len(picking_ids), chunk_size):
        chunk = picking_ids[start : start + chunk_size]
        rows = models.execute_kw(
            db,
            uid,
            password,
            "stock.tracking",
            "search_read",
            [[("picking_id", "in", chunk), ("pod_link", "!=", False)]],
            {"fields": ["picking_id"], "limit": max(1, len(chunk) * 10)},
        )
        if not isinstance(rows, list):
            raise ValueError("Odoo stock.tracking search_read returned an unexpected response")
        for row in rows:
            picking = row.get("picking_id") if isinstance(row, dict) else None
            if isinstance(picking, (list, tuple)) and picking:
                result.add(int(picking[0]))
            elif isinstance(picking, int):
                result.add(picking)
    return result


def fetch_candidates(full_cfg: dict, *, models=None, now: datetime | None = None):
    """Return eligible pool candidates and references already completed in Odoo."""
    source_cfg = dict(full_cfg.get("westerman_api") or {})
    odoo_cfg = dict(full_cfg.get("odoo") or {})
    # Candidate discovery is an external Odoo read and must be explicitly
    # enabled.  A missing section or a missing flag is intentionally disabled.
    if source_cfg.get("enabled") is not True:
        return {
            "scanned_count": 0,
            "scan_truncated": False,
            "candidate_count": 0,
            "already_completed_count": 0,
            "candidates": [],
            "completed_references": [],
        }
    models = models or connect_models(odoo_cfg)
    rows, scan_truncated = _read_candidate_pickings(
        models, odoo_cfg, source_cfg, now=now
    )
    picking_ids = [int(row["id"]) for row in rows if row.get("id")]
    completed_ids = _existing_pod_picking_ids(models, odoo_cfg, picking_ids)
    candidates = []
    completed_references = []
    for row in rows:
        reference = str(row.get("name") or "").strip()
        picking_id = int(row.get("id") or 0)
        if not reference or not picking_id:
            continue
        if picking_id in completed_ids:
            completed_references.append(reference)
            continue
        candidates.append(
            {
                "shipment_reference": reference,
                "odoo_picking_id": picking_id,
                "tracking_number": str(row.get("carrier_tracking_ref") or "").strip() or None,
                "picking_state": str(row.get("state") or "").strip() or None,
                "wms_status": str(row.get("wms_status") or "").strip() or None,
            }
        )
    return {
        "scanned_count": len(rows),
        "scan_truncated": scan_truncated,
        "candidate_count": len(candidates),
        "already_completed_count": len(completed_references),
        "candidates": candidates,
        "completed_references": completed_references,
    }
