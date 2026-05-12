from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .actions import resolve_storage_dir
from .odoo_missing_report import load_odoo_missing_snapshot
from .phase1a_runs import get_latest_phase1a_run, resolve_artifact_path
from .xlsx_utils import build_xlsx_workbook


STOCK_TRACKING_POD_IDS_PATH = Path(__file__).resolve().parents[1] / "runtime" / "stock_tracking_pod_picking_ids.json"


def _load_stock_tracking_pod_picking_ids():
    if not STOCK_TRACKING_POD_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(STOCK_TRACKING_POD_IDS_PATH.read_text(encoding="utf-8"))
        return set(int(pid) for pid in data.get("picking_ids", []))
    except Exception:
        return set()


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / "runtime" / "pod_odoo_compare"
SNAPSHOT_PATH = RUNTIME_DIR / "latest_snapshot.json"
SUMMARY_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_summary.csv"
MATCHED_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_matched.csv"
MATCHED_CANCELLED_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_matched_cancelled.csv"
MATCHED_OTHER_CARRIER_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_matched_other_carrier.csv"
NOT_FOUND_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_not_found.csv"
AMBIGUOUS_CSV_PATH = RUNTIME_DIR / "pod_vs_odoo_ambiguous.csv"
REPORT_MD_PATH = RUNTIME_DIR / "pod_vs_odoo_report.md"
PUSH_READY_XLSX_PATH = RUNTIME_DIR / "pod_ready_for_odoo.xlsx"

SOURCE_CARRIER_MAP = {
    "CMC": {"carrier_id": 25, "carrier_name": "CMC"},
    "EMTC": {"carrier_id": 24, "carrier_name": "EMTC"},
}

PREFERRED_KEY_TYPE_LABELS = {
    "picking_name": "出库单号",
    "tracking_number": "tracking number",
}

COMPARE_STATUS_LABELS = {
    "matched": "匹配成功",
    "matched_cancelled": "匹配到已取消单",
    "odoo_not_found": "按匹配键未命中 Odoo",
    "multiple_odoo_matches": "Odoo 命中多条",
}

COMPARE_STATUS_EXPLANATION_TEMPLATES = {
    "matched": "POD 已识别出{key_type}，在 Odoo 中命中 1 条 done 单据。",
    "matched_cancelled": "POD 已识别出{key_type}，在当前来源对应的 Odoo carrier 范围内命中 1 条单据，但状态是 cancel。",
    "odoo_not_found": "POD 已识别出{key_type}，但在 Odoo 中没有命中。",
    "multiple_odoo_matches": "POD 已识别出{key_type}，但在 Odoo 中命中了多条记录，暂时无法自动确定唯一单据。",
}

SUMMARY_FIELDS = [
    "source_name",
    "carrier_id",
    "carrier_name",
    "unique_identifier_count",
    "matched_count",
    "matched_cancelled_count",
    "not_found_count",
    "ambiguous_count",
    "match_rate_percent",
    "stock_tracking_pod_count",
]

DETAIL_FIELDS = [
    "compare_status",
    "source_name",
    "carrier_id",
    "carrier_name",
    "preferred_key_type",
    "preferred_key",
    "preferred_key_display",
    "document_count",
    "latest_source_modified_at",
    "source_file_path",
    "sample_file_names",
    "sample_source_paths",
    "matched_odoo_count",
    "matched_odoo_picking_ids",
    "matched_odoo_picking_names",
    "matched_odoo_carrier_names",
    "matched_odoo_tracking_numbers",
    "matched_odoo_pod_links",
    "matched_odoo_states",
    "matched_odoo_primary_business_datetime",
    "has_stock_tracking_pod",
]


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows, fieldnames):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _parse_datetime(value: str | None):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _canonicalize(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _normalize_picking_name(value):
    normalized = _canonicalize(value)
    return normalized or None


def _normalize_tracking(value):
    normalized = _canonicalize(value)
    return normalized or None


def _display_picking_name(value: str | None):
    normalized = _normalize_picking_name(value)
    if not normalized:
        return value
    if normalized.startswith("WCB2BOUT"):
        return f"WCB2B/OUT/{normalized[len('WCB2BOUT'):]}"
    if normalized.startswith("WCB2COUT"):
        return f"WCB2C/OUT/{normalized[len('WCB2COUT'):]}"
    if normalized.startswith("WBB2BRET"):
        return f"WBB2B/RET/{normalized[len('WBB2BRET'):]}"
    if normalized.startswith("OTHER"):
        return f"Other/{normalized[len('OTHER'):]}"
    if normalized.startswith("WHOUT"):
        return f"WH/OUT/{normalized[len('WHOUT'):]}"
    return value or normalized


def _float_percent(numerator, denominator):
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100, 2)


