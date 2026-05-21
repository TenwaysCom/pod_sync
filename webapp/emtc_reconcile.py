from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from .actions import resolve_storage_dir
from .odoo_missing_report import SNAPSHOT_PATH as ODOO_SNAPSHOT_PATH, load_odoo_missing_snapshot
from .phase1a_runs import get_latest_phase1a_run, resolve_artifact_path
from .xlsx_utils import build_xlsx_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POD_INDEX_PATH = PROJECT_ROOT / "runtime" / "odoo_push" / "odoo_pod_index.json"

RECONCILE_SOURCE_CONFIGS = {
    "emtc": {
        "slug": "emtc",
        "label": "EMTC",
        "carrier_id": 24,
        "source_name": "EMTC",
        "match_key_type": "tracking_number",
        "match_key_label": "tracking number",
        "odoo_total_label": "EMTC 出库单总数",
        "odoo_with_key_label": "Odoo 有 tracking number",
        "odoo_with_key_hint": "只有这些单据能继续和 POD 文件比对",
        "odoo_match_label": "Odoo 命中 POD",
        "odoo_match_hint": "同 tracking number 已在最新 POD 识别里找到文件",
        "odoo_missing_label": "Odoo 未命中 POD",
        "odoo_missing_hint": "有 tracking number，但还没在 POD 中看到",
        "odoo_without_key_label": "Odoo 没有 tracking number",
        "odoo_without_key_hint": "Odoo 单据缺少 tracking number，暂时不能进入自动对账。",
        "pod_total_label": "EMTC POD 文件总数",
        "pod_parseable_label": "POD 可解析 tracking number",
        "pod_parseable_hint": "已经能从文件名里稳定提取出 tracking number",
        "pod_found_label": "POD 可在 Odoo 找到",
        "pod_found_hint": "同 tracking number 已存在于 EMTC Odoo 单池、Odoo POD 索引，或 Odoo 其他承运商单据中",
        "pod_missing_label": "POD 未命中 Odoo",
        "pod_missing_hint": "tracking number 已解析，但在 Odoo 总明细和 POD 索引里都没找到",
        "pod_unparseable_label": "POD 解析不了匹配键",
        "pod_unparseable_hint": "文件存在，但当前规则还没法稳定提取匹配键。",
        "odoo_heading": "EMTC 出库单和 tracking number，能不能在 POD 里找到",
        "pod_heading": "EMTC POD 文件，能不能解析出 tracking number，并在 Odoo 中找到",
        "odoo_search_placeholder": "出库单、tracking number、POD 文件名、POD 路径",
        "pod_search_placeholder": "tracking number、文件名、路径、Odoo 单号",
    },
    "cmc": {
        "slug": "cmc",
        "label": "CMC",
        "carrier_id": 25,
        "source_name": "CMC",
        "match_key_type": "picking_name",
        "match_key_label": "出库单号",
        "odoo_total_label": "CMC 出库单总数",
        "odoo_with_key_label": "Odoo 有出库单号",
        "odoo_with_key_hint": "只有这些单据能继续和 POD 文件比对",
        "odoo_match_label": "Odoo 命中 POD",
        "odoo_match_hint": "同出库单号已在最新 POD 识别里找到文件",
        "odoo_missing_label": "Odoo 未命中 POD",
        "odoo_missing_hint": "有出库单号，但还没在 POD 中看到",
        "odoo_without_key_label": "Odoo 没有出库单号",
        "odoo_without_key_hint": "Odoo 单据缺少出库单号，暂时不能进入自动对账。",
        "pod_total_label": "CMC POD 文件总数",
        "pod_parseable_label": "POD 可解析出库单号",
        "pod_parseable_hint": "已经能从文件名里稳定提取出出库单号",
        "pod_found_label": "POD 可在 Odoo 找到",
        "pod_found_hint": "同出库单号已存在于 CMC Odoo 单池、Odoo POD 索引，或 Odoo 其他承运商单据中",
        "pod_missing_label": "POD 未命中 Odoo",
        "pod_missing_hint": "出库单号已解析，但在 Odoo 总明细和 POD 索引里都没找到",
        "pod_unparseable_label": "POD 解析不了匹配键",
        "pod_unparseable_hint": "文件存在，但当前规则还没法稳定提取匹配键。",
        "odoo_heading": "CMC 出库单和出库单号，能不能在 POD 里找到",
        "pod_heading": "CMC POD 文件，能不能解析出出库单号，并在 Odoo 中找到",
        "odoo_search_placeholder": "出库单、POD 文件名、POD 路径",
        "pod_search_placeholder": "出库单号、文件名、路径、Odoo 单号",
    },
}

DEFAULT_RECONCILE_KIND = "emtc"

ODOO_TO_POD_STATUS_ORDER = {
    "matched_pod": 0,
    "pod_not_found": 1,
    "no_match_key": 2,
}

POD_TO_ODOO_STATUS_ORDER = {
    "found_in_odoo": 0,
    "found_in_odoo_pod_index": 1,
    "found_other_carrier": 2,
    "not_found_in_odoo": 3,
    "no_match_key": 4,
}

POD_FOUND_IN_ODOO_STATUSES = {
    "found_in_odoo",
    "found_in_odoo_pod_index",
    "found_other_carrier",
}


