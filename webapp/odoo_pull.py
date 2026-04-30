#!/usr/bin/env python3
"""Fetch existing POD data from Odoo via QueryDeluxe API.

API endpoint: {ODOO_URL}/api/querydeluxe/{query_id}/data
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.actions import resolve_project_path

INDEX_FILE = PROJECT_ROOT / "runtime" / "odoo_push" / "odoo_pod_index.json"


def load_config(config_path: str) -> dict:
    resolved = resolve_project_path(config_path, "config.local.json")
    return json.loads(resolved.read_text(encoding="utf-8"))


def get_odoo_cfg(full_cfg: dict) -> dict:
    return full_cfg.get("odoo", {})


class ExistingPodIndex:
    """Index of existing POD data in Odoo for fast duplicate checking."""

    def __init__(self):
        self.by_tracking_number: dict[str, dict] = {}
        self.by_picking_name: dict[str, dict] = {}
        self.total_loaded: int = 0
        self.fetched_at: str = ""

    def add_row(self, tracking_number: str | None, picking_name: str | None, pod_link: str, source_model: str = "api"):
        if not pod_link:
            return
        if tracking_number:
            for tn in str(tracking_number).split(","):
                tn = tn.strip()
                if tn and tn not in self.by_tracking_number:
                    self.by_tracking_number[tn] = {
                        "picking_name": picking_name or "",
                        "pod_link": pod_link,
                        "source_model": source_model,
                    }
        if picking_name:
            self.by_picking_name[str(picking_name).strip()] = {
                "tracking_number": tracking_number or "",
                "pod_link": pod_link,
                "source_model": source_model,
            }
        self.total_loaded += 1

    def has_pod(self, *, tracking_number: str | None = None, picking_name: str | None = None) -> bool:
        if tracking_number and str(tracking_number).strip() in self.by_tracking_number:
            return True
        if picking_name and str(picking_name).strip() in self.by_picking_name:
            return True
        return False

    def get_pod_info(self, *, tracking_number: str | None = None, picking_name: str | None = None) -> dict | None:
        if tracking_number:
            info = self.by_tracking_number.get(str(tracking_number).strip())
            if info:
                return info
        if picking_name:
            info = self.by_picking_name.get(str(picking_name).strip())
            if info:
                return info
        return None

    def summary(self) -> dict:
        return {
            "by_tracking_number_count": len(self.by_tracking_number),
            "by_picking_name_count": len(self.by_picking_name),
            "total_loaded": self.total_loaded,
            "fetched_at": self.fetched_at,
        }

    def to_dict(self) -> dict:
        return {
            "by_tracking_number": self.by_tracking_number,
            "by_picking_name": self.by_picking_name,
            "total_loaded": self.total_loaded,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExistingPodIndex":
        idx = cls()
        idx.by_tracking_number = data.get("by_tracking_number", {})
        idx.by_picking_name = data.get("by_picking_name", {})
        idx.total_loaded = data.get("total_loaded", 0)
        idx.fetched_at = data.get("fetched_at", "")
        return idx


def save_index(index: ExistingPodIndex):
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(INDEX_FILE)


def load_saved_index() -> ExistingPodIndex | None:
    if not INDEX_FILE.exists():
        return None
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return ExistingPodIndex.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return None


def fetch_and_save_index(config_path: str = "config.local.json") -> ExistingPodIndex:
    """Fetch POD data from QueryDeluxe API and save to local index."""
    from datetime import datetime, timezone
    import requests

    full_cfg = load_config(config_path)
    odoo_cfg = get_odoo_cfg(full_cfg)
    url = odoo_cfg.get("url", "").rstrip("/")
    query_id = full_cfg.get("querydeluxe", {}).get("existing_pod_query_id", 65)
    api_url = f"{url}/api/querydeluxe/{query_id}/data"

    print(f"[odoo_pull] Fetching from QueryDeluxe API: {api_url}")
    resp = requests.get(api_url, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    if not payload.get("success"):
        raise ValueError(f"QueryDeluxe API error: {payload}")

    data = payload.get("data", {})
    headers = data.get("header", [])
    rows = data.get("data", [])

    index = ExistingPodIndex()
    index.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in rows:
        # QueryDeluxe may return either dict objects or plain arrays
        if isinstance(row, dict):
            record = row
        else:
            record = dict(zip(headers, row))
        index.add_row(
            tracking_number=record.get("tracking_number"),
            picking_name=record.get("picking_name"),
            pod_link=record.get("pod_link"),
            source_model="querydeluxe",
        )

    save_index(index)
    print(f"[odoo_pull] Saved index: {index.summary()}")
    return index


def get_index(config_path: str = "config.local.json") -> ExistingPodIndex:
    """Return saved index, or fetch fresh if none exists."""
    saved = load_saved_index()
    if saved:
        return saved
    return fetch_and_save_index(config_path)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch existing POD data from Odoo QueryDeluxe API")
    parser.add_argument("--config", default="config.local.json")
    args = parser.parse_args()

    index = fetch_and_save_index(args.config)
    print(json.dumps(index.summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