def _resolve_odoo_tracking_number(row):
    return row.get("tracking_number") or row.get("carrier_tracking_ref")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_carrier_context(source_name: str | None):
    source_key = _canonicalize(source_name)
    for name, context in SOURCE_CARRIER_MAP.items():
        if source_key.startswith(_canonicalize(name)):
            return context
    return {"carrier_id": None, "carrier_name": source_name or "-"}


def _detail_sort_key(row):
    return (
        row.get("compare_status") or "",
        row.get("source_name") or "",
        row.get("preferred_key") or "",
    )


def _local_file_context(local_path: str | None):
    if not local_path:
        return {"local_relative_path": None, "local_file_href": None}
    path = Path(local_path).resolve()
    storage_root = resolve_storage_dir().resolve()
    try:
        relative = path.relative_to(storage_root).as_posix()
    except ValueError:
        return {"local_relative_path": str(path), "local_file_href": None}
    return {
        "local_relative_path": relative,
        "local_file_href": f"/storage/files/{quote(relative, safe='/')}",
    }


def _load_latest_phase1a_ready_rows():
    run = get_latest_phase1a_run()
    if not run:
        return None
    matched_path = resolve_artifact_path(run["run_name"], "matched_report_json")
    if not matched_path:
        return None
    return {
        "run": run,
        "rows": list(_read_json(matched_path) or []),
    }


def _aggregate_pod_identifiers(rows):
    identifiers = {}
    for row in rows:
        preferred_key_type = row.get("preferred_key_type")
        preferred_key = row.get("preferred_key")
        if not preferred_key_type or not preferred_key:
            continue
        carrier_context = _resolve_carrier_context(row.get("source_name"))
        if preferred_key_type == "picking_name":
            normalized_key = _normalize_picking_name(preferred_key)
        elif preferred_key_type == "tracking_number":
            normalized_key = _normalize_tracking(preferred_key)
        else:
            normalized_key = _canonicalize(preferred_key)
        if not normalized_key:
            continue
        identifier_key = (
            row.get("source_name") or "-",
            carrier_context["carrier_id"],
            preferred_key_type,
            normalized_key,
        )
        item = identifiers.setdefault(
            identifier_key,
            {
                "source_name": row.get("source_name") or "-",
                "source_type": row.get("source_type") or "-",
                "carrier_id": carrier_context["carrier_id"],
                "carrier_name": carrier_context["carrier_name"],
                "preferred_key_type": preferred_key_type,
                "preferred_key": preferred_key,
                "normalized_key": normalized_key,
                "document_count": 0,
                "sample_file_names": [],
                "sample_source_paths": [],
                "sample_documents": [],
                "latest_source_modified_at": None,
            },
        )
        item["document_count"] += 1
        file_name = row.get("file_name")
        source_path = row.get("source_path")
        if file_name and file_name not in item["sample_file_names"] and len(item["sample_file_names"]) < 5:
            item["sample_file_names"].append(file_name)
        if source_path and source_path not in item["sample_source_paths"] and len(item["sample_source_paths"]) < 5:
            item["sample_source_paths"].append(source_path)
        if len(item["sample_documents"]) < 5:
            local_context = _local_file_context(row.get("local_path"))
            sample_signature = (
                file_name or "",
                source_path or "",
                row.get("file_url") or "",
                local_context["local_file_href"] or "",
            )
            existing_signatures = {
                (
                    sample.get("file_name") or "",
                    sample.get("source_path") or "",
                    sample.get("file_url") or "",
                    sample.get("local_file_href") or "",
                )
                for sample in item["sample_documents"]
            }
            if sample_signature not in existing_signatures:
                item["sample_documents"].append(
                    {
                        "file_name": file_name,
                        "source_path": source_path,
                        "file_url": row.get("file_url"),
                        "local_relative_path": local_context["local_relative_path"],
                        "local_file_href": local_context["local_file_href"],
                    }
                )
        current_dt = _parse_datetime(row.get("source_modified_at"))
        existing_dt = _parse_datetime(item.get("latest_source_modified_at"))
        if current_dt and (existing_dt is None or current_dt > existing_dt):
            item["latest_source_modified_at"] = row.get("source_modified_at")
    return list(identifiers.values())


