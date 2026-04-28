from __future__ import annotations

import math
import json
import shutil
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from .actions import PROJECT_ROOT, resolve_storage_dir


PHASE1A_OUT_ROOT = PROJECT_ROOT / "phase1a" / "out"
SOURCE_TYPE_LABELS = {
    "onedrive": "OneDrive",
    "gdrive": "Google Drive",
    "westerman_api": "Westerman API",
}

REASON_CODE_LABELS = {
    "NO_PICKING_FOUND": "未找到对应出库单",
    "INVALID_PICKING_KEY": "无法解析出有效出库单号",
    "INVALID_TRACKING_KEY": "无法解析出有效 tracking number",
    "MULTIPLE_PICKING_CANDIDATES": "命中多个候选出库单",
    "MULTIPLE_TRACKING_CANDIDATES": "命中多个候选 tracking number",
    "NO_IDENTIFIER_FOUND": "未解析出可用标识",
    "ODOO_SCOPE_FILTERED_OUT": "被当前 Odoo Scope 过滤",
    "PICKING_NOT_ELIGIBLE": "出库单不满足回填条件",
}


def _safe_read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def discover_phase1a_runs(limit: int = 12):
    if not PHASE1A_OUT_ROOT.exists():
        return []

    runs = []
    for summary_path in PHASE1A_OUT_ROOT.glob("*/phase1a_summary.json"):
        summary = _safe_read_json(summary_path)
        if not summary:
            continue
        run_dir = summary_path.parent.resolve()
        runs.append(
            {
                "run_name": run_dir.name,
                "run_dir": str(run_dir),
                "summary_path": str(summary_path.resolve()),
                "generated_at": summary.get("generated_at"),
                "job_id": summary.get("job_id"),
                "document_count": summary.get("document_count", 0),
                "processing_mode": summary.get("processing_mode", "scope_match"),
                "ready_document_count": summary.get("ready_document_count", summary.get("matched_document_count", 0)),
                "review_document_count": summary.get("review_document_count", summary.get("unmatched_document_count", 0)),
                "payload_item_count": summary.get("payload_item_count", summary.get("matched_picking_count", 0)),
                "matched_document_count": summary.get("matched_document_count", 0),
                "unmatched_document_count": summary.get("unmatched_document_count", 0),
                "matched_picking_count": summary.get("matched_picking_count", 0),
                "source_count": summary.get("source_count", 0),
                "output_dir": summary.get("output_dir"),
                "artifacts": summary.get("artifacts", {}),
            }
        )

    runs.sort(key=lambda item: (item.get("generated_at") or "", item["run_name"]), reverse=True)
    return runs[:limit]


def get_phase1a_run(run_name: str):
    summary_path = (PHASE1A_OUT_ROOT / run_name / "phase1a_summary.json").resolve()
    if not summary_path.exists():
        return None
    summary = _safe_read_json(summary_path)
    if not summary:
        return None
    run_dir = summary_path.parent.resolve()
    return {
        "run_name": run_dir.name,
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "summary": summary,
        "artifacts": summary.get("artifacts", {}),
    }


def get_latest_phase1a_run():
    runs = discover_phase1a_runs(limit=1)
    if not runs:
        return None
    return get_phase1a_run(runs[0]["run_name"])


def _source_filter_key(source_name: str, source_type: str):
    return f"{source_name}::{source_type}"


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


def _report_signature(path: Path):
    if not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=6)
