from __future__ import annotations

from typing import Any

import requests

from clients.westerman_api.candidates import connect_models
from webapp.odoo_reconcile_cache import (
    load_cached_pod_state,
    refresh_reconcile_cache,
    resolve_read_backend,
)


REQUIRED_QUERY65_FIELDS = {"tracking_number", "picking_name", "pod_link"}
SHADOW_CONCURRENT_ONLY_LIMIT = 20


def _querydeluxe_rows(full_cfg: dict) -> list[dict]:
    odoo_cfg = full_cfg.get("odoo") or {}
    url = str(odoo_cfg.get("url") or "").rstrip("/")
    if not url:
        raise ValueError("Odoo URL not configured")
    query_id = (full_cfg.get("querydeluxe") or {}).get("existing_pod_query_id", 65)
    response = requests.get(
        f"{url}/api/querydeluxe/{query_id}/data",
        timeout=int(odoo_cfg.get("timeout_seconds", 60)),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("QueryDeluxe POD index returned an invalid response shape")
    if not payload.get("success"):
        raise ValueError("QueryDeluxe POD index returned success=false")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("QueryDeluxe POD index data has an invalid response shape")
    headers = list(data.get("header") or [])
    if not REQUIRED_QUERY65_FIELDS.issubset(headers):
        raise ValueError("QueryDeluxe POD index response is missing required columns")
    result = []
    for row_number, row in enumerate(data.get("data") or [], start=1):
        if isinstance(row, dict):
            normalized = dict(row)
        elif isinstance(row, (list, tuple)) and len(row) == len(headers):
            normalized = dict(zip(headers, row))
        else:
            raise ValueError(f"QueryDeluxe POD index row {row_number} has an invalid shape")
        if not REQUIRED_QUERY65_FIELDS.issubset(normalized):
            raise ValueError(f"QueryDeluxe POD index row {row_number} is missing required fields")
        result.append(normalized)
    return result


def _picking_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _direct_coverage_ids(full_cfg: dict, *, models=None, page_size: int = 2000) -> list[int]:
    odoo_cfg = full_cfg.get("odoo") or {}
    models = models or connect_models(odoo_cfg)
    result = set()
    last_id = 0
    while True:
        rows = models.execute_kw(
            odoo_cfg["db"], odoo_cfg["uid"], odoo_cfg["api_key"],
            "stock.tracking", "search_read",
            [[("pod_link", "!=", False), ("picking_id", "!=", False), ("id", ">", last_id)]],
            {"fields": ["id", "picking_id"], "limit": page_size, "order": "id ASC"},
        )
        if not isinstance(rows, list):
            raise ValueError("Odoo stock.tracking.search_read returned an unexpected response")
        if not rows:
            break
        ids = [int(row.get("id") or 0) for row in rows]
        new_last_id = max(ids, default=0)
        if new_last_id <= last_id:
            raise ValueError("Odoo stock.tracking pagination cursor did not advance")
        result.update(
            value for row in rows if (value := _picking_id(row.get("picking_id"))) is not None
        )
        last_id = new_last_id
        if len(rows) < page_size:
            break
    return sorted(result)


def _index_projection(rows: list[dict]) -> dict[str, dict[str, frozenset[tuple[str, str]]]]:
    tracking: dict[str, set[tuple[str, str]]] = {}
    picking: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        pod_link = str(row.get("pod_link") or "")
        if not pod_link:
            continue
        picking_name = str(row.get("picking_name") or "").strip()
        tracking_number = str(row.get("tracking_number") or "")
        for token in tracking_number.split(","):
            key = token.strip()
            if key:
                tracking.setdefault(key, set()).add((picking_name, pod_link))
        if picking_name:
            picking.setdefault(picking_name, set()).add((tracking_number, pod_link))
    return {
        "tracking": {key: frozenset(value) for key, value in tracking.items()},
        "picking": {key: frozenset(value) for key, value in picking.items()},
    }


def _shadow_counts(authoritative_rows: list[dict], candidate_rows: list[dict]) -> dict:
    authoritative = _index_projection(authoritative_rows)
    candidate = _index_projection(candidate_rows)
    result = {}
    for label in ("tracking", "picking"):
        left = authoritative[label]
        right = candidate[label]
        left_keys = set(left)
        right_keys = set(right)
        result[label] = {
            "authoritative_count": len(left),
            "candidate_count": len(right),
            "authoritative_only_count": len(left_keys - right_keys),
            "candidate_only_count": len(right_keys - left_keys),
            "mapping_mismatch_count": sum(
                1 for key in left_keys & right_keys if left[key] != right[key]
            ),
        }
    return result


def classify_shadow_status(
    *,
    mapping_mismatch_count: int,
    only_counts: list[int] | tuple[int, ...],
) -> str:
    """Classify small key drift separately without hiding bulk omissions.

    QueryDeluxe and XML-RPC are read sequentially, so a handful of key-only
    rows can be legitimate concurrent writes. More than the bounded allowance
    is treated as a real difference and blocks a quiet cutover.
    """
    if mapping_mismatch_count:
        return "differences"
    normalized_only_counts = [max(0, int(value or 0)) for value in only_counts]
    if any(value > SHADOW_CONCURRENT_ONLY_LIMIT for value in normalized_only_counts):
        return "differences"
    if any(normalized_only_counts):
        return "inconclusive_concurrent_changes"
    return "match"


def _shadow_status(counts: dict) -> str:
    return classify_shadow_status(
        mapping_mismatch_count=sum(
            int(group.get("mapping_mismatch_count") or 0)
            for group in counts.values()
        ),
        only_counts=[
            int(group.get(key) or 0)
            for group in counts.values()
            for key in ("authoritative_only_count", "candidate_only_count")
        ],
    )


def fetch_pod_read_state(
    full_cfg: dict,
    *,
    config_path: str,
    refresh_xmlrpc_cache: bool = True,
) -> dict:
    """Return one complete, mode-consistent POD read generation.

    Shadow XML-RPC failures are represented as metadata; they never replace the
    QueryDeluxe authority. XML-RPC authoritative failures propagate before any
    caller-owned files are committed.
    """
    backend = resolve_read_backend(full_cfg)
    cache_state = None
    if backend in {"querydeluxe", "shadow"}:
        authoritative_rows = _querydeluxe_rows(full_cfg)
        authoritative_source = "querydeluxe"
    else:
        if refresh_xmlrpc_cache:
            refresh_reconcile_cache(config_path, full_cfg=full_cfg)
        cache_state = load_cached_pod_state(config_path, full_cfg=full_cfg)
        if not cache_state.get("available"):
            raise RuntimeError("XML-RPC reconcile cache is not available")
        authoritative_rows = cache_state["rows"]
        authoritative_source = "xmlrpc"

    result = {
        "requested_backend": backend,
        "authoritative_source": authoritative_source,
        "rows": authoritative_rows,
        "coverage_ids": [],
        "coverage_status": "",
        "shadow_status": "",
        "shadow_difference_counts": {},
        "cache_generation": int((cache_state or {}).get("generation") or 0),
        "cache_epoch": str((cache_state or {}).get("cache_epoch") or ""),
    }

    if backend == "shadow":
        try:
            if refresh_xmlrpc_cache:
                refresh_reconcile_cache(config_path, full_cfg=full_cfg)
            cache_state = load_cached_pod_state(config_path, full_cfg=full_cfg)
            if not cache_state.get("available"):
                raise RuntimeError("XML-RPC reconcile cache is not available")
            candidate_rows = cache_state["rows"]
            counts = _shadow_counts(authoritative_rows, candidate_rows)
            result["shadow_status"] = _shadow_status(counts)
            result["shadow_difference_counts"] = counts
            result["coverage_ids"] = cache_state["picking_ids"]
            result["coverage_status"] = "complete"
            result["cache_generation"] = int(cache_state["generation"])
            result["cache_epoch"] = str(cache_state.get("cache_epoch") or "")
        except Exception as exc:
            result["shadow_status"] = "unavailable"
            result["shadow_difference_counts"] = {"error_type": type(exc).__name__}
            try:
                result["coverage_ids"] = _direct_coverage_ids(full_cfg)
                result["coverage_status"] = "complete"
            except Exception as coverage_exc:
                result["coverage_status"] = f"unavailable:{type(coverage_exc).__name__}"
    elif backend == "xmlrpc":
        result["coverage_ids"] = cache_state["picking_ids"]
        result["coverage_status"] = "complete"
    else:
        try:
            # QueryDeluxe is the rollback path. Keep its historical behaviour:
            # coverage is read live instead of silently reusing a possibly
            # stale SQLite generation created by shadow/XML-RPC mode.
            result["coverage_ids"] = _direct_coverage_ids(full_cfg)
            result["coverage_status"] = "complete"
        except Exception as exc:
            result["coverage_status"] = f"unavailable:{type(exc).__name__}"
    return result