def _parse_matched_picking_ids(matched_odoo_picking_ids_str):
    if not matched_odoo_picking_ids_str:
        return []
    ids = []
    for pid in str(matched_odoo_picking_ids_str).split("|"):
        pid = pid.strip()
        if pid.isdigit():
            ids.append(int(pid))
    return ids


def _build_odoo_indices(total_detail_rows):
    by_picking = defaultdict(list)
    by_tracking = defaultdict(list)
    by_picking_any = defaultdict(list)
    by_tracking_any = defaultdict(list)
    for row in total_detail_rows:
        carrier_id = row.get("carrier_id")
        normalized_picking = _normalize_picking_name(row.get("picking_name"))
        if normalized_picking:
            by_picking[(carrier_id, normalized_picking)].append(row)
            by_picking_any[normalized_picking].append(row)
        normalized_tracking = _normalize_tracking(_resolve_odoo_tracking_number(row))
        if normalized_tracking:
            by_tracking[(carrier_id, normalized_tracking)].append(row)
            by_tracking_any[normalized_tracking].append(row)
    return by_picking, by_tracking, by_picking_any, by_tracking_any


def _build_ready_for_odoo_push_rows(ready_rows, total_detail_rows):
    by_picking, by_tracking, _, _ = _build_odoo_indices(total_detail_rows)
    export_rows = []
    for row in ready_rows:
        preferred_key_type = row.get("preferred_key_type")
        preferred_key = row.get("preferred_key")
        if not preferred_key_type or not preferred_key:
            continue

        carrier_context = _resolve_carrier_context(row.get("source_name"))
        carrier_id = carrier_context.get("carrier_id")
        if carrier_id is None:
            continue

        if preferred_key_type == "picking_name":
            normalized_key = _normalize_picking_name(preferred_key)
            matches = list(by_picking.get((carrier_id, normalized_key), []))
            preferred_key_display = _display_picking_name(preferred_key)
        elif preferred_key_type == "tracking_number":
            normalized_key = _normalize_tracking(preferred_key)
            matches = list(by_tracking.get((carrier_id, normalized_key), []))
            preferred_key_display = preferred_key
        else:
            continue

        done_matches = [item for item in matches if item.get("state") == "done"]
        if len(done_matches) != 1:
            continue

        odoo_row = done_matches[0]
        if odoo_row.get("x_studio_pod_link"):
            continue

        local_context = _local_file_context(row.get("local_path"))
        if not local_context.get("local_file_href"):
            continue

        export_rows.append(
            {
                "odoo_picking_id": odoo_row.get("odoo_picking_id"),
                "local_static_file_url": local_context["local_file_href"],
                "source_file_path": row.get("source_path") or "",
                "file_name": row.get("file_name") or "",
                "source_name": row.get("source_name") or "",
                "matching_key_type": preferred_key_type,
                "matching_key": preferred_key_display,
            }
        )
    return export_rows


