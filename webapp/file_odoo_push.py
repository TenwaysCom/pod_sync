from __future__ import annotations

from pathlib import Path

from phase1a.phase1a_pod.parsing import infer_document_fields
from phase1a.phase1a_pod.pipeline import build_direct_push_decision

from webapp.actions import DEFAULT_SYNC_CONFIG_REL, resolve_storage_dir
from webapp.file_browser import (
    WESTERMAN_PUSH_BLOCK_REASON,
    odoo_push_eligibility,
    resolve_storage_file,
)
from webapp.odoo_push import POD_INDEX_SUCCESS_STATUSES, push_single_item


SOURCE_TYPE_BY_KEY = {
    "CMC": "onedrive",
    "EMTC": "gdrive",
    "Westerman": "westerman_api",
}

PARSE_REASON_MESSAGES = {
    "NO_IDENTIFIER_FOUND": "无法从文件名解析出库单号或运单号",
    "MULTIPLE_PICKING_CANDIDATES": "文件名匹配到多个出库单号，无法唯一确定",
    "MULTIPLE_TRACKING_CANDIDATES": "文件名匹配到多个运单号，无法唯一确定",
}

STATUS_MESSAGES = {
    "pushed_tracking": "已写入 Odoo stock.tracking",
    "created_tracking": "已在 Odoo 创建 stock.tracking 并写入链接",
    "skipped_same": "Odoo 中已是相同链接，未重复写入",
    "not_found": "未找到对应的出库单或运单",
    "needs_review": "匹配结果需要人工确认，未写入",
    "multiple_tracking_matches": "运单号命中多条 stock.tracking，未写入",
    "multiple_picking_matches": "标识命中多条出库单，未写入",
    "multiple_tracking_records": "出库单关联多条 stock.tracking，未写入",
    "blocked": "该文件不允许单独推送",
    "failed": "推送到 Odoo 失败",
    "skipped": "缺少可写入的本地文件 URL，未推送",
}


def _source_key_from_relative_path(relative_path: str) -> str:
    return (relative_path or "").split("/", 1)[0]


def _push_targets_from_decision(decision: dict) -> list[dict]:
    split_pickings = list(decision.get("direct_split_pickings") or [])
    if split_pickings:
        return [
            {"match_key_type": "picking_name", "match_key": key}
            for key in split_pickings
        ]
    match_key_type = decision.get("preferred_key_type")
    match_key = decision.get("preferred_key")
    if match_key_type and match_key:
        return [{"match_key_type": match_key_type, "match_key": match_key}]
    return []


