#!/usr/bin/env python3
"""Odoo push history store.

Keeps a persistent record of every push run in runtime/odoo_push/.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = PROJECT_ROOT / "runtime" / "odoo_push"
HISTORY_FILE = HISTORY_DIR / "history.json"

_lock = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _generate_push_id() -> str:
    return f"push-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def load_history() -> list[dict]:
    with _lock:
        if not HISTORY_FILE.exists():
            return []
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []


def _save_history(records: list[dict]):
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(HISTORY_FILE)


def append_record(record: dict) -> dict:
    with _lock:
        _ensure_dir()
        records = load_history()
        records.insert(0, record)
        # Keep last 500 records
        records = records[:500]
        _save_history(records)
        return record


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
        "tracking_pushed": 0,
        "tracking_created": 0,
        "tracking_skipped_same": 0,
        "tracking_not_found": 0,
        "picking_not_found": 0,
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
        records = load_history()
        for rec in records:
            if rec.get("push_id") == push_id:
                rec["finished_at"] = _utc_now()
                rec["status"] = status
                rec["total"] = result.get("total", 0)
                rec["tracking_pushed"] = result.get("tracking_pushed", 0)
                rec["tracking_created"] = result.get("tracking_pushed", 0) - result.get("tracking_skipped_same", 0)
                rec["tracking_skipped_same"] = result.get("tracking_skipped_same", 0)
                rec["tracking_not_found"] = result.get("tracking_not_found", 0)
                rec["picking_not_found"] = result.get("picking_not_found", 0)
                rec["skipped_existing"] = result.get("skipped_existing", 0)
                rec["failed"] = result.get("failed", 0)
                _save_history(records)
                return rec
        return None


def get_push_run(push_id: str) -> dict | None:
    for rec in load_history():
        if rec.get("push_id") == push_id:
            return rec
    return None


def list_push_runs(limit: int = 50) -> list[dict]:
    return load_history()[:limit]


def get_log_content(push_id: str) -> str | None:
    rec = get_push_run(push_id)
    if not rec:
        return None
    log_path = PROJECT_ROOT / rec.get("log_file", "")
    if not log_path.exists():
        return None
    try:
        return log_path.read_text(encoding="utf-8")
    except OSError:
        return None