def _build_ready_for_odoo_push_workbook(ready_rows, total_detail_rows):
    export_rows = _build_ready_for_odoo_push_rows(ready_rows, total_detail_rows)
    workbook_rows = [
        [
            "odoo_picking_id",
            "local_static_file_url",
            "source_file_path",
            "file_name",
            "source_name",
            "matching_key_type",
            "matching_key",
        ]
    ]
    for row in export_rows:
        workbook_rows.append(
            [
                row.get("odoo_picking_id") or "",
                row.get("local_static_file_url") or "",
                row.get("source_file_path") or "",
                row.get("file_name") or "",
                row.get("source_name") or "",
                row.get("matching_key_type") or "",
                row.get("matching_key") or "",
            ]
        )
    return build_xlsx_workbook(
        [{"name": "POD可推送进Odoo", "rows": workbook_rows}],
        title="POD 可推送进 Odoo",
    )


def _build_detail_row(item, matches):
    preferred_key_type = item.get("preferred_key_type")
    return {
        "source_name": item.get("source_name"),
        "carrier_id": item.get("carrier_id"),
        "carrier_name": item.get("carrier_name"),
        "preferred_key_type": preferred_key_type,
        "preferred_key_type_label": PREFERRED_KEY_TYPE_LABELS.get(preferred_key_type, preferred_key_type or "匹配键"),
        "preferred_key": item.get("preferred_key"),
        "preferred_key_display": (
            _display_picking_name(item.get("preferred_key"))
            if preferred_key_type == "picking_name"
            else item.get("preferred_key")
        ),
        "document_count": item.get("document_count"),
        "latest_source_modified_at": item.get("latest_source_modified_at"),
        "source_file_path": (item.get("sample_source_paths") or [None])[0],
        "sample_file_names": " | ".join(item.get("sample_file_names") or []),
        "sample_source_paths": " | ".join(item.get("sample_source_paths") or []),
        "sample_documents": item.get("sample_documents") or [],
        "matched_odoo_count": len(matches),
        "matched_odoo_picking_ids": ",".join(str(row.get("odoo_picking_id")) for row in matches[:10] if row.get("odoo_picking_id") is not None),
        "matched_odoo_picking_names": " | ".join(str(row.get("picking_name") or "") for row in matches[:10] if row.get("picking_name")),
        "matched_odoo_carrier_names": " | ".join(
            str(row.get("carrier_name") or row.get("carrier_id") or "")
            for row in matches[:10]
            if row.get("carrier_name") or row.get("carrier_id") is not None
        ),
        "matched_odoo_tracking_numbers": " | ".join(
            str(_resolve_odoo_tracking_number(row) or "") for row in matches[:10] if _resolve_odoo_tracking_number(row)
        ),
        "matched_odoo_pod_links": " | ".join(str(row.get("x_studio_pod_link") or "") for row in matches[:10] if row.get("x_studio_pod_link")),
        "matched_odoo_states": " | ".join(str(row.get("state") or "") for row in matches[:10] if row.get("state")),
        "matched_odoo_primary_business_datetime": (matches[0].get("business_datetime") if matches else None),
        "compare_status_label": None,
        "compare_explanation": None,
    }


def _decorate_detail_row(detail_row, compare_status):
    detail_row["compare_status"] = compare_status
    detail_row["compare_status_label"] = COMPARE_STATUS_LABELS.get(compare_status, compare_status)
    key_type_label = detail_row.get("preferred_key_type_label") or "匹配键"
    template = COMPARE_STATUS_EXPLANATION_TEMPLATES.get(compare_status, "")
    detail_row["compare_explanation"] = template.format(key_type=key_type_label) if template else ""
    return detail_row


