from __future__ import annotations

import json
from pathlib import Path

from .actions import PROJECT_ROOT


REMOTE_COUNT_SNAPSHOT_PATH = PROJECT_ROOT / "runtime" / "remote_counts.json"


def load_remote_count_snapshot(snapshot_path: Path | None = None):
    path = Path(snapshot_path or REMOTE_COUNT_SNAPSHOT_PATH)
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "generated_at": None,
            "config_path": None,
            "sources": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "exists": False,
            "path": str(path),
            "generated_at": None,
            "config_path": None,
            "sources": [],
        }
    return {
        "exists": True,
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "config_path": payload.get("config_path"),
        "sources": payload.get("sources", []),
    }


def write_remote_count_snapshot(payload, snapshot_path: Path | None = None):
    path = Path(snapshot_path or REMOTE_COUNT_SNAPSHOT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
