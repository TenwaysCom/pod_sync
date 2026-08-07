#!/usr/bin/env python3
"""Build the existing-POD index from QueryDeluxe, shadow, or XML-RPC reads."""
from __future__ import annotations

import fcntl
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.actions import resolve_project_path

INDEX_FILE = PROJECT_ROOT / "runtime" / "odoo_push" / "odoo_pod_index.json"
COVERAGE_FILE = PROJECT_ROOT / "runtime" / "stock_tracking_pod_picking_ids.json"
INDEX_LOCK_FILE = PROJECT_ROOT / "runtime" / "odoo_push" / ".odoo_pod_index.lock"


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
        self.updated_at: str = ""
        self.requested_backend: str = ""
        self.authoritative_source: str = ""
        self.generation_id: str = ""
        self.coverage_status: str = ""
        self.shadow_status: str = ""
        self.shadow_difference_counts: dict = {}
        self.cache_generation: int = 0
        self.cache_epoch: str = ""
        self.local_coverage_picking_ids: list[int] = []

    @staticmethod
    def _entry_pod_links(entry) -> list:
        """Return all unique POD URLs recorded for one index key."""
        if not isinstance(entry, dict):
            return []

        raw_links = entry.get("pod_links")
        if isinstance(raw_links, (list, tuple)):
            candidates = list(raw_links)
        elif raw_links:
            candidates = [raw_links]
        else:
            candidates = []
        candidates.append(entry.get("pod_link"))

        links = []
        for link in candidates:
            if link and link not in links:
                links.append(link)
        return links

    @classmethod
    def _append_pod_link(cls, entry: dict, pod_link):
        links = cls._entry_pod_links(entry)
        if pod_link and pod_link not in links:
            links.append(pod_link)
        entry["pod_links"] = links

    @staticmethod
    def _append_local_pod_link(entry: dict, pod_link):
        links = list(entry.get("local_pod_links") or [])
        if pod_link and pod_link not in links:
            links.append(pod_link)
        entry["local_pod_links"] = links

    @classmethod
    def _restore_entries(cls, raw_entries) -> dict[str, dict]:
        if not isinstance(raw_entries, dict):
            return {}
        restored = {}
        for key, raw_entry in raw_entries.items():
            if not isinstance(raw_entry, dict):
                restored[key] = raw_entry
                continue
            entry = dict(raw_entry)
            entry["pod_links"] = cls._entry_pod_links(entry)
            local_links = list(dict.fromkeys(entry.get("local_pod_links") or []))
            if local_links:
                entry["local_pod_links"] = local_links
            else:
                entry.pop("local_pod_links", None)
            restored[key] = entry
        return restored

    def add_row(self, tracking_number: str | None, picking_name: str | None, pod_link: str, source_model: str = "api"):
        if not pod_link:
            return
        if tracking_number:
            for tn in str(tracking_number).split(","):
                tn = tn.strip()
                if tn and tn not in self.by_tracking_number:
                    entry = {
                        "picking_name": picking_name or "",
                        "pod_link": pod_link,
                        "source_model": source_model,
                    }
                    self._append_pod_link(entry, pod_link)
                    if source_model == "local_push":
                        self._append_local_pod_link(entry, pod_link)
                    self.by_tracking_number[tn] = entry
                elif tn:
                    self._append_pod_link(self.by_tracking_number[tn], pod_link)
                    if source_model == "local_push":
                        self._append_local_pod_link(self.by_tracking_number[tn], pod_link)
        if picking_name:
            picking_key = str(picking_name).strip()
            previous_links = self._entry_pod_links(self.by_picking_name.get(picking_key))
            previous_local_links = list(
                (self.by_picking_name.get(picking_key) or {}).get("local_pod_links") or []
            )
            entry = {
                "tracking_number": tracking_number or "",
                "pod_link": pod_link,
                "source_model": source_model,
                "pod_links": previous_links,
            }
            if previous_local_links:
                entry["local_pod_links"] = previous_local_links
            self._append_pod_link(entry, pod_link)
            if source_model == "local_push":
                self._append_local_pod_link(entry, pod_link)
            self.by_picking_name[picking_key] = entry
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

    def has_exact_pod_url(
        self,
        pod_url: str,
        *,
        tracking_number: str | None = None,
        picking_name: str | None = None,
    ) -> bool:
        """Return whether any row for the requested key has this exact URL."""
        if not pod_url:
            return False
        target_url = str(pod_url)
        entries = []
        if tracking_number:
            entry = self.by_tracking_number.get(str(tracking_number).strip())
            if entry:
                entries.append(entry)
        if picking_name:
            entry = self.by_picking_name.get(str(picking_name).strip())
            if entry:
                entries.append(entry)
        return any(
            str(link or "") == target_url
            for entry in entries
            for link in self._entry_pod_links(entry)
        )

    def summary(self) -> dict:
        return {
            "by_tracking_number_count": len(self.by_tracking_number),
            "by_picking_name_count": len(self.by_picking_name),
            "total_loaded": self.total_loaded,
            "fetched_at": self.fetched_at,
            "updated_at": self.updated_at,
            "requested_backend": self.requested_backend,
            "authoritative_source": self.authoritative_source,
            "generation_id": self.generation_id,
            "coverage_status": self.coverage_status,
            "shadow_status": self.shadow_status,
            "shadow_difference_counts": self.shadow_difference_counts,
            "cache_generation": self.cache_generation,
            "cache_epoch": self.cache_epoch,
            "local_coverage_picking_ids": self.local_coverage_picking_ids,
        }

    def to_dict(self) -> dict:
        return {
            "by_tracking_number": self.by_tracking_number,
            "by_picking_name": self.by_picking_name,
            "total_loaded": self.total_loaded,
            "fetched_at": self.fetched_at,
            "updated_at": self.updated_at,
            "requested_backend": self.requested_backend,
            "authoritative_source": self.authoritative_source,
            "generation_id": self.generation_id,
            "coverage_status": self.coverage_status,
            "shadow_status": self.shadow_status,
            "shadow_difference_counts": self.shadow_difference_counts,
            "cache_generation": self.cache_generation,
            "cache_epoch": self.cache_epoch,
            "local_coverage_picking_ids": self.local_coverage_picking_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExistingPodIndex":
        idx = cls()
        idx.by_tracking_number = cls._restore_entries(data.get("by_tracking_number", {}))
        idx.by_picking_name = cls._restore_entries(data.get("by_picking_name", {}))
        idx.total_loaded = data.get("total_loaded", 0)
        idx.fetched_at = data.get("fetched_at", "")
        idx.updated_at = data.get("updated_at", "")
        idx.requested_backend = data.get("requested_backend", "")
        idx.authoritative_source = data.get("authoritative_source", "")
        idx.generation_id = data.get("generation_id", "")
        idx.coverage_status = data.get("coverage_status", "")
        idx.shadow_status = data.get("shadow_status", "")
        idx.shadow_difference_counts = dict(data.get("shadow_difference_counts") or {})
        idx.cache_generation = int(data.get("cache_generation") or 0)
        idx.cache_epoch = str(data.get("cache_epoch") or "")
        idx.local_coverage_picking_ids = sorted(
            {
                int(value)
                for value in data.get("local_coverage_picking_ids") or []
                if value
            }
        )
        return idx


@contextmanager
def _index_file_lock():
    INDEX_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with INDEX_LOCK_FILE.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_index_unlocked(index: ExistingPodIndex):
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_name(f".{INDEX_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(INDEX_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()


def save_index(index: ExistingPodIndex):
    with _index_file_lock():
        _write_index_unlocked(index)


def load_saved_index() -> ExistingPodIndex | None:
    if not INDEX_FILE.exists():
        return None
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return ExistingPodIndex.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return None


def load_coverage_picking_ids(*, expected_generation_id: str | None = None) -> set[int]:
    """Read coverage only from a complete index/coverage generation.

    The writer exposes coverage first and the index commit marker last. A
    reader that lands in that tiny window retries; degraded generations
    intentionally continue using the last known-good coverage file.
    """
    for _attempt in range(3):
        before = load_saved_index()
        if not COVERAGE_FILE.exists():
            if expected_generation_id:
                raise RuntimeError("Odoo POD coverage generation is unavailable")
            return set()
        try:
            payload = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
            picking_ids = {int(value) for value in payload.get("picking_ids") or []}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if expected_generation_id:
                raise RuntimeError("Odoo POD coverage generation is unreadable") from exc
            return set()
        after = load_saved_index()
        if (before and before.generation_id) != (after and after.generation_id):
            continue
        current = after or before
        if (
            expected_generation_id
            and (not current or current.generation_id != expected_generation_id)
        ):
            continue
        if not current or not current.generation_id:
            return picking_ids
        if current.coverage_status != "complete":
            if expected_generation_id:
                raise RuntimeError("Odoo POD coverage generation is incomplete")
            return picking_ids
        if payload.get("generation_id") == current.generation_id:
            return picking_ids
    if expected_generation_id:
        raise RuntimeError("Odoo POD index/coverage generation changed during report build")
    return set()


def update_saved_index(
    mutator,
    *,
    coverage_picking_id: int | None = None,
) -> ExistingPodIndex:
    """Apply a local overlay while keeping index/coverage generations honest."""
    with _index_file_lock():
        index = load_saved_index() or ExistingPodIndex()
        previous_generation_id = index.generation_id
        coverage_ids = None
        if (
            coverage_picking_id
            and previous_generation_id
            and index.coverage_status == "complete"
            and COVERAGE_FILE.exists()
        ):
            try:
                coverage_payload = json.loads(
                    COVERAGE_FILE.read_text(encoding="utf-8")
                )
                if coverage_payload.get("generation_id") == previous_generation_id:
                    coverage_ids = {
                        int(value)
                        for value in coverage_payload.get("picking_ids") or []
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                coverage_ids = None

        mutator(index)
        index.generation_id = f"local-{uuid.uuid4().hex}"
        if coverage_picking_id:
            index.local_coverage_picking_ids = sorted(
                set(index.local_coverage_picking_ids) | {int(coverage_picking_id)}
            )
        if coverage_ids is None:
            # Keep the last known-good coverage file, but make its mismatch
            # explicit so compare generation checks cannot accept it as fresh.
            index.coverage_status = "local_push_pending_refresh"
        else:
            coverage_ids.add(int(coverage_picking_id))
            index.coverage_status = "complete"
            temporary = COVERAGE_FILE.with_name(
                f".{COVERAGE_FILE.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        {
                            "picking_ids": sorted(coverage_ids),
                            "count": len(coverage_ids),
                            "generation_id": index.generation_id,
                            "cache_generation": index.cache_generation,
                            "cache_epoch": index.cache_epoch,
                        },
                        ensure_ascii=False,
                    ) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(COVERAGE_FILE)
            finally:
                if temporary.exists():
                    temporary.unlink()
        # Coverage is exposed first; this index write is the commit marker.
        _write_index_unlocked(index)
        return index


def _local_push_markers(index: ExistingPodIndex | None) -> set[tuple[str, str, str, str]]:
    if not index:
        return set()
    markers = set()
    for key_type, entries, counterpart_field in (
        ("tracking", index.by_tracking_number, "picking_name"),
        ("picking", index.by_picking_name, "tracking_number"),
    ):
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            local_links = list(entry.get("local_pod_links") or [])
            if not local_links and entry.get("source_model") == "local_push":
                local_links = ExistingPodIndex._entry_pod_links(entry)
            for pod_link in local_links:
                markers.add(
                    (key_type, str(key), str(pod_link), str(entry.get(counterpart_field) or ""))
                )
    return markers


def _local_coverage_markers(index: ExistingPodIndex | None) -> set[int]:
    if not index:
        return set()
    return {
        int(value)
        for value in index.local_coverage_picking_ids
        if value
    }


def _merge_unconfirmed_local_pushes(
    index: ExistingPodIndex,
    current_markers: set[tuple[str, str, str, str]],
) -> None:
    """Keep local pushes until the authority returns the exact same URL."""
    for key_type, key, pod_link, counterpart in current_markers:
        if key_type == "tracking":
            if index.has_exact_pod_url(pod_link, tracking_number=key):
                continue
            index.add_row(
                tracking_number=key,
                picking_name=counterpart or None,
                pod_link=pod_link,
                source_model="local_push",
            )
        else:
            if index.has_exact_pod_url(pod_link, picking_name=key):
                continue
            index.add_row(
                tracking_number=counterpart or None,
                picking_name=key,
                pod_link=pod_link,
                source_model="local_push",
            )


def _commit_index_generation(
    index: ExistingPodIndex,
    state: dict,
) -> ExistingPodIndex:
    """Commit coverage first and the index generation marker last.

    When coverage is unavailable, keep the last known-good coverage file. The
    new index records the degraded status instead of turning a transient read
    failure into thousands of false missing-POD results.
    """
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _index_file_lock():
        current = load_saved_index()
        incoming_epoch = str(index.cache_epoch or "")
        current_epoch = str(current.cache_epoch or "") if current else ""
        incoming_is_older = False
        if current and current.requested_backend == index.requested_backend:
            if current_epoch and not incoming_epoch:
                # Never let an old process without epoch metadata replace an
                # index already committed by the epoch-aware code.
                incoming_is_older = True
            elif current_epoch and incoming_epoch and current_epoch != incoming_epoch:
                incoming_is_older = incoming_epoch < current_epoch
            elif current_epoch == incoming_epoch:
                incoming_is_older = bool(
                    index.cache_generation
                    and current.cache_generation > index.cache_generation
                )
        if incoming_is_older:
            return current
        current_url_markers = _local_push_markers(current)
        current_coverage_markers = _local_coverage_markers(current)
        authoritative_coverage_ids = {
            int(value) for value in state.get("coverage_ids", []) if value
        }
        index.local_coverage_picking_ids = sorted(
            current_coverage_markers - authoritative_coverage_ids
        )
        _merge_unconfirmed_local_pushes(index, current_url_markers)
        token = uuid.uuid4().hex
        index_tmp = INDEX_FILE.with_name(f".{INDEX_FILE.name}.{token}.tmp")
        coverage_tmp = COVERAGE_FILE.with_name(f".{COVERAGE_FILE.name}.{token}.tmp")
        try:
            index_tmp.write_text(
                json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if state.get("coverage_status") == "complete":
                picking_ids = sorted(
                    authoritative_coverage_ids | current_coverage_markers
                )
                coverage_tmp.write_text(
                    json.dumps(
                        {
                            "picking_ids": picking_ids,
                            "count": len(picking_ids),
                            "generation_id": index.generation_id,
                            "cache_generation": index.cache_generation,
                            "cache_epoch": index.cache_epoch,
                        },
                        ensure_ascii=False,
                    ) + "\n",
                    encoding="utf-8",
                )
                coverage_tmp.replace(COVERAGE_FILE)
            index_tmp.replace(INDEX_FILE)
        finally:
            for temporary in (index_tmp, coverage_tmp):
                if temporary.exists():
                    temporary.unlink()
        return index


def fetch_and_save_index(
    config_path: str = "config.local.json",
    *,
    refresh_xmlrpc_cache: bool = True,
) -> ExistingPodIndex:
    """Fetch one complete POD generation from the configured read backend."""
    from datetime import datetime, timezone
    from webapp.odoo_read_backends import fetch_pod_read_state

    full_cfg = load_config(config_path)
    state = fetch_pod_read_state(
        full_cfg,
        config_path=config_path,
        refresh_xmlrpc_cache=refresh_xmlrpc_cache,
    )

    index = ExistingPodIndex()
    now = datetime.now(timezone.utc)
    index.fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    index.requested_backend = state["requested_backend"]
    index.authoritative_source = state["authoritative_source"]
    index.generation_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    index.coverage_status = state["coverage_status"]
    index.shadow_status = state["shadow_status"]
    index.shadow_difference_counts = state["shadow_difference_counts"]
    index.cache_generation = int(state.get("cache_generation") or 0)
    index.cache_epoch = str(state.get("cache_epoch") or "")

    for record in state["rows"]:
        index.add_row(
            tracking_number=record.get("tracking_number"),
            picking_name=record.get("picking_name"),
            pod_link=record.get("pod_link"),
            source_model=state["authoritative_source"],
        )

    index = _commit_index_generation(index, state)
    print(f"[odoo_pull] Saved index: {index.summary()}")
    return index


def _fetch_and_save_stock_tracking_pod_ids(odoo_cfg: dict):
    """Backward-compatible coverage refresh using uncapped keyset pagination."""
    from webapp.odoo_read_backends import _direct_coverage_ids

    picking_ids = _direct_coverage_ids({"odoo": odoo_cfg})
    COVERAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _index_file_lock():
        temporary = COVERAGE_FILE.with_name(
            f".{COVERAGE_FILE.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    {"picking_ids": picking_ids, "count": len(picking_ids)},
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            temporary.replace(COVERAGE_FILE)
        finally:
            if temporary.exists():
                temporary.unlink()
    print(f"[odoo_pull] Saved {len(picking_ids)} stock.tracking picking IDs with POD.")
    return picking_ids


def get_index(config_path: str = "config.local.json") -> ExistingPodIndex:
    """Return a saved index only when it belongs to the configured mode."""
    from webapp.odoo_reconcile_cache import cache_status, resolve_read_backend

    full_cfg = load_config(config_path)
    backend = resolve_read_backend(full_cfg)
    saved = load_saved_index()
    if backend in {"shadow", "xmlrpc"}:
        status = cache_status(config_path, full_cfg=full_cfg)
        if (
            saved
            and saved.requested_backend == backend
            and saved.cache_generation == int(status.get("generation") or 0)
            and saved.cache_epoch == str(status.get("cache_epoch") or "")
        ):
            return saved
        if status.get("available"):
            return fetch_and_save_index(config_path, refresh_xmlrpc_cache=False)
    if saved and (
        saved.requested_backend == backend
        or (not saved.requested_backend and backend == "querydeluxe")
    ):
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