def build_pod_odoo_compare_snapshot():
    phase1a_data = _load_latest_phase1a_ready_rows()
    if not phase1a_data:
        raise RuntimeError("还没有可用的 POD 识别结果。")

    odoo_snapshot = load_odoo_missing_snapshot()
    if not odoo_snapshot:
        raise RuntimeError("还没有可用的 Odoo 总明细快照。")

    run = phase1a_data["run"]
    ready_rows = phase1a_data["rows"]
    aggregated = _aggregate_pod_identifiers(ready_rows)
    total_detail_rows = list(odoo_snapshot.get("total_detail_rows") or [])
    by_picking, by_tracking, by_picking_any, by_tracking_any = _build_odoo_indices(total_detail_rows)

    stock_tracking_tn_to_picking = {}
    try:
        pod_index_path = PROJECT_ROOT / "runtime" / "odoo_push" / "odoo_pod_index.json"
        if pod_index_path.exists():
            pod_index = json.loads(pod_index_path.read_text(encoding="utf-8"))
            for tn, entry in (pod_index.get("by_tracking_number") or {}).items():
                picking_name = (entry.get("picking_name") or "").strip()
                if picking_name:
                    stock_tracking_tn_to_picking[_normalize_tracking(tn)] = _normalize_picking_name(picking_name)
    except Exception:
        pass

    stock_tracking_ids = _load_stock_tracking_pod_picking_ids()

    matched_rows = []
    matched_cancelled_rows = []
    matched_other_carrier_rows = []
    not_found_rows = []
    ambiguous_rows = []
    per_source = defaultdict(
        lambda: {
            "unique_identifier_count": 0,
            "matched_count": 0,
            "matched_cancelled_count": 0,
            "not_found_count": 0,
            "ambiguous_count": 0,
        }
    )
    matched_odoo_ids = set()

    for item in aggregated:
        carrier_id = item.get("carrier_id")
        preferred_key_type = item.get("preferred_key_type")
        normalized_key = item.get("normalized_key")
        if preferred_key_type == "picking_name":
            matches = list(by_picking.get((carrier_id, normalized_key), []))
            any_carrier_matches = list(by_picking_any.get(normalized_key, []))
        else:
            matches = list(by_tracking.get((carrier_id, normalized_key), []))
            any_carrier_matches = list(by_tracking_any.get(normalized_key, []))
            # Priority: stock.tracking first, then stock.picking fallback
            if not any_carrier_matches and normalized_key in stock_tracking_tn_to_picking:
                st_picking_name = stock_tracking_tn_to_picking[normalized_key]
                st_matches = by_picking_any.get(st_picking_name, [])
                done_st = [r for r in st_matches if r.get("state") == "done"]
                if done_st:
                    any_carrier_matches = st_matches

        same_carrier_done = [row for row in matches if row.get("state") == "done"]
        same_carrier_cancel = [row for row in matches if row.get("state") == "cancel"]
        other_carrier_done = []
        if carrier_id is not None:
            other_carrier_done = [row for row in any_carrier_matches if row.get("carrier_id") != carrier_id and row.get("state") == "done"]

        source_key = (item.get("source_name"), item.get("carrier_id"), item.get("carrier_name"))
        per_source[source_key]["unique_identifier_count"] += 1
        if len(same_carrier_done) == 1:
            detail_row = _build_detail_row(item, same_carrier_done)
            _decorate_detail_row(detail_row, "matched")
            matched_rows.append(detail_row)
            per_source[source_key]["matched_count"] += 1
            matched_odoo_ids.add(same_carrier_done[0].get("odoo_picking_id"))
        elif len(other_carrier_done) == 1:
            detail_row = _build_detail_row(item, other_carrier_done)
            _decorate_detail_row(detail_row, "matched")
            matched_rows.append(detail_row)
            per_source[source_key]["matched_count"] += 1
            matched_odoo_ids.add(other_carrier_done[0].get("odoo_picking_id"))
        elif len(same_carrier_done) == 0 and len(same_carrier_cancel) == 1:
            detail_row = _build_detail_row(item, same_carrier_cancel)
            _decorate_detail_row(detail_row, "matched_cancelled")
            matched_cancelled_rows.append(detail_row)
            per_source[source_key]["matched_cancelled_count"] += 1
            matched_odoo_ids.add(same_carrier_cancel[0].get("odoo_picking_id"))
        elif len(matches) == 0 and not other_carrier_done:
            detail_row = _build_detail_row(item, [])
            _decorate_detail_row(detail_row, "odoo_not_found")
            not_found_rows.append(detail_row)
            per_source[source_key]["not_found_count"] += 1
        else:
            detail_row = _build_detail_row(item, matches or other_carrier_done)
            _decorate_detail_row(detail_row, "multiple_odoo_matches")
            ambiguous_rows.append(detail_row)
            per_source[source_key]["ambiguous_count"] += 1

    for row in matched_rows:
        odoo_picking_ids = _parse_matched_picking_ids(row.get("matched_odoo_picking_ids", ""))
        row["has_stock_tracking_pod"] = any(pid in stock_tracking_ids for pid in odoo_picking_ids)

    summary_rows = []
    for (source_name, carrier_id, carrier_name), counts in sorted(per_source.items(), key=lambda item: (item[0][1] or 0, item[0][0] or "")):
        tracking_pod = sum(
            1 for row in matched_rows
            if row.get("source_name") == source_name and row.get("has_stock_tracking_pod")
        )
        summary_rows.append(
            {
                "source_name": source_name,
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "unique_identifier_count": counts["unique_identifier_count"],
                "matched_count": counts["matched_count"],
                "matched_cancelled_count": counts["matched_cancelled_count"],
                "not_found_count": counts["not_found_count"],
                "ambiguous_count": counts["ambiguous_count"],
                "match_rate_percent": _float_percent(
                    counts["matched_count"] + counts["matched_cancelled_count"],
                    counts["unique_identifier_count"],
                ),
                "stock_tracking_pod_count": tracking_pod,
            }
        )

    unique_identifier_count = len(aggregated)
    matched_count = len(matched_rows)
    matched_cancelled_count = len(matched_cancelled_rows)
    not_found_count = len(not_found_rows)
    ambiguous_count = len(ambiguous_rows)
    tracking_pod_total = sum(1 for row in matched_rows if row.get("has_stock_tracking_pod"))

    summary = run["summary"]
    snapshot = {
        "generated_at": _utc_now(),
        "phase1a": {
            "run_name": run["run_name"],
            "generated_at": summary.get("generated_at"),
            "ready_document_count": summary.get("ready_document_count", summary.get("matched_document_count", 0)),
            "review_document_count": summary.get("review_document_count", summary.get("unmatched_document_count", 0)),
        },
        "odoo": {
            "generated_at": odoo_snapshot.get("generated_at"),
            "total_done_outgoing": (odoo_snapshot.get("totals") or {}).get("total_done_outgoing", 0),
            "without_tracking_number": (odoo_snapshot.get("totals") or {}).get("without_tracking_number", 0),
        },
        "totals": {
            "unique_identifier_count": unique_identifier_count,
            "matched_count": matched_count,
            "matched_cancelled_count": matched_cancelled_count,
            "not_found_count": not_found_count,
            "ambiguous_count": ambiguous_count,
            "match_rate_percent": _float_percent(
                matched_count + matched_cancelled_count,
                unique_identifier_count,
            ),
            "review_document_count": summary.get("review_document_count", summary.get("unmatched_document_count", 0)),
            "matched_odoo_picking_count": len(matched_odoo_ids),
            "stock_tracking_pod_count": tracking_pod_total,
        },
        "summary_rows": summary_rows,
        "matched_rows": sorted(matched_rows, key=_detail_sort_key),
        "matched_cancelled_rows": sorted(matched_cancelled_rows, key=_detail_sort_key),
        "matched_other_carrier_rows": sorted(matched_other_carrier_rows, key=_detail_sort_key),
        "not_found_rows": sorted(not_found_rows, key=_detail_sort_key),
        "ambiguous_rows": sorted(ambiguous_rows, key=_detail_sort_key),
        "status_counts": dict(
            Counter(
                row.get("compare_status")
                for row in (matched_rows + matched_cancelled_rows + matched_other_carrier_rows + not_found_rows + ambiguous_rows)
            )
        ),
    }
    return snapshot


