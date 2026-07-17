#!/usr/bin/env python3
"""Read-only shadow validation for replacing QueryDeluxe with Odoo XML-RPC.

The command compares QueryDeluxe query 64/65 with ordinary XML-RPC reads.
It never calls create/write/unlink and stores only aggregate counts plus record
IDs for a small mismatch sample; POD URLs and credentials are never persisted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from xmlrpc.client import ProtocolError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.westerman_api.candidates import connect_models
from webapp.actions import resolve_project_path
from webapp.odoo_pull import ExistingPodIndex


DEFAULT_DELIVERY_TYPES = ("spare", "cmc", "westman")
DEFAULT_PAGE_SIZE = 2000
READ_ONLY_METHODS = frozenset({"read", "search_read"})
TRANSIENT_XMLRPC_HTTP_CODES = frozenset({502, 503, 504})
XMLRPC_MAX_ATTEMPTS = 3
EXPECTED_QUERY_IDS = {
    "missing_tracking_query_id": 64,
    "existing_pod_query_id": 65,
}
QUERY_REQUIRED_HEADERS = {
    "missing_tracking_query_id": {
        "odoo_picking_id",
        "picking_name",
        "carrier_id",
        "carrier_name",
        "state",
        "business_datetime",
        "x_studio_pod_link",
        "carrier_tracking_ref",
    },
    "existing_pod_query_id": {
        "id",
        "tracking_number",
        "pod_link",
        "picking_name",
        "carrier_tracking_ref",
    },
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _odoo_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_odoo_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value in (None, False, ""):
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value: Any) -> str:
    if value in (None, False):
        return ""
    return str(value)


def _clean_text(value: Any) -> str:
    return _text(value).strip()


def _load_config(config_path: str) -> dict:
    path = resolve_project_path(config_path, "config.local.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _querydeluxe_url(full_cfg: dict, query_key: str) -> str:
    base_url = str(full_cfg.get("odoo", {}).get("url") or "").rstrip("/")
    query_id = _as_int(full_cfg.get("querydeluxe", {}).get(query_key))
    if not base_url or not query_id:
        raise ValueError(f"Missing Odoo URL or querydeluxe.{query_key}")
    expected_id = EXPECTED_QUERY_IDS[query_key]
    if query_id != expected_id:
        raise ValueError(
            f"querydeluxe.{query_key} must be {expected_id} for this migration validation"
        )
    return f"{base_url}/api/querydeluxe/{query_id}/data"


def _normalize_query_rows(data: dict, *, required_headers: set[str] | None = None) -> list[dict]:
    headers = list(data.get("header") or [])
    missing_headers = sorted((required_headers or set()) - set(headers))
    if missing_headers:
        raise ValueError(f"QueryDeluxe response missing required headers: {', '.join(missing_headers)}")
    normalized = []
    for row_number, row in enumerate(data.get("data") or [], start=1):
        if isinstance(row, dict):
            missing_row_fields = sorted((required_headers or set()) - set(row))
            if missing_row_fields:
                raise ValueError(
                    f"QueryDeluxe row {row_number} missing required fields: {', '.join(missing_row_fields)}"
                )
            normalized.append(dict(row))
        else:
            if len(row) != len(headers):
                raise ValueError(
                    f"QueryDeluxe row {row_number} has {len(row)} values for {len(headers)} headers"
                )
            normalized.append(dict(zip(headers, row)))
    return normalized


def _fetch_querydeluxe(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    required_headers: set[str],
) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    response = session.get(url, timeout=timeout)
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise ValueError("QueryDeluxe returned success=false")
    data = payload.get("data") or {}
    rows = _normalize_query_rows(data, required_headers=required_headers)
    return rows, {
        "row_count": len(rows),
        "response_bytes": len(response.content),
        "elapsed_seconds": round(elapsed, 3),
        "headers": list(data.get("header") or []),
    }


def _execute_read_only(models, odoo_cfg: dict, model: str, method: str, args: list, kwargs: dict):
    if method not in READ_ONLY_METHODS:
        raise AssertionError(f"Refusing non-read-only XML-RPC method: {method}")
    for attempt in range(1, XMLRPC_MAX_ATTEMPTS + 1):
        try:
            return models.execute_kw(
                odoo_cfg["db"],
                odoo_cfg["uid"],
                odoo_cfg["api_key"],
                model,
                method,
                args,
                kwargs,
            )
        except ProtocolError as exc:
            if exc.errcode not in TRANSIENT_XMLRPC_HTTP_CODES or attempt >= XMLRPC_MAX_ATTEMPTS:
                raise
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _search_read_pages(
    models,
    odoo_cfg: dict,
    *,
    model: str,
    domain: list,
    fields: list[str],
    page_size: int,
    context: dict | None = None,
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    last_id = 0
    calls = 0
    response_bytes = 0
    started = time.perf_counter()
    while True:
        page_domain = list(domain) + [("id", ">", last_id)]
        kwargs = {"fields": fields, "limit": page_size, "order": "id ASC"}
        if context:
            kwargs["context"] = dict(context)
        page = _execute_read_only(
            models,
            odoo_cfg,
            model,
            "search_read",
            [page_domain],
            kwargs,
        )
        calls += 1
        response_bytes += len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if not page:
            break
        page_ids = [_as_int(row.get("id")) for row in page]
        if any(value is None for value in page_ids):
            raise ValueError(f"{model} returned a row without a valid id")
        new_last_id = max(page_ids)
        if new_last_id <= last_id:
            raise ValueError(f"{model} id cursor did not advance")
        rows.extend(page)
        last_id = new_last_id
        if len(page) < page_size:
            break
    return rows, {
        "row_count": len(rows),
        "calls": calls,
        "response_bytes": response_bytes,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "last_id": last_id,
    }


def _read_ids(
    models,
    odoo_cfg: dict,
    *,
    model: str,
    ids: Iterable[int],
    fields: list[str],
    batch_size: int,
) -> list[dict]:
    ordered_ids = sorted(set(int(value) for value in ids if value))
    rows: list[dict] = []
    for start in range(0, len(ordered_ids), batch_size):
        batch = ordered_ids[start : start + batch_size]
        rows.extend(
            _execute_read_only(
                models,
                odoo_cfg,
                model,
                "read",
                [batch],
                {"fields": fields},
            )
        )
    return rows


def _canonical_query64_row(row: dict) -> dict:
    return {
        "id": _as_int(row.get("odoo_picking_id")),
        "name": _text(row.get("picking_name")),
        "carrier_id": _as_int(row.get("carrier_id")),
        "carrier_name": _clean_text(row.get("carrier_name")),
        "state": _text(row.get("state")),
        "business_datetime": _text(row.get("business_datetime")),
        "tracking_number": _clean_text(
            row.get("tracking_number") or row.get("carrier_tracking_ref")
        ),
        "pod_link": _clean_text(row.get("x_studio_pod_link")),
        "write_date": "",
    }


def _canonical_picking_row(row: dict, carrier_names_by_id: dict[int, str] | None = None) -> dict:
    carrier_id = _as_int(row.get("carrier_id"))
    return {
        "id": _as_int(row.get("id")),
        "name": _text(row.get("name")),
        "carrier_id": carrier_id,
        "carrier_name": _clean_text((carrier_names_by_id or {}).get(carrier_id, "")),
        "state": _text(row.get("state")),
        "business_datetime": _text(
            row.get("date_done") or row.get("scheduled_date") or row.get("create_date")
        ),
        "tracking_number": _clean_text(row.get("carrier_tracking_ref")),
        "pod_link": _clean_text(row.get("x_studio_pod_link")),
        "write_date": _text(row.get("write_date")),
    }


def _canonical_query65_row(row: dict) -> dict:
    return {
        "id": _as_int(row.get("id")),
        "tracking_number": _text(row.get("tracking_number")),
        "picking_name": _text(row.get("picking_name")),
        "carrier_tracking_ref": _text(row.get("carrier_tracking_ref")),
        "pod_link": _text(row.get("pod_link")),
        "write_date": "",
    }


def _canonical_tracking_row(row: dict, pickings_by_id: dict[int, dict]) -> dict:
    picking_id = _as_int(row.get("picking_id"))
    picking = pickings_by_id.get(picking_id) or {}
    return {
        "id": _as_int(row.get("id")),
        "tracking_number": _text(row.get("tracking_number")),
        "picking_name": _text(picking.get("name")),
        "carrier_tracking_ref": _text(picking.get("carrier_tracking_ref")),
        "pod_link": _text(row.get("pod_link")),
        "write_date": _text(row.get("write_date")),
    }


def _rows_by_id(rows: Iterable[dict]) -> tuple[dict[int, dict], list[int], int]:
    indexed: dict[int, dict] = {}
    duplicates = []
    invalid_count = 0
    for row in rows:
        record_id = _as_int(row.get("id"))
        if record_id is None:
            invalid_count += 1
            continue
        if record_id in indexed:
            duplicates.append(record_id)
        indexed[record_id] = row
    return indexed, duplicates, invalid_count


def _is_recent(row: dict, recent_cutoff: datetime) -> bool:
    write_date = _parse_odoo_datetime(row.get("write_date"))
    return bool(write_date and write_date >= recent_cutoff)


def compare_canonical_rows(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    fields: tuple[str, ...],
    recent_cutoff: datetime,
    sample_limit: int,
) -> dict:
    left, left_duplicates, left_invalid_count = _rows_by_id(left_rows)
    right, right_duplicates, right_invalid_count = _rows_by_id(right_rows)
    left_ids = set(left)
    right_ids = set(right)
    left_only = sorted(left_ids - right_ids)
    right_only = sorted(right_ids - left_ids)
    shared_ids = sorted(left_ids & right_ids)

    mismatch_counts: Counter[str] = Counter()
    stable_mismatch_counts: Counter[str] = Counter()
    mismatch_sample_ids: dict[str, list[int]] = defaultdict(list)
    for record_id in shared_ids:
        for field in fields:
            if left[record_id].get(field) == right[record_id].get(field):
                continue
            mismatch_counts[field] += 1
            if not _is_recent(right[record_id], recent_cutoff):
                stable_mismatch_counts[field] += 1
            if len(mismatch_sample_ids[field]) < sample_limit:
                mismatch_sample_ids[field].append(record_id)

    right_only_recent = [record_id for record_id in right_only if _is_recent(right[record_id], recent_cutoff)]
    right_only_stable = [record_id for record_id in right_only if record_id not in set(right_only_recent)]
    return {
        "left_count": len(left),
        "right_count": len(right),
        "shared_count": len(shared_ids),
        "left_only_count": len(left_only),
        "right_only_count": len(right_only),
        "right_only_recent_count": len(right_only_recent),
        "right_only_stable_count": len(right_only_stable),
        "left_only_sample_ids": left_only[:sample_limit],
        "right_only_sample_ids": right_only[:sample_limit],
        "right_only_stable_sample_ids": right_only_stable[:sample_limit],
        "left_duplicate_id_count": len(set(left_duplicates)),
        "right_duplicate_id_count": len(set(right_duplicates)),
        "left_invalid_id_count": left_invalid_count,
        "right_invalid_id_count": right_invalid_count,
        "field_mismatch_counts": dict(mismatch_counts),
        "stable_field_mismatch_counts": dict(stable_mismatch_counts),
        "field_mismatch_sample_ids": dict(mismatch_sample_ids),
    }


def _build_index_projection(rows: list[dict], *, sort_by_id: bool) -> dict:
    index = ExistingPodIndex()
    ordered_rows = sorted(rows, key=lambda item: _as_int(item.get("id")) or 0) if sort_by_id else rows
    for row in ordered_rows:
        index.add_row(
            tracking_number=row.get("tracking_number"),
            picking_name=row.get("picking_name"),
            pod_link=row.get("pod_link"),
            source_model="shadow_validation",
        )
    return {
        "tracking": {
            key: (value.get("picking_name") or "", value.get("pod_link") or "")
            for key, value in index.by_tracking_number.items()
        },
        "picking": {
            key: (value.get("tracking_number") or "", value.get("pod_link") or "")
            for key, value in index.by_picking_name.items()
        },
        "total_loaded": index.total_loaded,
    }


def compare_index_projections(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    sample_limit: int,
    sort_by_id: bool = True,
) -> dict:
    left = _build_index_projection(left_rows, sort_by_id=sort_by_id)
    right = _build_index_projection(right_rows, sort_by_id=sort_by_id)
    result = {"left_total_loaded": left["total_loaded"], "right_total_loaded": right["total_loaded"]}
    for label in ("tracking", "picking"):
        left_map = left[label]
        right_map = right[label]
        left_keys = set(left_map)
        right_keys = set(right_map)
        shared = left_keys & right_keys
        mapping_mismatches = sorted(key for key in shared if left_map[key] != right_map[key])
        result[label] = {
            "left_key_count": len(left_keys),
            "right_key_count": len(right_keys),
            "left_only_key_count": len(left_keys - right_keys),
            "right_only_key_count": len(right_keys - left_keys),
            "mapping_mismatch_count": len(mapping_mismatches),
            # Key values may contain customer identifiers, so only counts are persisted.
            "sample_omitted_for_redaction": bool(
                (left_keys - right_keys) or (right_keys - left_keys) or mapping_mismatches
            ),
        }
    return result


def _duplicate_key_summary(rows: list[dict]) -> dict:
    tracking_candidates: dict[str, list[tuple[int, str]]] = defaultdict(list)
    picking_candidates: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        record_id = _as_int(row.get("id")) or 0
        pod_link = _text(row.get("pod_link"))
        for token in _text(row.get("tracking_number")).split(","):
            token = token.strip()
            if token:
                tracking_candidates[token].append((record_id, pod_link))
        picking_name = _text(row.get("picking_name")).strip()
        if picking_name:
            picking_candidates[picking_name].append((record_id, pod_link))

    def summarize(candidates: dict[str, list[tuple[int, str]]]) -> dict:
        duplicate = {key: values for key, values in candidates.items() if len(values) > 1}
        conflicting = {
            key: values for key, values in duplicate.items() if len({value[1] for value in values}) > 1
        }
        return {
            "key_count": len(candidates),
            "duplicate_key_count": len(duplicate),
            "different_pod_url_key_count": len(conflicting),
        }

    return {"tracking": summarize(tracking_candidates), "picking": summarize(picking_candidates)}


def _classify_left_only_query64(
    rows: list[dict],
    *,
    target_carrier_ids: set[int],
    outgoing_type_ids: set[int],
    recent_cutoff: datetime,
) -> dict:
    recent = 0
    moved_out_of_scope = 0
    stable_in_scope = 0
    for row in rows:
        if _is_recent(row, recent_cutoff):
            recent += 1
            continue
        carrier_id = _as_int(row.get("carrier_id"))
        picking_type_id = _as_int(row.get("picking_type_id"))
        if carrier_id not in target_carrier_ids or picking_type_id not in outgoing_type_ids:
            moved_out_of_scope += 1
        else:
            stable_in_scope += 1
    return {
        "re_read_count": len(rows),
        "recent_change_count": recent,
        "moved_out_of_scope_count": moved_out_of_scope,
        "stable_still_in_scope_count": stable_in_scope,
    }


def _classify_left_only_query65(rows: list[dict], *, recent_cutoff: datetime) -> dict:
    recent = 0
    moved_out_of_scope = 0
    stable_in_scope = 0
    for row in rows:
        if _is_recent(row, recent_cutoff):
            recent += 1
            continue
        if not row.get("pod_link") or not row.get("picking_id"):
            moved_out_of_scope += 1
        else:
            stable_in_scope += 1
    return {
        "re_read_count": len(rows),
        "recent_change_count": recent,
        "moved_out_of_scope_count": moved_out_of_scope,
        "stable_still_in_scope_count": stable_in_scope,
    }


def _has_stable_difference(comparison: dict, left_only_classification: dict) -> bool:
    return bool(
        comparison.get("right_only_stable_count")
        or sum((comparison.get("stable_field_mismatch_counts") or {}).values())
        or left_only_classification.get("stable_still_in_scope_count")
        or comparison.get("left_duplicate_id_count")
        or comparison.get("right_duplicate_id_count")
        or comparison.get("left_invalid_id_count")
        or comparison.get("right_invalid_id_count")
    )


def _index_has_mapping_difference(comparison: dict) -> bool:
    return any(
        comparison[label][metric]
        for label in ("tracking", "picking")
        for metric in ("left_only_key_count", "right_only_key_count", "mapping_mismatch_count")
    )


def _picking_summary(rows: list[dict], carrier_type_by_id: dict[int, str]) -> dict:
    provider_counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    tracking_count = 0
    picking_pod_link_count = 0
    for row in rows:
        provider_counts[carrier_type_by_id.get(_as_int(row.get("carrier_id")), "unknown")] += 1
        state_counts[_text(row.get("state")) or "unknown"] += 1
        tracking_count += bool(row.get("tracking_number"))
        picking_pod_link_count += bool(row.get("pod_link"))
    return {
        "row_count": len(rows),
        "provider_counts": dict(sorted(provider_counts.items())),
        "state_counts": dict(sorted(state_counts.items())),
        "with_tracking_number_count": tracking_count,
        "with_legacy_picking_pod_link_count": picking_pod_link_count,
    }


def run_validation(
    full_cfg: dict,
    *,
    delivery_types: tuple[str, ...] = DEFAULT_DELIVERY_TYPES,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: int = 300,
    sample_limit: int = 20,
    overlap_seconds: int = 60,
    session: requests.Session | None = None,
) -> dict:
    started_at = _utc_now()
    recent_cutoff = started_at - timedelta(seconds=max(0, overlap_seconds))
    odoo_cfg = full_cfg.get("odoo") or {}
    models = connect_models(odoo_cfg)
    http = session or requests.Session()

    query64_rows, query64_transport = _fetch_querydeluxe(
        http,
        _querydeluxe_url(full_cfg, "missing_tracking_query_id"),
        timeout=timeout,
        required_headers=QUERY_REQUIRED_HEADERS["missing_tracking_query_id"],
    )
    query65_rows, query65_transport = _fetch_querydeluxe(
        http,
        _querydeluxe_url(full_cfg, "existing_pod_query_id"),
        timeout=timeout,
        required_headers=QUERY_REQUIRED_HEADERS["existing_pod_query_id"],
    )

    carrier_rows, carrier_transport = _search_read_pages(
        models,
        odoo_cfg,
        model="delivery.carrier",
        domain=[("delivery_type", "in", list(delivery_types))],
        fields=["id", "name", "delivery_type", "active"],
        page_size=page_size,
        context={"active_test": False},
    )
    carriers_by_type: dict[str, list[int]] = defaultdict(list)
    for row in carrier_rows:
        carriers_by_type[_text(row.get("delivery_type"))].append(_as_int(row.get("id")) or 0)
    target_carrier_ids = {
        record_id for record_ids in carriers_by_type.values() for record_id in record_ids if record_id
    }
    missing_delivery_types = sorted(set(delivery_types) - set(carriers_by_type))
    if missing_delivery_types:
        raise ValueError(f"No visible delivery.carrier for delivery_type: {', '.join(missing_delivery_types)}")
    carrier_names_by_id = {
        _as_int(row.get("id")): _clean_text(row.get("name"))
        for row in carrier_rows
        if _as_int(row.get("id")) is not None
    }
    carrier_type_by_id = {
        _as_int(row.get("id")): _text(row.get("delivery_type"))
        for row in carrier_rows
        if _as_int(row.get("id")) is not None
    }

    picking_type_rows, picking_type_transport = _search_read_pages(
        models,
        odoo_cfg,
        model="stock.picking.type",
        domain=[("code", "=", "outgoing")],
        fields=["id", "code"],
        page_size=page_size,
    )
    outgoing_type_ids = {_as_int(row.get("id")) for row in picking_type_rows}
    outgoing_type_ids.discard(None)

    xml_picking_rows, picking_transport = _search_read_pages(
        models,
        odoo_cfg,
        model="stock.picking",
        domain=[
            ("picking_type_id.code", "=", "outgoing"),
            ("carrier_id.delivery_type", "in", list(delivery_types)),
        ],
        fields=[
            "id",
            "name",
            "carrier_id",
            "state",
            "carrier_tracking_ref",
            "x_studio_pod_link",
            "date_done",
            "scheduled_date",
            "create_date",
            "write_date",
        ],
        page_size=page_size,
    )
    query64_target_rows = [
        _canonical_query64_row(row)
        for row in query64_rows
        if _as_int(row.get("carrier_id")) in target_carrier_ids
    ]
    canonical_pickings = [
        _canonical_picking_row(row, carrier_names_by_id) for row in xml_picking_rows
    ]
    query64_comparison = compare_canonical_rows(
        query64_target_rows,
        canonical_pickings,
        fields=(
            "name",
            "carrier_id",
            "carrier_name",
            "state",
            "business_datetime",
            "tracking_number",
            "pod_link",
        ),
        recent_cutoff=recent_cutoff,
        sample_limit=sample_limit,
    )

    query64_left_only_ids = query64_comparison["left_only_sample_ids"]
    # Re-read every left-only ID for classification, not only the persisted sample.
    q64_left_map, _, _ = _rows_by_id(query64_target_rows)
    xml_picking_map, _, _ = _rows_by_id(canonical_pickings)
    query64_left_only_ids = sorted(set(q64_left_map) - set(xml_picking_map))
    query64_left_current = _read_ids(
        models,
        odoo_cfg,
        model="stock.picking",
        ids=query64_left_only_ids,
        fields=["id", "carrier_id", "picking_type_id", "write_date"],
        batch_size=page_size,
    )
    query64_left_classification = _classify_left_only_query64(
        query64_left_current,
        target_carrier_ids=target_carrier_ids,
        outgoing_type_ids={int(value) for value in outgoing_type_ids},
        recent_cutoff=recent_cutoff,
    )
    query64_left_classification["missing_or_not_visible_count"] = (
        len(query64_left_only_ids) - len(query64_left_current)
    )

    xml_tracking_rows, tracking_transport = _search_read_pages(
        models,
        odoo_cfg,
        model="stock.tracking",
        domain=[("pod_link", "!=", False), ("picking_id", "!=", False)],
        fields=["id", "tracking_number", "pod_link", "picking_id", "write_date"],
        page_size=page_size,
    )
    picking_ids = {_as_int(row.get("picking_id")) for row in xml_tracking_rows}
    picking_ids.discard(None)
    tracking_picking_rows = _read_ids(
        models,
        odoo_cfg,
        model="stock.picking",
        ids={int(value) for value in picking_ids},
        fields=["id", "name", "carrier_tracking_ref"],
        batch_size=page_size,
    )
    tracking_pickings_by_id = {
        _as_int(row.get("id")): row for row in tracking_picking_rows if _as_int(row.get("id")) is not None
    }
    canonical_query65 = [_canonical_query65_row(row) for row in query65_rows]
    canonical_tracking = [
        _canonical_tracking_row(row, tracking_pickings_by_id) for row in xml_tracking_rows
    ]
    query65_comparison = compare_canonical_rows(
        canonical_query65,
        canonical_tracking,
        fields=("tracking_number", "picking_name", "carrier_tracking_ref", "pod_link"),
        recent_cutoff=recent_cutoff,
        sample_limit=sample_limit,
    )
    query65_left_map, _, _ = _rows_by_id(canonical_query65)
    xml_tracking_map, _, _ = _rows_by_id(canonical_tracking)
    query65_left_only_ids = sorted(set(query65_left_map) - set(xml_tracking_map))
    query65_left_current = _read_ids(
        models,
        odoo_cfg,
        model="stock.tracking",
        ids=query65_left_only_ids,
        fields=["id", "pod_link", "picking_id", "write_date"],
        batch_size=page_size,
    )
    query65_left_classification = _classify_left_only_query65(
        query65_left_current,
        recent_cutoff=recent_cutoff,
    )
    query65_left_classification["missing_or_not_visible_count"] = (
        len(query65_left_only_ids) - len(query65_left_current)
    )

    query65_index_comparison = compare_index_projections(
        canonical_query65,
        canonical_tracking,
        sample_limit=sample_limit,
    )
    query65_current_order_index_comparison = compare_index_projections(
        canonical_query65,
        canonical_tracking,
        sample_limit=sample_limit,
        sort_by_id=False,
    )
    query65_duplicate_keys = _duplicate_key_summary(canonical_tracking)

    q64_stable_diff = _has_stable_difference(query64_comparison, query64_left_classification)
    q65_stable_diff = _has_stable_difference(query65_comparison, query65_left_classification)
    missing_or_hidden = bool(
        query64_left_classification["missing_or_not_visible_count"]
        or query65_left_classification["missing_or_not_visible_count"]
    )
    current_order_index_diff = _index_has_mapping_difference(
        query65_current_order_index_comparison
    )
    deterministic_index_diff = _index_has_mapping_difference(query65_index_comparison)
    duplicate_url_risk = bool(
        query65_duplicate_keys["tracking"]["different_pod_url_key_count"]
        or query65_duplicate_keys["picking"]["different_pod_url_key_count"]
    )
    has_concurrent_differences = bool(
        query64_comparison["left_only_count"]
        or query64_comparison["right_only_count"]
        or sum(query64_comparison["field_mismatch_counts"].values())
        or query65_comparison["left_only_count"]
        or query65_comparison["right_only_count"]
        or sum(query65_comparison["field_mismatch_counts"].values())
    )
    status = "pass"
    if q64_stable_diff or q65_stable_diff or missing_or_hidden:
        status = "differences_found"
    elif has_concurrent_differences:
        status = "inconclusive_concurrent_changes"
    elif current_order_index_diff or deterministic_index_diff:
        status = "differences_found"
    elif duplicate_url_risk:
        status = "inconclusive_duplicate_url_conflicts"

    finished_at = _utc_now()
    return {
        "status": status,
        "read_only": True,
        "started_at": _iso_datetime(started_at),
        "finished_at": _iso_datetime(finished_at),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "recent_change_cutoff": _iso_datetime(recent_cutoff),
        "delivery_types": list(delivery_types),
        "query_ids": dict(EXPECTED_QUERY_IDS),
        "carrier_ids_by_delivery_type": {
            key: sorted(value) for key, value in sorted(carriers_by_type.items())
        },
        "query64_pickings": {
            "transport": query64_transport,
            "xmlrpc_transport": picking_transport,
            "comparison": query64_comparison,
            "querydeluxe_summary": _picking_summary(
                query64_target_rows, carrier_type_by_id
            ),
            "xmlrpc_summary": _picking_summary(canonical_pickings, carrier_type_by_id),
            "left_only_classification": query64_left_classification,
        },
        "query65_tracking": {
            "transport": query65_transport,
            "xmlrpc_transport": tracking_transport,
            "comparison": query65_comparison,
            "deterministic_index_comparison": query65_index_comparison,
            "current_order_index_comparison": query65_current_order_index_comparison,
            "xmlrpc_duplicate_keys": query65_duplicate_keys,
            "left_only_classification": query65_left_classification,
        },
        "supporting_transport": {
            "delivery_carrier": carrier_transport,
            "stock_picking_type": picking_type_transport,
            "tracking_picking_read_count": len(tracking_picking_rows),
        },
        "safety": {
            "xmlrpc_method_allowlist": sorted(READ_ONLY_METHODS),
            "persisted_urls": False,
            "persisted_credentials": False,
            "sample_values": "record_ids_only",
            "concurrent_changes_are_success": False,
        },
    }


def _default_output_path(started_at: datetime | None = None) -> Path:
    timestamp = (started_at or _utc_now()).strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "runtime" / "validation" / f"odoo_xmlrpc_migration_{timestamp}.json"


def _save_report(path: Path, report: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only QueryDeluxe 64/65 versus Odoo XML-RPC shadow validation"
    )
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument(
        "--delivery-types",
        default=",".join(DEFAULT_DELIVERY_TYPES),
        help="Comma-separated delivery.carrier delivery_type values",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--overlap-seconds", type=int, default=60)
    parser.add_argument("--output", default="")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    if not 100 <= args.page_size <= 5000:
        parser.error("--page-size must be between 100 and 5000")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not 0 <= args.sample_limit <= 100:
        parser.error("--sample-limit must be between 0 and 100")
    if not 0 <= args.overlap_seconds <= 300:
        parser.error("--overlap-seconds must be between 0 and 300")

    delivery_types = tuple(
        dict.fromkeys(item.strip() for item in args.delivery_types.split(",") if item.strip())
    )
    if not delivery_types:
        parser.error("--delivery-types must not be empty")

    report = run_validation(
        _load_config(args.config),
        delivery_types=delivery_types,
        page_size=args.page_size,
        timeout=args.timeout,
        sample_limit=args.sample_limit,
        overlap_seconds=args.overlap_seconds,
    )
    if not args.no_save:
        output_path = Path(args.output).expanduser() if args.output else _default_output_path()
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        output_path = output_path.resolve()
        report["report_path"] = str(output_path)
        _save_report(output_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
