#!/usr/bin/env python3
"""Odoo push history store.

Keeps a persistent record of every push run in runtime/odoo_push/.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from clients.common.sync_integrity import exclusive_file_lock
from webapp.signed_urls import redact_sensitive_data, redact_sensitive_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = PROJECT_ROOT / "runtime" / "odoo_push"
HISTORY_FILE = HISTORY_DIR / "history.json"

_lock = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)


def _history_lock_path() -> Path:
    """Return a stable lock file next to the replaceable history file."""

    return HISTORY_FILE.with_name(f".{HISTORY_FILE.name}.lock")


def _generate_push_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"push-{timestamp}-{uuid.uuid4().hex}"


def _load_history_unlocked() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        records = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return redact_sensitive_data(records) if isinstance(records, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_history() -> list[dict]:
    with _lock:
        if not HISTORY_FILE.exists():
            return []
        _ensure_dir()
        with exclusive_file_lock(_history_lock_path()):
            return _load_history_unlocked()


def _save_history_unlocked(records: list[dict]):
    safe_records = redact_sensitive_data(records)
    tmp = HISTORY_FILE.with_name(
        f".{HISTORY_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with tmp.open("x", encoding="utf-8") as tmp_fh:
            tmp_fh.write(
                json.dumps(safe_records, ensure_ascii=False, indent=2) + "\n"
            )
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        tmp.replace(HISTORY_FILE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _save_history(records: list[dict]):
    with _lock:
        _ensure_dir()
        with exclusive_file_lock(_history_lock_path()):
            _save_history_unlocked(records)


def append_record(record: dict) -> dict:
    with _lock:
        _ensure_dir()
        with exclusive_file_lock(_history_lock_path()):
            safe_record = redact_sensitive_data(record)
            records = _load_history_unlocked()
            records.insert(0, safe_record)
            # Keep last 500 records
            records = records[:500]
            _save_history_unlocked(records)
            return safe_record


def start_push_run(
    *,
    config_path: str,
    payload_path: str,
    dry_run: bool,
    limit: int,
    source_filter: str | None,
) -> tuple[str, Path]:
    push_id = _generate_push_id()
    log_path = HISTORY_DIR / f"{push_id}.log"
    record = {
        "push_id": push_id,
        "started_at": _utc_now(),
        "finished_at": None,
        "status": "running",
        "config_path": config_path,
        "payload_path": payload_path,
        "dry_run": dry_run,
        "limit": limit,
        "source_filter": source_filter,
        "total": 0,
        "input_total": 0,
        "eligible_document_count": 0,
        "selected_target_count": 0,
        "coalesced_count": 0,
        "superseded_count": 0,
        "attempted": 0,
        "completed": 0,
        "incomplete": 0,
        "retryable": 0,
        "manual_review": 0,
        "tracking_pushed": 0,
        "tracking_created": 0,
        "tracking_skipped_same": 0,
        "tracking_not_found": 0,
        "picking_not_found": 0,
        "ambiguous": 0,
        "needs_review": 0,
        "missing_required": 0,
        "failed": 0,
        "log_file": str(log_path.relative_to(PROJECT_ROOT)),
    }
    append_record(record)
    return push_id, log_path


def finish_push_run(
    push_id: str,
    *,
    status: str,
    result: dict,
) -> dict | None:
    with _lock:
        _ensure_dir()
        with exclusive_file_lock(_history_lock_path()):
            result = redact_sensitive_data(result)
            records = _load_history_unlocked()
            for rec in records:
                if rec.get("push_id") == push_id:
                    rec["finished_at"] = _utc_now()
                    rec["status"] = status
                    rec["total"] = result.get("total", 0)
                    rec["input_total"] = result.get(
                        "input_total", result.get("total", 0)
                    )
                    rec["eligible_document_count"] = result.get(
                        "eligible_document_count", rec["input_total"]
                    )
                    rec["selected_target_count"] = result.get(
                        "selected_target_count", result.get("total", 0)
                    )
                    rec["coalesced_count"] = result.get("coalesced_count", 0)
                    rec["superseded_count"] = result.get("superseded_count", 0)
                    rec["attempted"] = result.get(
                        "attempted", result.get("total", 0)
                    )
                    rec["completed"] = result.get("completed", 0)
                    rec["incomplete"] = result.get("incomplete", 0)
                    rec["retryable"] = result.get("retryable", 0)
                    rec["manual_review"] = result.get("manual_review", 0)
                    rec["tracking_pushed"] = result.get("tracking_pushed", 0)
                    rec["tracking_created"] = result.get(
                        "tracking_pushed", 0
                    ) - result.get("tracking_skipped_same", 0)
                    rec["tracking_skipped_same"] = result.get(
                        "tracking_skipped_same", 0
                    )
                    rec["tracking_not_found"] = result.get(
                        "tracking_not_found", 0
                    )
                    rec["picking_not_found"] = result.get(
                        "picking_not_found", 0
                    )
                    rec["skipped_existing"] = result.get("skipped_existing", 0)
                    rec["ambiguous"] = result.get("ambiguous", 0)
                    rec["needs_review"] = result.get("needs_review", 0)
                    rec["missing_required"] = result.get("missing_required", 0)
                    rec["failed"] = result.get("failed", 0)
                    _save_history_unlocked(records)
                    return rec
            return None


def get_push_run(push_id: str) -> dict | None:
    for rec in load_history():
        if rec.get("push_id") == push_id:
            return rec
    return None


def list_push_runs(limit: int = 50) -> list[dict]:
    return load_history()[:limit]


def delete_push_run(push_id: str) -> tuple[bool, str | None]:
    with _lock:
        _ensure_dir()
        with exclusive_file_lock(_history_lock_path()):
            records = _load_history_unlocked()
            target = None
            kept = []
            for rec in records:
                if rec.get("push_id") == push_id:
                    target = rec
                else:
                    kept.append(rec)

            if target is None:
                return False, "未找到推送记录。"
            if target.get("status") == "running":
                return False, "运行中的推送记录不能删除。"

            _save_history_unlocked(kept)

            log_file = target.get("log_file")
            if log_file:
                log_path = (PROJECT_ROOT / log_file).resolve()
                history_root = HISTORY_DIR.resolve()
                try:
                    log_path.relative_to(history_root)
                except ValueError:
                    return True, None
                if log_path.exists() and log_path.is_file():
                    try:
                        log_path.unlink()
                    except OSError:
                        pass
            return True, None


def get_log_content(push_id: str) -> str | None:
    rec = get_push_run(push_id)
    if not rec:
        return None
    log_path = PROJECT_ROOT / rec.get("log_file", "")
    if not log_path.exists():
        return None
    try:
        return redact_sensitive_text(log_path.read_text(encoding="utf-8"))
    except OSError:
        return None