def render_pod_odoo_compare_markdown(snapshot):
    totals = snapshot.get("totals") or {}
    lines = [
        "# 最新 POD 识别 vs Odoo 对比报告",
        "",
        f"- 生成时间：{snapshot.get('generated_at') or '-'}",
        f"- POD 识别批次：`{snapshot.get('phase1a', {}).get('run_name') or '-'}`",
        f"- POD 识别时间：{snapshot.get('phase1a', {}).get('generated_at') or '-'}",
        f"- Odoo 快照时间：{snapshot.get('odoo', {}).get('generated_at') or '-'}",
        "",
        "## 总览",
        "",
        f"- POD 唯一匹配键数：{totals.get('unique_identifier_count', 0)}",
        f"- 匹配到 Odoo：{totals.get('matched_count', 0)}",
        f"- 匹配到已取消单：{totals.get('matched_cancelled_count', 0)}",
        f"- 按匹配键未命中 Odoo：{totals.get('not_found_count', 0)}",
        f"- Odoo 命中多条：{totals.get('ambiguous_count', 0)}",
        f"- 匹配率：{totals.get('match_rate_percent', 0)}%",
        f"- 识别待处理文档：{totals.get('review_document_count', 0)}",
        f"- 已推送 POD (stock.tracking)：{totals.get('stock_tracking_pod_count', 0)}",
        "",
        "## 来源汇总",
        "",
        "| 来源 | carrier_id | 唯一匹配键数 | 匹配到 Odoo | 匹配到已取消单 | 按匹配键未命中 Odoo | Odoo 命中多条 | 匹配率 | 已推送 POD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in snapshot.get("summary_rows") or []:
        lines.append(
            "| {source_name} | {carrier_id} | {unique_identifier_count} | {matched_count} | {matched_cancelled_count} | "
            "{not_found_count} | {ambiguous_count} | {match_rate_percent}% | {stock_tracking_pod_count} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## 下载文件",
            "",
            "- `pod_vs_odoo_summary.csv`",
            "- `pod_vs_odoo_matched.csv`",
            "- `pod_vs_odoo_matched_cancelled.csv`",
            "- `pod_vs_odoo_not_found.csv`",
            "- `pod_vs_odoo_ambiguous.csv`",
            "- `pod_ready_for_odoo.xlsx`",
            "- `latest_snapshot.json`",
            "",
        ]
    )
    return "\n".join(lines)