def plan_storage_file_odoo_push(
    relative_path: str,
    *,
    storage_root: Path | str | None = None,
) -> dict:
    cleaned = (relative_path or "").strip().lstrip("/")
    if not cleaned:
        return {
            "ok": False,
            "status": "blocked",
            "error_code": "PATH_REQUIRED",
            "error": "缺少文件路径",
            "http_status": 400,
        }

    root = Path(storage_root) if storage_root is not None else resolve_storage_dir()
    local_file = resolve_storage_file(root, cleaned)
    if local_file is None:
        return {
            "ok": False,
            "status": "blocked",
            "error_code": "FILE_NOT_FOUND",
            "error": "未找到该本地文件",
            "relative_path": cleaned,
            "http_status": 404,
        }

    source_key = _source_key_from_relative_path(cleaned)
    eligibility = odoo_push_eligibility(source_key)
    if not eligibility["odoo_push_allowed"]:
        error_code = "UNKNOWN_SOURCE"
        if source_key == "Westerman":
            error_code = "WESTERMAN_SINGLE_PUSH_REQUIRES_POOL_PAYLOAD"
        elif source_key == "_integrity":
            error_code = "INTERNAL_FILE"
        return {
            "ok": False,
            "status": "blocked",
            "error_code": error_code,
            "error": eligibility["odoo_push_blocked_reason"] or WESTERMAN_PUSH_BLOCK_REASON,
            "relative_path": cleaned,
            "source_key": source_key,
            "file_name": local_file.name,
            "local_path": str(local_file),
            "http_status": 409,
        }

    doc = infer_document_fields(
        {
            "file_name": local_file.name,
            "source_path": cleaned,
            "local_path": str(local_file),
        },
        source_name=source_key,
        source_type=SOURCE_TYPE_BY_KEY.get(source_key, source_key.lower()),
    )
    decision = build_direct_push_decision(doc)
    if decision.get("status") != "ready":
        reason_code = decision.get("reason_code") or "NO_IDENTIFIER_FOUND"
        return {
            "ok": False,
            "status": "needs_review",
            "error_code": reason_code,
            "error": PARSE_REASON_MESSAGES.get(
                reason_code,
                decision.get("reason_message") or "无法从文件名确定推送目标",
            ),
            "relative_path": cleaned,
            "source_key": source_key,
            "file_name": local_file.name,
            "local_path": str(local_file),
            "inferred_picking_names": doc.get("inferred_picking_names") or [],
            "inferred_tracking_numbers": doc.get("inferred_tracking_numbers") or [],
            "http_status": 409,
        }

    targets = _push_targets_from_decision(decision)
    if not targets:
        return {
            "ok": False,
            "status": "needs_review",
            "error_code": "NO_IDENTIFIER_FOUND",
            "error": PARSE_REASON_MESSAGES["NO_IDENTIFIER_FOUND"],
            "relative_path": cleaned,
            "source_key": source_key,
            "file_name": local_file.name,
            "local_path": str(local_file),
            "http_status": 409,
        }

    return {
        "ok": True,
        "status": "ready",
        "relative_path": cleaned,
        "source_key": source_key,
        "file_name": local_file.name,
        "local_path": str(local_file),
        "targets": targets,
        "inferred_picking_names": doc.get("inferred_picking_names") or [],
        "inferred_tracking_numbers": doc.get("inferred_tracking_numbers") or [],
    }


def _summarize_push_result(result: dict) -> str:
    status = str(result.get("status") or "")
    if result.get("error"):
        prefix = STATUS_MESSAGES.get(status, "推送未完成")
        return f"{prefix}：{result['error']}"
    if status in STATUS_MESSAGES:
        match_key = result.get("match_key")
        if match_key:
            return f"{STATUS_MESSAGES[status]}（{result.get('match_key_type')}: {match_key}）"
        return STATUS_MESSAGES[status]
    return status or "推送完成"


def push_storage_file_to_odoo(
    relative_path: str,
    *,
    config_path: str = DEFAULT_SYNC_CONFIG_REL,
    storage_root: Path | str | None = None,
    dry_run: bool = False,
) -> dict:
    plan = plan_storage_file_odoo_push(relative_path, storage_root=storage_root)
    if not plan.get("ok"):
        return plan

    details = []
    for target in plan["targets"]:
        result = push_single_item(
            config_path=config_path,
            match_key=target["match_key"],
            match_key_type=target["match_key_type"],
            file_url="",
            local_path=plan["local_path"],
            source_name=plan["source_key"],
            dry_run=dry_run,
        )
        details.append(result)

    successful = [
        item
        for item in details
        if item.get("ok") and item.get("status") in POD_INDEX_SUCCESS_STATUSES
    ]
    failed = [item for item in details if item not in successful]
    overall_ok = bool(details) and not failed
    first_failure = failed[0] if failed else None
    status = (
        successful[0].get("status")
        if overall_ok
        else (first_failure or {}).get("status") or "failed"
    )
    payload = {
        "ok": overall_ok,
        "status": status,
        "relative_path": plan["relative_path"],
        "source_key": plan["source_key"],
        "file_name": plan["file_name"],
        "local_path": plan["local_path"],
        "targets": plan["targets"],
        "details": details,
        "message": "；".join(_summarize_push_result(item) for item in details),
    }
    if first_failure:
        payload["error"] = first_failure.get("error") or _summarize_push_result(first_failure)
        payload["error_code"] = first_failure.get("error_code") or status
        payload["http_status"] = 200
    elif not overall_ok:
        payload["error"] = payload["message"]
        payload["http_status"] = 200
    return payload
