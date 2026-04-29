#!/usr/bin/env python3
"""Background scheduler for recurring pipeline runs.

Stores config in runtime/scheduler.json and runs a daemon thread
that wakes up every minute to check whether the next scheduled time
has been reached.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "runtime" / "scheduler.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "runtime" / "scheduler.log"


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_config():
    return {
        "enabled": False,
        "interval_hours": 24,
        "next_run_at": None,
        "last_run_at": None,
        "last_run_job_id": None,
        "last_run_ok": None,
        "created_at": _fmt_iso(_utc_now()),
        "pipeline_options": {
            "config_path": "config.local.json",
            "phase1a_config_path": "phase1a/examples/phase1a_from_sync.example.json",
            "cmc_incremental": True,
            "emtc_incremental": True,
            "cmc_limit": 0,
            "emtc_limit": 0,
            "cmc_workers": 3,
            "emtc_workers": 3,
        },
    }


class Scheduler:
    def __init__(
        self,
        job_store,
        config_path: Path | str | None = None,
        log_path: Path | str | None = None,
    ):
        self.job_store = job_store
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.log_path = Path(log_path or DEFAULT_LOG_PATH)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, message: str):
        ts = _fmt_iso(_utc_now())
        line = f"[{ts}] {message}\n"
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def load_config(self) -> dict:
        with self._lock:
            if not self.config_path.exists():
                cfg = _default_config()
                self._save_config_raw(cfg)
                return cfg
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return _default_config()

    def _save_config_raw(self, cfg: dict):
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)

    def save_config(self, cfg: dict):
        with self._lock:
            self._save_config_raw(cfg)

    def update_config(self, patch: dict) -> dict:
        with self._lock:
            cfg = self.load_config()
            for key in ("enabled", "interval_hours"):
                if key in patch:
                    cfg[key] = patch[key]
            if "pipeline_options" in patch:
                cfg["pipeline_options"] = {**cfg.get("pipeline_options", {}), **patch["pipeline_options"]}
            cfg["updated_at"] = _fmt_iso(_utc_now())
            self._save_config_raw(cfg)
            return cfg

    def compute_next_run(self, cfg: dict) -> datetime | None:
        interval = int(cfg.get("interval_hours", 24))
        if interval <= 0:
            return None
        last_run_at = _parse_iso(cfg.get("last_run_at"))
        base = last_run_at or _parse_iso(cfg.get("created_at")) or _utc_now()
        next_run = base.replace(minute=0, second=0, microsecond=0)
        # Advance by at least one interval from base
        while next_run <= _utc_now():
            next_run = datetime.fromtimestamp(next_run.timestamp() + interval * 3600, tz=timezone.utc)
        return next_run

    def _run_pipeline_job(self) -> str | None:
        from .actions import build_pipeline_command

        cfg = self.load_config()
        opts = cfg.get("pipeline_options", {})
        try:
            label, command, metadata = build_pipeline_command(
                config_path=opts.get("config_path", "config.local.json"),
                cmc_incremental=bool(opts.get("cmc_incremental", True)),
                emtc_incremental=bool(opts.get("emtc_incremental", True)),
                phase1a_config_path=opts.get("phase1a_config_path", "phase1a/examples/phase1a_from_sync.example.json"),
                cmc_limit=int(opts.get("cmc_limit", 0) or 0),
                emtc_limit=int(opts.get("emtc_limit", 0) or 0),
                cmc_workers=int(opts.get("cmc_workers", 3) or 3),
                emtc_workers=int(opts.get("emtc_workers", 3) or 3),
            )
        except Exception as exc:
            self._log(f"Failed to build pipeline command: {exc}")
            return None

        job = self.job_store.start_job(
            action="run_pipeline",
            label=label,
            command=command,
            metadata=metadata,
        )
        self._log(f"Started pipeline job {job['job_id']}")
        return job["job_id"]

    def tick(self):
        cfg = self.load_config()
        if not cfg.get("enabled", False):
            return

        next_run_at = _parse_iso(cfg.get("next_run_at"))
        if next_run_at is None or next_run_at <= _utc_now():
            job_id = self._run_pipeline_job()
            cfg["last_run_at"] = _fmt_iso(_utc_now())
            cfg["last_run_job_id"] = job_id
            cfg["last_run_ok"] = None  # Will be reconciled later if needed
            cfg["next_run_at"] = _fmt_iso(self.compute_next_run(cfg)) if job_id else None
            self.save_config(cfg)
            self._log(f"Tick executed. Next run at {cfg['next_run_at']}")

    def _loop(self):
        self._log("Scheduler loop started")
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self._log(f"Tick error: {exc}")
            # Sleep in small chunks so stop_event is responsive
            for _ in range(60):
                if self._stop_event.is_set():
                    break
                time.sleep(1)
        self._log("Scheduler loop stopped")

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            cfg = self.load_config()
            if cfg.get("enabled", False) and not cfg.get("next_run_at"):
                nxt = self.compute_next_run(cfg)
                if nxt:
                    cfg["next_run_at"] = _fmt_iso(nxt)
                    self.save_config(cfg)
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return True

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        cfg = self.load_config()
        next_run_at = cfg.get("next_run_at")
        if not next_run_at and cfg.get("enabled", False):
            nxt = self.compute_next_run(cfg)
            next_run_at = _fmt_iso(nxt) if nxt else None
        return {
            "enabled": cfg.get("enabled", False),
            "interval_hours": cfg.get("interval_hours", 24),
            "next_run_at": next_run_at,
            "last_run_at": cfg.get("last_run_at"),
            "last_run_job_id": cfg.get("last_run_job_id"),
            "last_run_ok": cfg.get("last_run_ok"),
            "is_running": self._thread is not None and self._thread.is_alive(),
            "pipeline_options": cfg.get("pipeline_options", {}),
        }