def save_pod_odoo_compare_snapshot(snapshot):
    _ensure_directory(RUNTIME_DIR)
    _write_json(SNAPSHOT_PATH, snapshot)
    _write_csv(SUMMARY_CSV_PATH, snapshot.get("summary_rows") or [], SUMMARY_FIELDS)
    _write_csv(MATCHED_CSV_PATH, snapshot.get("matched_rows") or [], DETAIL_FIELDS)
    _write_csv(MATCHED_CANCELLED_CSV_PATH, snapshot.get("matched_cancelled_rows") or [], DETAIL_FIELDS)
    _write_csv(MATCHED_OTHER_CARRIER_CSV_PATH, snapshot.get("matched_other_carrier_rows") or [], DETAIL_FIELDS)
    _write_csv(NOT_FOUND_CSV_PATH, snapshot.get("not_found_rows") or [], DETAIL_FIELDS)
    _write_csv(AMBIGUOUS_CSV_PATH, snapshot.get("ambiguous_rows") or [], DETAIL_FIELDS)
    REPORT_MD_PATH.write_text(render_pod_odoo_compare_markdown(snapshot), encoding="utf-8")
    return {
        "snapshot_json": SNAPSHOT_PATH,
        "summary_csv": SUMMARY_CSV_PATH,
        "matched_csv": MATCHED_CSV_PATH,
        "matched_cancelled_csv": MATCHED_CANCELLED_CSV_PATH,
        "not_found_csv": NOT_FOUND_CSV_PATH,
        "ambiguous_csv": AMBIGUOUS_CSV_PATH,
        "report_md": REPORT_MD_PATH,
    }