def _load_document_rows_cached(
    run_name: str,
    matched_signature: tuple[int, int],
    unmatched_signature: tuple[int, int],
    storage_root: str,
):
    run = get_phase1a_run(run_name)
    if not run:
        return {"items": [], "source_options": [], "reason_options": []}

    matched_path = resolve_artifact_path(run_name, "matched_report_json")
    unmatched_path = resolve_artifact_path(run_name, "unmatched_report_json")
    rows = []
    source_options = {}
    reason_options = {}
    storage_dir = Path(storage_root)

    for report_path in (matched_path, unmatched_path):
        if not report_path:
            continue
        for row in _safe_read_json(report_path) or []:
            local_context = _local_file_context(row.get("local_path"), storage_dir)
            source_key = _source_filter_key(row.get("source_name") or "-", row.get("source_type") or "-")
            if source_key not in source_options:
                source_label = f"{row.get('source_name') or '-'} / {SOURCE_TYPE_LABELS.get(row.get('source_type'), row.get('source_type') or '-')}"
                source_options[source_key] = source_label
            reason_code = row.get("reason_code")
            if reason_code and reason_code not in reason_options:
                reason_options[reason_code] = REASON_CODE_LABELS.get(reason_code, reason_code)
            rows.append(
                {
                    "document_uid": row.get("document_uid"),
                    "file_name": row.get("file_name"),
                    "source_name": row.get("source_name"),
                    "source_type": row.get("source_type"),
                    "source_key": source_key,
                    "source_label": source_options[source_key],
                    "source_path": row.get("source_path"),
                    "source_modified_at": row.get("source_modified_at"),
                    "status": row.get("status"),
                    "reason_code": reason_code,
                    "reason_label": REASON_CODE_LABELS.get(reason_code, reason_code) if reason_code else None,
                    "preferred_key_type": row.get("preferred_key_type"),
                    "preferred_key": row.get("preferred_key"),
                    "primary_picking_name": row.get("primary_picking_name"),
                    "primary_tracking_number": row.get("primary_tracking_number"),
                    "file_url": row.get("file_url"),
                    "local_relative_path": local_context["local_relative_path"],
                    "local_file_href": local_context["local_file_href"],
                    "search_blob": " ".join(
                        str(value or "")
                        for value in (
                            row.get("file_name"),
                            row.get("source_path"),
                            row.get("preferred_key"),
                            row.get("primary_picking_name"),
                            row.get("primary_tracking_number"),
                            row.get("reason_code"),
                        )
                    ).lower(),
                }
            )

    rows.sort(key=lambda item: (item.get("source_modified_at") or "", item.get("file_name") or ""), reverse=True)
    return {
        "items": rows,
        "source_options": sorted(source_options.items(), key=lambda item: item[1]),
        "reason_options": sorted(reason_options.items(), key=lambda item: item[0]),
    }


def build_phase1a_document_listing(
    run_name: str,
    *,
    query: str = "",
    status: str = "",
    source: str = "",
    reason: str = "",
    page: int = 1,
    page_size: int = 25,
):
    run = get_phase1a_run(run_name)
    if not run:
        return None

    matched_path = resolve_artifact_path(run_name, "matched_report_json")
    unmatched_path = resolve_artifact_path(run_name, "unmatched_report_json")
    cached = _load_document_rows_cached(
        run_name,
        _report_signature(matched_path) if matched_path else (0, 0),
        _report_signature(unmatched_path) if unmatched_path else (0, 0),
        str(resolve_storage_dir()),
    )
    rows = cached["items"]
    query_text = (query or "").strip().lower()
    filtered = []
    for row in rows:
        if query_text and query_text not in row["search_blob"]:
            continue
        if status and row.get("status") != status:
            continue
        if source and row.get("source_key") != source:
            continue
        if reason and (row.get("reason_code") or "") != reason:
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
        "run_name": run_name,
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "page_start": start + 1 if total else 0,
        "page_end": min(end, total),
        "items": filtered[start:end],
        "filters": {
            "query": query,
            "status": status,
            "source": source,
            "reason": reason,
        },
        "status_options": [
            {"value": "", "label": "全部状态"},
            {"value": "ready", "label": "可推送"},
            {"value": "needs_review", "label": "待处理"},
        ],
        "source_options": [{"value": "", "label": "全部来源"}]
        + [{"value": value, "label": label} for value, label in cached["source_options"]],
        "reason_options": [{"value": "", "label": "全部原因"}]
        + [{"value": value, "label": label} for value, label in cached["reason_options"]],
    }


def resolve_phase1a_summary_path(config_path: str | Path):
    resolved_config = Path(config_path).resolve()
    config_payload = _safe_read_json(resolved_config)
    if not config_payload:
        return None
    output_dir = config_payload.get("output_dir")
    if not output_dir:
        return None
    output_path = Path(output_dir)
    if output_path.is_absolute():
        resolved_output_dir = output_path.resolve()
    else:
        resolved_output_dir = (resolved_config.parent / output_path).resolve()
    if config_payload.get("auto_run_dir"):
        candidates = []
        for summary_path in resolved_output_dir.glob("*/phase1a_summary.json"):
            summary = _safe_read_json(summary_path) or {}
            candidates.append((summary.get("generated_at") or "", summary_path))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
        return candidates[0][1]

    summary_path = resolved_output_dir / "phase1a_summary.json"
    if summary_path.exists():
        return summary_path
    return None


def resolve_artifact_path(run_name: str, artifact_key: str):
    run = get_phase1a_run(run_name)
    if not run:
        return None
    artifact_path = run["artifacts"].get(artifact_key)
    if not artifact_path:
        return None
    path = Path(artifact_path).resolve()
    run_dir = Path(run["run_dir"]).resolve()
    if run_dir not in path.parents and path != run_dir:
        return None
    if not path.exists():
        return None
    return path


def delete_phase1a_run(run_name: str):
    run_dir = (PHASE1A_OUT_ROOT / run_name).resolve()
    if not run_dir.exists():
        return False, "未找到识别运行记录。"
    if PHASE1A_OUT_ROOT.resolve() not in run_dir.parents:
        return False, "运行目录不合法。"
    shutil.rmtree(run_dir)
    return True, None