def _canonicalize(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _normalize_tracking(value):
    normalized = _canonicalize(value)
    return normalized or None


def _normalize_picking_name(value):
    normalized = _canonicalize(value)
    return normalized or None


def _normalize_match_key(value, match_key_type: str):
    if match_key_type == "tracking_number":
        return _normalize_tracking(value)
    return _normalize_picking_name(value)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _pod_index_key_sources(config):
    if config["match_key_type"] == "tracking_number":
        return ("by_tracking_number",)
    return ("by_picking_name", "by_tracking_number")


def _dedupe_pod_index_entries(entries):
    seen = set()
    deduped = []
    for entry in entries:
        identity = (
            entry.get("picking_name") or "",
            entry.get("tracking_number") or "",
            entry.get("pod_link") or "",
            entry.get("index_key") or "",
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(entry)
    return deduped


def _load_pod_index_by_key(config):
    if not POD_INDEX_PATH.exists():
        return {}, {}
    try:
        index = _read_json(POD_INDEX_PATH) or {}
    except (json.JSONDecodeError, OSError):
        return {}, {}

    entries_by_key = defaultdict(list)
    for source_key in _pod_index_key_sources(config):
        for raw_key, raw_entry in (index.get(source_key) or {}).items():
            normalized_key = _normalize_match_key(raw_key, config["match_key_type"])
            if not normalized_key:
                continue
            entry = dict(raw_entry) if isinstance(raw_entry, dict) else {}
            entry["index_key"] = raw_key
            entry["index_source"] = source_key
            if source_key == "by_picking_name" and not entry.get("picking_name"):
                entry["picking_name"] = raw_key
            if source_key == "by_tracking_number" and not entry.get("tracking_number"):
                entry["tracking_number"] = raw_key
            entries_by_key[normalized_key].append(entry)

    return (
        {key: _dedupe_pod_index_entries(entries) for key, entries in entries_by_key.items()},
        {
            "fetched_at": index.get("fetched_at"),
            "updated_at": index.get("updated_at"),
            "total_loaded": index.get("total_loaded"),
        },
    )


def _join_index_values(entries, *field_names):
    values = []
    seen = set()
    for entry in entries:
        for field_name in field_names:
            value = entry.get(field_name)
            if value in (None, ""):
                continue
            text = str(value)
            if text in seen:
                continue
            seen.add(text)
            values.append(text)
            break
    return " | ".join(values)


def _file_signature(path: Path | None):
    if not path or not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _parse_datetime(value: str | None):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _resolve_tracking_number(row):
    tracking_number = row.get("tracking_number")
    if isinstance(tracking_number, str):
        tracking_number = tracking_number.strip() or None
    return tracking_number or row.get("carrier_tracking_ref")


def _resolve_reconcile_source(kind: str | None):
    normalized = (kind or DEFAULT_RECONCILE_KIND).strip().lower()
    return RECONCILE_SOURCE_CONFIGS.get(normalized, RECONCILE_SOURCE_CONFIGS[DEFAULT_RECONCILE_KIND])


def list_reconcile_sources():
    return [RECONCILE_SOURCE_CONFIGS["emtc"], RECONCILE_SOURCE_CONFIGS["cmc"]]


def _status_labels(config):
    key_label = config["match_key_label"]
    return {
        "odoo_to_pod": {
            "matched_pod": "命中 POD",
            "pod_not_found": "未命中 POD",
            "no_match_key": f"缺少{key_label}",
        },
        "pod_to_odoo": {
            "found_in_odoo": "在 Odoo 中找到",
            "found_in_odoo_pod_index": "已回传 Odoo POD",
            "found_other_carrier": "命中其他承运商",
            "not_found_in_odoo": "在 Odoo 中未找到",
            "no_match_key": f"无法解析{key_label}",
        },
    }


def _local_file_context(local_path: str | None, storage_root: Path):
    if not local_path:
        return {"local_relative_path": None, "local_file_href": None}
    path = Path(local_path).resolve()
    try:
        relative = path.relative_to(storage_root).as_posix()
    except ValueError:
        return {"local_relative_path": str(path), "local_file_href": None}
    return {
        "local_relative_path": relative,
        "local_file_href": f"/storage/files/{quote(relative, safe='/')}",
    }


def _build_sample_document(row, storage_root: Path):
    local_context = _local_file_context(row.get("local_path"), storage_root)
    return {
        "file_name": row.get("file_name"),
        "source_path": row.get("source_path"),
        "file_url": row.get("file_url"),
        "local_relative_path": local_context["local_relative_path"],
        "local_file_href": local_context["local_file_href"],
    }


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _matches_source_name(value: str | None, expected: str):
    return _canonicalize(value).startswith(_canonicalize(expected))


def _extract_odoo_match_key(row, config):
    if config["match_key_type"] == "tracking_number":
        raw_value = _resolve_tracking_number(row)
    else:
        raw_value = row.get("picking_name")
    return raw_value, _normalize_match_key(raw_value, config["match_key_type"])


def _extract_pod_match_key(row, config):
    preferred_key = row.get("preferred_key")
    preferred_key_type = row.get("preferred_key_type")
    raw_value = None

    if preferred_key_type == config["match_key_type"] and preferred_key:
        raw_value = preferred_key
    elif config["match_key_type"] == "tracking_number":
        raw_value = row.get("primary_tracking_number")
    else:
        raw_value = row.get("primary_picking_name")

    return raw_value, _normalize_match_key(raw_value, config["match_key_type"])


def _timestamp_sort_value(value: str | None):
    parsed = _parse_datetime(value)
    if not parsed:
        return 0
    return int(parsed.timestamp())


def _normalize_start_month(value: str | None):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        return ""
    year, month = text.split("-")
    try:
        year_int = int(year)
        month_int = int(month)
    except ValueError:
        return ""
    if month_int < 1 or month_int > 12:
        return ""
    return f"{year_int:04d}-{month_int:02d}"


def _month_bucket(value: str | None):
    parsed = _parse_datetime(value)
    if not parsed:
        return None
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _month_label(value: str):
    year, month = value.split("-")
    return f"{int(year)} 年 {int(month)} 月"


def _row_on_or_after_month(value: str | None, start_month: str):
    normalized_start = _normalize_start_month(start_month)
    if not normalized_start:
        return True
    bucket = _month_bucket(value)
    if not bucket:
        return False
    return bucket >= normalized_start


def _share_text(numerator: int, denominator: int, scope_label: str):
    if denominator <= 0:
        pct = 0.0
    else:
        pct = (numerator / denominator) * 100.0
    return f"占{scope_label} {pct:.1f}%"


def _rebuild_summary(filtered_dataset):
    odoo_rows = filtered_dataset["odoo_to_pod_rows"]
    pod_rows = filtered_dataset["pod_to_odoo_rows"]
    odoo_total = len(odoo_rows)
    odoo_with_key = sum(1 for row in odoo_rows if row.get("compare_status") != "no_match_key")
    odoo_matched = sum(1 for row in odoo_rows if row.get("compare_status") == "matched_pod")
    odoo_without_pod = sum(1 for row in odoo_rows if row.get("compare_status") == "pod_not_found")
    odoo_without_key = sum(1 for row in odoo_rows if row.get("compare_status") == "no_match_key")
    filtered_dataset["odoo_summary"] = {
        "total_count": odoo_total,
        "total_share_text": _share_text(odoo_total, odoo_total, "当前 Odoo 单池"),
        "with_match_key_count": odoo_with_key,
        "with_match_key_share_text": _share_text(odoo_with_key, odoo_total, "当前 Odoo 单池"),
        "matched_pod_count": odoo_matched,
        "matched_pod_share_text": _share_text(odoo_matched, odoo_with_key, "Odoo 有匹配键单数"),
        "without_pod_count": odoo_without_pod,
        "without_pod_share_text": _share_text(odoo_without_pod, odoo_with_key, "Odoo 有匹配键单数"),
        "without_match_key_count": odoo_without_key,
        "without_match_key_share_text": _share_text(odoo_without_key, odoo_total, "当前 Odoo 单池"),
    }
    pod_total = len(pod_rows)
    pod_parseable = sum(1 for row in pod_rows if row.get("compare_status") != "no_match_key")
    pod_found = sum(1 for row in pod_rows if row.get("compare_status") in POD_FOUND_IN_ODOO_STATUSES)
    pod_not_found = sum(1 for row in pod_rows if row.get("compare_status") == "not_found_in_odoo")
    pod_unparseable = sum(1 for row in pod_rows if row.get("compare_status") == "no_match_key")
    filtered_dataset["pod_summary"] = {
        "total_count": pod_total,
        "total_share_text": _share_text(pod_total, pod_total, "当前 POD 文件池"),
        "parseable_count": pod_parseable,
        "parseable_share_text": _share_text(pod_parseable, pod_total, "当前 POD 文件池"),
        "found_in_odoo_count": pod_found,
        "found_in_odoo_share_text": _share_text(pod_found, pod_parseable, "可解析 POD 文件"),
        "not_found_in_odoo_count": pod_not_found,
        "not_found_in_odoo_share_text": _share_text(pod_not_found, pod_parseable, "可解析 POD 文件"),
        "unparseable_count": pod_unparseable,
        "unparseable_share_text": _share_text(pod_unparseable, pod_total, "当前 POD 文件池"),
    }
    return filtered_dataset


def _collect_start_month_options(dataset):
    months = set()
    for row in dataset.get("_odoo_rows", []):
        bucket = _month_bucket(row.get("business_datetime"))
        if bucket:
            months.add(bucket)
    ordered = sorted(months)
    return [{"value": value, "label": _month_label(value)} for value in ordered]


def _build_dataset_view(
    config,
    odoo_rows,
    pod_docs,
    *,
    generated_at: str | None,
    run_name: str,
    phase1a_generated_at: str | None,
    all_odoo_rows=None,
    pod_index_by_key=None,
    pod_index_meta=None,
):
    labels = _status_labels(config)
    pod_docs_by_key = defaultdict(list)
    for row in pod_docs:
        normalized_key = row.get("normalized_match_key")
        if normalized_key:
            pod_docs_by_key[normalized_key].append(row)

    odoo_to_pod_rows = []
    for row in odoo_rows:
        raw_key = row.get("match_key")
        normalized_key = row.get("normalized_match_key")
        pod_matches = pod_docs_by_key.get(normalized_key, []) if normalized_key else []
        if not normalized_key:
            compare_status = "no_match_key"
        elif pod_matches:
            compare_status = "matched_pod"
        else:
            compare_status = "pod_not_found"
        odoo_to_pod_rows.append(
            {
                "odoo_picking_id": row.get("odoo_picking_id"),
                "picking_name": row.get("picking_name"),
                "state": row.get("state"),
                "business_datetime": row.get("business_datetime"),
                "match_key": raw_key,
                "compare_status": compare_status,
                "compare_status_label": labels["odoo_to_pod"][compare_status],
                "pod_file_count": len(pod_matches),
                "sample_pod_files": " | ".join(
                    doc.get("file_name") or "" for doc in pod_matches[:10] if doc.get("file_name")
                ),
                "sample_pod_paths": " | ".join(
                    doc.get("source_path") or "" for doc in pod_matches[:10] if doc.get("source_path")
                ),
                "sample_documents": [doc.get("sample_document") for doc in pod_matches[:5] if doc.get("sample_document")],
                "search_blob": " ".join(
                    str(value or "")
                    for value in (
                        row.get("picking_name"),
                        raw_key,
                        row.get("state"),
                        " ".join(doc.get("file_name") or "" for doc in pod_matches[:10]),
                        " ".join(doc.get("source_path") or "" for doc in pod_matches[:10]),
                    )
                ).lower(),
            }
        )

    filtered_odoo_by_key = defaultdict(list)
    for row in odoo_rows:
        normalized_key = row.get("normalized_match_key")
        if normalized_key:
            filtered_odoo_by_key[normalized_key].append(row)

    all_odoo_by_key = defaultdict(list)
    for row in all_odoo_rows or odoo_rows:
        normalized_key = row.get("normalized_match_key")
        if normalized_key:
            all_odoo_by_key[normalized_key].append(row)

    pod_index_by_key = pod_index_by_key or {}
    pod_to_odoo_rows = []
    for row in pod_docs:
        normalized_key = row.get("normalized_match_key")
        matches = filtered_odoo_by_key.get(normalized_key, []) if normalized_key else []
        all_matches = all_odoo_by_key.get(normalized_key, []) if normalized_key else []
        other_carrier_matches = [
            match for match in all_matches
            if match.get("carrier_id") != config["carrier_id"]
        ]
        pod_index_matches = pod_index_by_key.get(normalized_key, []) if normalized_key else []
        if not normalized_key:
            status = "no_match_key"
        elif matches:
            status = "found_in_odoo"
        elif pod_index_matches:
            status = "found_in_odoo_pod_index"
        elif other_carrier_matches:
            status = "found_other_carrier"
        else:
            status = "not_found_in_odoo"

        display_matches = matches or other_carrier_matches
        matched_count = len(display_matches)
        if not matched_count and pod_index_matches:
            matched_count = len(pod_index_matches)
        matched_names = " | ".join(
            str(match.get("picking_name") or "")
            for match in display_matches[:10]
            if match.get("picking_name")
        )
        matched_states = " | ".join(
            str(match.get("state") or "")
            for match in display_matches[:10]
            if match.get("state")
        )
        if not matched_names and pod_index_matches:
            matched_names = _join_index_values(pod_index_matches[:10], "picking_name", "index_key", "tracking_number")
        if not matched_states and pod_index_matches:
            matched_states = _join_index_values(pod_index_matches[:10], "source_model") or "stock.tracking"
        pod_to_odoo_rows.append(
            {
                "document_uid": row.get("document_uid"),
                "file_name": row.get("file_name"),
                "source_path": row.get("source_path"),
                "source_modified_at": row.get("source_modified_at"),
                "match_key": row.get("match_key"),
                "normalized_match_key": normalized_key,
                "compare_status": status,
                "compare_status_label": labels["pod_to_odoo"][status],
                "matched_odoo_count": matched_count,
                "matched_odoo_picking_names": matched_names,
                "matched_odoo_states": matched_states,
                "odoo_pod_index_count": len(pod_index_matches),
                "odoo_pod_index_links": _join_index_values(pod_index_matches[:10], "pod_link"),
                "sample_document": row.get("sample_document"),
                "reason_code": row.get("reason_code"),
                "search_blob": " ".join(
                    str(value or "")
                    for value in (
                        row.get("file_name"),
                        row.get("source_path"),
                        row.get("match_key"),
                        " ".join(str(match.get("picking_name") or "") for match in display_matches[:10]),
                        " ".join(str(match.get("state") or "") for match in display_matches[:10]),
                        " ".join(str(entry.get("pod_link") or "") for entry in pod_index_matches[:10]),
                        " ".join(str(entry.get("index_key") or "") for entry in pod_index_matches[:10]),
                    )
                ).lower(),
            }
        )

    odoo_with_key_count = sum(1 for row in odoo_rows if row.get("normalized_match_key"))
    odoo_matched_pod_count = sum(1 for row in odoo_to_pod_rows if row.get("compare_status") == "matched_pod")
    pod_parseable_count = sum(1 for row in pod_docs if row.get("normalized_match_key"))
    pod_found_in_odoo_count = sum(
        1 for row in pod_to_odoo_rows
        if row.get("compare_status") in POD_FOUND_IN_ODOO_STATUSES
    )
    odoo_total = len(odoo_rows)
    pod_total = len(pod_docs)

    odoo_to_pod_rows.sort(
        key=lambda row: (
            ODOO_TO_POD_STATUS_ORDER.get(row.get("compare_status"), 99),
            -_timestamp_sort_value(row.get("business_datetime")),
            -_safe_int(row.get("odoo_picking_id")),
        )
    )
    pod_to_odoo_rows.sort(
        key=lambda row: (
            POD_TO_ODOO_STATUS_ORDER.get(row.get("compare_status"), 99),
            -_timestamp_sort_value(row.get("source_modified_at")),
            row.get("file_name") or "",
        )
    )

    return {
        "generated_at": generated_at,
        "phase1a_run_name": run_name,
        "phase1a_generated_at": phase1a_generated_at,
        "source_config": config,
        "_odoo_rows": odoo_rows,
        "_all_odoo_rows": list(all_odoo_rows or odoo_rows),
        "_pod_docs": pod_docs,
        "_pod_index_by_key": pod_index_by_key,
        "_pod_index_meta": pod_index_meta or {},
        "odoo_pod_index_fetched_at": (pod_index_meta or {}).get("fetched_at"),
        "odoo_pod_index_updated_at": (pod_index_meta or {}).get("updated_at"),
        "odoo_summary": {
            "total_count": odoo_total,
            "total_share_text": _share_text(odoo_total, odoo_total, "当前 Odoo 单池"),
            "with_match_key_count": odoo_with_key_count,
            "with_match_key_share_text": _share_text(odoo_with_key_count, odoo_total, "当前 Odoo 单池"),
            "matched_pod_count": odoo_matched_pod_count,
            "matched_pod_share_text": _share_text(odoo_matched_pod_count, odoo_with_key_count, "Odoo 有匹配键单数"),
            "without_pod_count": odoo_with_key_count - odoo_matched_pod_count,
            "without_pod_share_text": _share_text(odoo_with_key_count - odoo_matched_pod_count, odoo_with_key_count, "Odoo 有匹配键单数"),
            "without_match_key_count": odoo_total - odoo_with_key_count,
            "without_match_key_share_text": _share_text(odoo_total - odoo_with_key_count, odoo_total, "当前 Odoo 单池"),
        },
        "pod_summary": {
            "total_count": pod_total,
            "total_share_text": _share_text(pod_total, pod_total, "当前 POD 文件池"),
            "parseable_count": pod_parseable_count,
            "parseable_share_text": _share_text(pod_parseable_count, pod_total, "当前 POD 文件池"),
            "found_in_odoo_count": pod_found_in_odoo_count,
            "found_in_odoo_share_text": _share_text(pod_found_in_odoo_count, pod_parseable_count, "可解析 POD 文件"),
            "not_found_in_odoo_count": pod_parseable_count - pod_found_in_odoo_count,
            "not_found_in_odoo_share_text": _share_text(pod_parseable_count - pod_found_in_odoo_count, pod_parseable_count, "可解析 POD 文件"),
            "unparseable_count": pod_total - pod_parseable_count,
            "unparseable_share_text": _share_text(pod_total - pod_parseable_count, pod_total, "当前 POD 文件池"),
        },
        "odoo_to_pod_rows": odoo_to_pod_rows,
        "pod_to_odoo_rows": pod_to_odoo_rows,
    }


def _apply_start_month_filter(dataset, start_month: str | None):
    normalized_start = _normalize_start_month(start_month)
    odoo_rows = dataset.get("_odoo_rows", [])
    all_odoo_rows = dataset.get("_all_odoo_rows", odoo_rows)
    pod_docs = dataset.get("_pod_docs", [])
    if normalized_start:
        odoo_rows = [
            row for row in odoo_rows
            if _row_on_or_after_month(row.get("business_datetime"), normalized_start)
        ]
        all_odoo_rows = [
            row for row in all_odoo_rows
            if _row_on_or_after_month(row.get("business_datetime"), normalized_start)
        ]
    filtered = _build_dataset_view(
        dataset["source_config"],
        odoo_rows,
        pod_docs,
        generated_at=dataset.get("generated_at"),
        run_name=dataset.get("phase1a_run_name"),
        phase1a_generated_at=dataset.get("phase1a_generated_at"),
        all_odoo_rows=all_odoo_rows,
        pod_index_by_key=dataset.get("_pod_index_by_key"),
        pod_index_meta=dataset.get("_pod_index_meta"),
    )
    filtered["selected_start_month"] = normalized_start
    filtered["start_month_options"] = _collect_start_month_options(dataset)
    return filtered


@lru_cache(maxsize=8)
def _load_reconcile_dataset_cached(
    source_slug: str,
    run_name: str,
    parsed_signature: tuple[int, int],
    odoo_signature: tuple[int, int],
    pod_index_signature: tuple[int, int],
    storage_root: str,
):
    config = _resolve_reconcile_source(source_slug)
    run = get_latest_phase1a_run()
    if not run or run["run_name"] != run_name:
        return None

    parsed_path = resolve_artifact_path(run_name, "parsed_documents_json")
    if not parsed_path or not parsed_path.exists():
        return None

    odoo_snapshot = load_odoo_missing_snapshot()
    if not odoo_snapshot:
        return None

    parsed_rows = list(_read_json(parsed_path) or [])
    storage_dir = Path(storage_root)
    pod_index_by_key, pod_index_meta = _load_pod_index_by_key(config)
    labels = _status_labels(config)
    odoo_rows = []
    all_odoo_rows = []
    odoo_by_key = defaultdict(list)
    for row in odoo_snapshot.get("total_detail_rows") or []:
        raw_key, normalized_key = _extract_odoo_match_key(row, config)
        normalized_row = dict(row)
        normalized_row["match_key"] = raw_key
        normalized_row["normalized_match_key"] = normalized_key
        all_odoo_rows.append(normalized_row)
        if row.get("carrier_id") == config["carrier_id"]:
            odoo_rows.append(normalized_row)
            if normalized_key:
                odoo_by_key[normalized_key].append(normalized_row)

    pod_docs = []
    for row in parsed_rows:
        if not _matches_source_name(row.get("source_name"), config["source_name"]):
            continue
        raw_key, normalized_key = _extract_pod_match_key(row, config)
        matches = odoo_by_key.get(normalized_key, []) if normalized_key else []
        status = (
            "found_in_odoo"
            if normalized_key and matches
            else ("not_found_in_odoo" if normalized_key else "no_match_key")
        )
        item = {
            "document_uid": row.get("document_uid"),
            "file_name": row.get("file_name"),
            "source_path": row.get("source_path"),
            "source_modified_at": row.get("source_modified_at"),
            "match_key": raw_key,
            "normalized_match_key": normalized_key,
            "compare_status": status,
            "compare_status_label": labels["pod_to_odoo"][status],
            "reason_code": row.get("reason_code"),
            "sample_document": _build_sample_document(row, storage_dir),
            "matched_odoo_count": len(matches),
            "matched_odoo_picking_names": " | ".join(
                str(match.get("picking_name") or "")
                for match in matches[:10]
                if match.get("picking_name")
            ),
            "matched_odoo_states": " | ".join(
                str(match.get("state") or "")
                for match in matches[:10]
                if match.get("state")
            ),
            "search_blob": " ".join(
                str(value or "")
                for value in (
                    row.get("file_name"),
                    row.get("source_path"),
                    raw_key,
                    " ".join(str(match.get("picking_name") or "") for match in matches[:10]),
                    " ".join(str(match.get("state") or "") for match in matches[:10]),
                )
            ).lower(),
        }
        pod_docs.append(item)

    return _build_dataset_view(
        config,
        odoo_rows,
        pod_docs,
        generated_at=odoo_snapshot.get("generated_at"),
        run_name=run_name,
        phase1a_generated_at=run.get("summary", {}).get("generated_at"),
        all_odoo_rows=all_odoo_rows,
        pod_index_by_key=pod_index_by_key,
        pod_index_meta=pod_index_meta,
    )


def load_reconcile_dataset(kind: str = DEFAULT_RECONCILE_KIND):
    source_config = _resolve_reconcile_source(kind)
    run = get_latest_phase1a_run()
    if not run:
        return None
    parsed_path = resolve_artifact_path(run["run_name"], "parsed_documents_json")
    if not parsed_path:
        return None
    return _load_reconcile_dataset_cached(
        source_config["slug"],
        run["run_name"],
        _file_signature(parsed_path),
        _file_signature(ODOO_SNAPSHOT_PATH),
        _file_signature(POD_INDEX_PATH),
        str(resolve_storage_dir()),
    )


def load_filtered_reconcile_dataset(kind: str = DEFAULT_RECONCILE_KIND, start_month: str = ""):
    dataset = load_reconcile_dataset(kind)
    if not dataset:
        return None
    return _apply_start_month_filter(dataset, start_month)


def _paginate(rows, *, page: int, page_size: int):
    safe_page_size = max(1, min(int(page_size or 25), 200))
    safe_page = max(1, int(page or 1))
    total = len(rows)
    total_pages = max(1, math.ceil(total / safe_page_size))
    if safe_page > total_pages:
        safe_page = total_pages
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "items": rows[start:end],
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "page_start": start + 1 if total else 0,
        "page_end": min(end, total),
    }


def build_reconcile_odoo_to_pod_listing(
    *,
    kind: str = DEFAULT_RECONCILE_KIND,
    start_month: str = "",
    query: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 25,
):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    query_text = (query or "").strip().lower()
    rows = []
    for row in dataset["odoo_to_pod_rows"]:
        if query_text and query_text not in row["search_blob"]:
            continue
        if status and row.get("compare_status") != status:
            continue
        rows.append(row)
    config = dataset["source_config"]
    payload = _paginate(rows, page=page, page_size=page_size)
    payload.update(
        {
            "generated_at": dataset["generated_at"],
            "kind": config["slug"],
            "filters": {"query": query, "status": status, "start_month": dataset.get("selected_start_month") or ""},
            "status_options": [
                {"value": "", "label": "全部状态"},
                {"value": "matched_pod", "label": "命中 POD"},
                {"value": "pod_not_found", "label": "未命中 POD"},
                {"value": "no_match_key", "label": f"缺少{config['match_key_label']}"},
            ],
        }
    )
    return payload


def build_reconcile_pod_to_odoo_listing(
    *,
    kind: str = DEFAULT_RECONCILE_KIND,
    start_month: str = "",
    query: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 25,
):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    query_text = (query or "").strip().lower()
    rows = []
    for row in dataset["pod_to_odoo_rows"]:
        if query_text and query_text not in row["search_blob"]:
            continue
        if status and row.get("compare_status") != status:
            continue
        rows.append(row)
    config = dataset["source_config"]
    payload = _paginate(rows, page=page, page_size=page_size)
    payload.update(
        {
            "generated_at": dataset["generated_at"],
            "kind": config["slug"],
            "filters": {"query": query, "status": status, "start_month": dataset.get("selected_start_month") or ""},
            "status_options": [
                {"value": "", "label": "全部状态"},
                {"value": "found_in_odoo", "label": "在 Odoo 中找到"},
                {"value": "found_in_odoo_pod_index", "label": "已回传 Odoo POD"},
                {"value": "found_other_carrier", "label": "命中其他承运商"},
                {"value": "not_found_in_odoo", "label": "在 Odoo 中未找到"},
                {"value": "no_match_key", "label": f"无法解析{config['match_key_label']}"},
            ],
        }
    )
    return payload


def _join_values(values):
    return "\n".join(str(value) for value in values if value not in (None, ""))


def _join_sample_documents(sample_documents, field_name):
    return _join_values(doc.get(field_name) for doc in (sample_documents or []))


def _export_sheet_rows(dataset):
    config = dataset["source_config"]

    odoo_headers = [
        "odoo_picking_id",
        "picking_name",
        "state",
        "business_datetime",
        config["match_key_label"],
        "pod_file_count",
        "sample_pod_files",
        "sample_pod_paths",
        "source_file_urls",
        "local_file_urls",
    ]
    pod_headers = [
        "document_uid",
        "file_name",
        "source_path",
        "source_modified_at",
        config["match_key_label"],
        "compare_status_label",
        "matched_odoo_count",
        "matched_odoo_picking_names",
        "matched_odoo_states",
        "odoo_pod_index_count",
        "odoo_pod_index_links",
        "source_file_url",
        "local_file_url",
        "reason_code",
    ]

    def odoo_rows(compare_status):
        rows = [odoo_headers]
        for row in dataset["odoo_to_pod_rows"]:
            if row.get("compare_status") != compare_status:
                continue
            rows.append(
                [
                    row.get("odoo_picking_id"),
                    row.get("picking_name"),
                    row.get("state"),
                    row.get("business_datetime"),
                    row.get("match_key"),
                    row.get("pod_file_count"),
                    row.get("sample_pod_files"),
                    row.get("sample_pod_paths"),
                    _join_sample_documents(row.get("sample_documents"), "file_url"),
                    _join_sample_documents(row.get("sample_documents"), "local_file_href"),
                ]
            )
        return rows

    def pod_rows(compare_status):
        compare_statuses = (
            set(compare_status)
            if isinstance(compare_status, (set, tuple, list))
            else {compare_status}
        )
        rows = [pod_headers]
        for row in dataset["pod_to_odoo_rows"]:
            if row.get("compare_status") not in compare_statuses:
                continue
            sample_document = row.get("sample_document") or {}
            rows.append(
                [
                    row.get("document_uid"),
                    row.get("file_name"),
                    row.get("source_path"),
                    row.get("source_modified_at"),
                    row.get("match_key"),
                    row.get("compare_status_label"),
                    row.get("matched_odoo_count"),
                    row.get("matched_odoo_picking_names"),
                    row.get("matched_odoo_states"),
                    row.get("odoo_pod_index_count"),
                    row.get("odoo_pod_index_links"),
                    sample_document.get("file_url"),
                    sample_document.get("local_file_href"),
                    row.get("reason_code"),
                ]
            )
        return rows

    return [
        {"name": config["odoo_match_label"], "rows": odoo_rows("matched_pod")},
        {"name": config["odoo_missing_label"], "rows": odoo_rows("pod_not_found")},
        {"name": config["odoo_without_key_label"], "rows": odoo_rows("no_match_key")},
        {"name": config["pod_found_label"], "rows": pod_rows(POD_FOUND_IN_ODOO_STATUSES)},
        {"name": config["pod_missing_label"], "rows": pod_rows("not_found_in_odoo")},
        {"name": config["pod_unparseable_label"], "rows": pod_rows("no_match_key")},
    ]


def _reconcile_export_suffix(dataset):
    selected_start_month = str(dataset.get("selected_start_month") or "").strip()
    return selected_start_month.replace("-", "") if selected_start_month else "all"


def _reconcile_export_stamp(dataset):
    generated_at = str(dataset.get("generated_at") or "").replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    return generated_at or datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _odoo_export_headers(config):
    return [
        "odoo_picking_id",
        "picking_name",
        "state",
        "business_datetime",
        config["match_key_label"],
        "pod_file_count",
        "sample_pod_files",
        "sample_pod_paths",
        "source_file_urls",
        "local_file_urls",
    ]


def _pod_export_headers(config):
    return [
        "document_uid",
        "file_name",
        "source_path",
        "source_modified_at",
        config["match_key_label"],
        "compare_status_label",
        "matched_odoo_count",
        "matched_odoo_picking_names",
        "matched_odoo_states",
        "odoo_pod_index_count",
        "odoo_pod_index_links",
        "source_file_url",
        "local_file_url",
        "reason_code",
    ]


def _build_odoo_export_rows(config, rows):
    export_rows = [_odoo_export_headers(config)]
    for row in rows:
        export_rows.append(
            [
                row.get("odoo_picking_id"),
                row.get("picking_name"),
                row.get("state"),
                row.get("business_datetime"),
                row.get("match_key"),
                row.get("pod_file_count"),
                row.get("sample_pod_files"),
                row.get("sample_pod_paths"),
                _join_sample_documents(row.get("sample_documents"), "file_url"),
                _join_sample_documents(row.get("sample_documents"), "local_file_href"),
            ]
        )
    return export_rows


def _build_pod_export_rows(config, rows):
    export_rows = [_pod_export_headers(config)]
    for row in rows:
        sample_document = row.get("sample_document") or {}
        export_rows.append(
            [
                row.get("document_uid"),
                row.get("file_name"),
                row.get("source_path"),
                row.get("source_modified_at"),
                row.get("match_key"),
                row.get("compare_status_label"),
                row.get("matched_odoo_count"),
                row.get("matched_odoo_picking_names"),
                row.get("matched_odoo_states"),
                row.get("odoo_pod_index_count"),
                row.get("odoo_pod_index_links"),
                sample_document.get("file_url"),
                sample_document.get("local_file_href"),
                row.get("reason_code"),
            ]
        )
    return export_rows


def build_reconcile_export_workbook(kind: str = DEFAULT_RECONCILE_KIND, start_month: str = ""):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    config = dataset["source_config"]
    sheet_specs = _export_sheet_rows(dataset)
    workbook_bytes = build_xlsx_workbook(
        sheet_specs,
        title=f"{config['label']} POD Reconcile Detail",
    )
    safe_stamp = _reconcile_export_stamp(dataset)
    suffix = "" if _reconcile_export_suffix(dataset) == "all" else f"_{_reconcile_export_suffix(dataset)}"
    return {
        "filename": f"pod_reconcile_{config['slug']}{suffix}_{safe_stamp}.xlsx",
        "content": workbook_bytes,
        "sheet_names": [spec["name"] for spec in sheet_specs],
    }


def build_reconcile_odoo_export_workbook(kind: str = DEFAULT_RECONCILE_KIND, start_month: str = ""):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    config = dataset["source_config"]
    sheet_specs = _export_sheet_rows(dataset)[:3]
    return {
        "filename": f"pod_reconcile_{config['slug']}_odoo_to_pod_{_reconcile_export_suffix(dataset)}_{_reconcile_export_stamp(dataset)}.xlsx",
        "content": build_xlsx_workbook(sheet_specs, title=f"{config['label']} Odoo to POD Detail"),
        "sheet_names": [spec["name"] for spec in sheet_specs],
    }


def build_reconcile_pod_export_workbook(kind: str = DEFAULT_RECONCILE_KIND, start_month: str = ""):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    config = dataset["source_config"]
    sheet_specs = _export_sheet_rows(dataset)[3:]
    return {
        "filename": f"pod_reconcile_{config['slug']}_pod_to_odoo_{_reconcile_export_suffix(dataset)}_{_reconcile_export_stamp(dataset)}.xlsx",
        "content": build_xlsx_workbook(sheet_specs, title=f"{config['label']} POD to Odoo Detail"),
        "sheet_names": [spec["name"] for spec in sheet_specs],
    }


def build_reconcile_metric_export_workbook(
    kind: str = DEFAULT_RECONCILE_KIND,
    start_month: str = "",
    *,
    scope: str,
    metric: str,
):
    dataset = load_filtered_reconcile_dataset(kind, start_month)
    if not dataset:
        return None
    config = dataset["source_config"]

    if scope == "odoo":
        metric_map = {
            "total": (config["odoo_total_label"], dataset["odoo_to_pod_rows"]),
            "without_key": (
                config["odoo_without_key_label"],
                [row for row in dataset["odoo_to_pod_rows"] if row.get("compare_status") == "no_match_key"],
            ),
            "with_key": (
                config["odoo_with_key_label"],
                [row for row in dataset["odoo_to_pod_rows"] if row.get("compare_status") != "no_match_key"],
            ),
            "matched": (
                config["odoo_match_label"],
                [row for row in dataset["odoo_to_pod_rows"] if row.get("compare_status") == "matched_pod"],
            ),
            "missing": (
                config["odoo_missing_label"],
                [row for row in dataset["odoo_to_pod_rows"] if row.get("compare_status") == "pod_not_found"],
            ),
        }
        if metric not in metric_map:
            raise ValueError(f"Unsupported Odoo reconcile metric: {metric}")
        sheet_name, rows = metric_map[metric]
        sheet_rows = _build_odoo_export_rows(config, rows)
    elif scope == "pod":
        metric_map = {
            "total": (config["pod_total_label"], dataset["pod_to_odoo_rows"]),
            "unparseable": (
                config["pod_unparseable_label"],
                [row for row in dataset["pod_to_odoo_rows"] if row.get("compare_status") == "no_match_key"],
            ),
            "parseable": (
                config["pod_parseable_label"],
                [row for row in dataset["pod_to_odoo_rows"] if row.get("compare_status") != "no_match_key"],
            ),
            "found": (
                config["pod_found_label"],
                [
                    row for row in dataset["pod_to_odoo_rows"]
                    if row.get("compare_status") in POD_FOUND_IN_ODOO_STATUSES
                ],
            ),
            "missing": (
                config["pod_missing_label"],
                [row for row in dataset["pod_to_odoo_rows"] if row.get("compare_status") == "not_found_in_odoo"],
            ),
        }
        if metric not in metric_map:
            raise ValueError(f"Unsupported POD reconcile metric: {metric}")
        sheet_name, rows = metric_map[metric]
        sheet_rows = _build_pod_export_rows(config, rows)
    else:
        raise ValueError(f"Unsupported reconcile metric scope: {scope}")

    safe_scope = "odoo" if scope == "odoo" else "pod"
    return {
        "filename": f"pod_reconcile_{config['slug']}_{safe_scope}_{metric}_{_reconcile_export_suffix(dataset)}_{_reconcile_export_stamp(dataset)}.xlsx",
        "content": build_xlsx_workbook([{"name": sheet_name, "rows": sheet_rows}], title=f"{config['label']} {sheet_name}"),
        "sheet_names": [sheet_name],
    }


# Backward-compatible wrappers kept for the old EMTC-only page and tests.
def load_emtc_reconcile_dataset():
    return load_reconcile_dataset("emtc")


def build_emtc_odoo_to_pod_listing(*, query: str = "", status: str = "", page: int = 1, page_size: int = 25):
    return build_reconcile_odoo_to_pod_listing(kind="emtc", query=query, status=status, page=page, page_size=page_size)


def build_emtc_pod_to_odoo_listing(*, query: str = "", status: str = "", page: int = 1, page_size: int = 25):
    return build_reconcile_pod_to_odoo_listing(kind="emtc", query=query, status=status, page=page, page_size=page_size)