def refresh_pod_odoo_compare_snapshot():
    snapshot = build_pod_odoo_compare_snapshot()
    save_pod_odoo_compare_snapshot(snapshot)
    phase1a_data = _load_latest_phase1a_ready_rows()
    odoo_snapshot = load_odoo_missing_snapshot()
    ready_rows = list((phase1a_data or {}).get("rows") or [])
    total_detail_rows = list((odoo_snapshot or {}).get("total_detail_rows") or [])
    _ensure_directory(RUNTIME_DIR)
    PUSH_READY_XLSX_PATH.write_bytes(_build_ready_for_odoo_push_workbook(ready_rows, total_detail_rows))
    return snapshot


def load_pod_odoo_compare_snapshot():
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def resolve_pod_odoo_compare_artifact(artifact_key: str):
    mapping = {
        "snapshot_json": SNAPSHOT_PATH,
        "summary_csv": SUMMARY_CSV_PATH,
        "matched_csv": MATCHED_CSV_PATH,
        "matched_cancelled_csv": MATCHED_CANCELLED_CSV_PATH,
        "matched_other_carrier_csv": MATCHED_OTHER_CARRIER_CSV_PATH,
        "not_found_csv": NOT_FOUND_CSV_PATH,
        "ambiguous_csv": AMBIGUOUS_CSV_PATH,
        "report_md": REPORT_MD_PATH,
        "push_ready_xlsx": PUSH_READY_XLSX_PATH,
    }
    path = mapping.get(artifact_key)
    if not path or not path.exists():
        return None
    return path


def build_pod_odoo_compare_listing(
    *,
    query: str = "",
    source: str = "",
    status: str = "odoo_not_found",
    page: int = 1,
    page_size: int = 25,
    snapshot: dict | None = None,
):
    snapshot = snapshot or load_pod_odoo_compare_snapshot()
    if not snapshot:
        return None

    rows = []
    for key in ("matched_rows", "matched_cancelled_rows", "not_found_rows", "ambiguous_rows"):
        rows.extend(snapshot.get(key) or [])

    query_text = (query or "").strip().lower()
    filtered = []
    source_options = {}
    for row in rows:
        source_key = row.get("source_name") or "-"
        source_options[source_key] = source_key
        search_blob = " ".join(
            str(value or "")
            for value in (
                row.get("source_name"),
                row.get("preferred_key"),
                row.get("preferred_key_display"),
                row.get("sample_file_names"),
                row.get("sample_source_paths"),
                row.get("matched_odoo_picking_names"),
                row.get("matched_odoo_tracking_numbers"),
                row.get("matched_odoo_pod_links"),
                row.get("matched_odoo_states"),
            )
        ).lower()
        if query_text and query_text not in search_blob:
            continue
        if source and source_key != source:
            continue
        if status == "matched_any":
            if row.get("compare_status") not in {"matched", "matched_cancelled"}:
                continue
        elif status and row.get("compare_status") != status:
            continue
        filtered.append(row)

    safe_page_size = max(1, min(int(page_size or 25), 200))
    safe_page = max(1, int(page or 1))
    total = len(filtered)
    total_pages = max(1, math.ceil(total / safe_page_size))
    if safe_page > total_pages:
        safe_page = total_pages
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size

    return {
        "generated_at": snapshot.get("generated_at"),
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "page_start": start + 1 if total else 0,
        "page_end": min(end, total),
        "items": filtered[start:end],
        "filters": {"query": query, "source": source, "status": status},
        "source_options": [{"value": "", "label": "全部来源"}]
        + [{"value": value, "label": value} for value in sorted(source_options)],
        "status_options": [
            {"value": "", "label": "全部状态"},
            {"value": "odoo_not_found", "label": "按匹配键未命中 Odoo"},
            {"value": "matched_any", "label": "全部命中"},
            {"value": "matched", "label": "匹配成功"},
            {"value": "matched_cancelled", "label": "匹配到已取消单"},
            {"value": "multiple_odoo_matches", "label": "Odoo 命中多条"},
        ],
    }
