#!/usr/bin/env python3
"""Background scheduler for recurring pipeline runs.

Stores config in runtime/scheduler.json and runs a daemon thread
that wakes up every minute to check whether the next scheduled time
has been reached.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from clients.common.sync_integrity import exclusive_file_lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "runtime" / "scheduler.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "runtime" / "scheduler.log"
SCHEDULE_TIMEZONE_NAME = "Asia/Hong_Kong"
SCHEDULE_TIMEZONE = ZoneInfo(SCHEDULE_TIMEZONE_NAME)
DEFAULT_START_TIME = "09:00"


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _fmt_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_start_time(value: str | None) -> str:
    """Validate and normalize the operator-facing Hong Kong start time."""

    text = str(value or "").strip()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise ValueError("启动时间格式无效，请使用 HH:MM（香港时间）。")
    return f"{match.group(1)}:{match.group(2)}"


def _local_time_from_iso(value: str | None) -> str | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return parsed.astimezone(SCHEDULE_TIMEZONE).strftime("%H:%M")


def _display_local_datetime(value: str | None) -> str | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return parsed.astimezone(SCHEDULE_TIMEZONE).strftime("%Y-%m-%d %H:%M")


DEFAULT_STEPS = [
    {"key": "westerman_sync", "label": "Westerman API 同步", "default": True},
    {"key": "cmc_sync", "label": "CMC OneDrive 同步", "default": True},
    {"key": "emtc_sync", "label": "EMTC Google Drive 同步", "default": True},
    {"key": "phase1a", "label": "Phase 1A 识别", "default": True},
    {"key": "refresh_reports", "label": "刷新统一报告", "default": True},
    {"key": "odoo_push", "label": "推送 POD 到 Odoo", "default": False},
]


def validate_pipeline_steps(steps: dict | None) -> None:
    """Reject scheduler step combinations that cannot produce a useful run."""

    supplied_steps = steps if isinstance(steps, dict) else {}
    effective_steps = {
        step["key"]: bool(supplied_steps.get(step["key"], step["default"]))
        for step in DEFAULT_STEPS
    }
    if not any(effective_steps.values()):
        raise ValueError("流水线至少需要启用一个执行步骤。")
    if effective_steps["odoo_push"] and not effective_steps["phase1a"]:
        raise ValueError("启用 Odoo 推送时必须同时启用 Phase 1A 识别。")


def _default_config():
    return {
        "enabled": False,
        "interval_hours": 24,
        "start_time": DEFAULT_START_TIME,
        "schedule_timezone": SCHEDULE_TIMEZONE_NAME,
        "next_run_at": None,
        "last_run_at": None,
        "last_run_job_id": None,
        "last_run_ok": None,
        "created_at": _fmt_iso(_utc_now()),
        "steps": {s["key"]: s["default"] for s in DEFAULT_STEPS},
        "pipeline_options": {
            "config_path": "config.local.json",
            "phase1a_config_path": "phase1a/examples/phase1a_from_sync.example.json",
            "cmc_incremental": True,
            "emtc_incremental": True,
            "cmc_limit": 0,
            "emtc_limit": 0,
            "cmc_workers": 3,
            "emtc_workers": 3,
            "westerman_limit": 0,
            "westerman_workers": 3,
        },
    }


def _normalize_config(cfg: dict) -> dict:
    """Add newly introduced scheduler options without discarding saved values."""
    defaults = _default_config()
    normalized = {**defaults, **cfg}
    saved_steps = cfg.get("steps") if isinstance(cfg.get("steps"), dict) else {}
    saved_options = (
        cfg.get("pipeline_options")
        if isinstance(cfg.get("pipeline_options"), dict)
        else {}
    )
    normalized["steps"] = {**defaults["steps"], **saved_steps}
    normalized["pipeline_options"] = {
        **defaults["pipeline_options"],
        **saved_options,
    }
    try:
        normalized["start_time"] = normalize_start_time(cfg.get("start_time"))
    except ValueError:
        # Preserve the effective clock time of legacy configs instead of
        # moving an existing production schedule during deployment.
        normalized["start_time"] = (
            _local_time_from_iso(cfg.get("next_run_at")) or DEFAULT_START_TIME
        )
    normalized["schedule_timezone"] = SCHEDULE_TIMEZONE_NAME
    return normalized


class Scheduler:
    def __init__(
        self,
        job_store,
        config_path: Path | str | None = None,
        log_path: Path | str | None = None,
        claim_path: Path | str | None = None,
    ):
        self.job_store = job_store
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.log_path = Path(log_path or DEFAULT_LOG_PATH)
        self.claim_path = Path(
            claim_path
            or self.config_path.with_name(f".{self.config_path.name}.claim.lock")
        )
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.claim_path.parent.mkdir(parents=True, exist_ok=True)

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
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(cfg, dict):
                    return _default_config()
                return _normalize_config(cfg)
            except (OSError, json.JSONDecodeError):
                return _default_config()

    def _save_config_raw(self, cfg: dict):
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)

    def save_config(self, cfg: dict):
        with self._lock:
            with exclusive_file_lock(self.claim_path):
                self._save_config_raw(cfg)

    def update_config(self, patch: dict) -> dict:
        with self._lock:
            with exclusive_file_lock(self.claim_path):
                cfg = self.load_config()
                schedule_changed = False
                for key in ("enabled", "interval_hours", "start_time"):
                    if key in patch:
                        value = patch[key]
                        if key == "start_time":
                            value = normalize_start_time(value)
                        if cfg.get(key) != value:
                            schedule_changed = True
                        cfg[key] = value
                if "steps" in patch:
                    cfg["steps"] = {**cfg.get("steps", {}), **patch["steps"]}
                if "pipeline_options" in patch:
                    cfg["pipeline_options"] = {**cfg.get("pipeline_options", {}), **patch["pipeline_options"]}
                cfg["schedule_timezone"] = SCHEDULE_TIMEZONE_NAME
                cfg["updated_at"] = _fmt_iso(_utc_now())
                if schedule_changed:
                    next_run = self.compute_next_run(cfg) if cfg.get("enabled") else None
                    cfg["next_run_at"] = _fmt_iso(next_run) if next_run else None
                self._save_config_raw(cfg)
                return cfg

    def compute_next_run(
        self,
        cfg: dict,
        *,
        now: datetime | None = None,
        after: datetime | None = None,
    ) -> datetime | None:
        interval = int(cfg.get("interval_hours", 24))
        if interval <= 0:
            return None
        now_utc = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
        interval_delta = timedelta(hours=interval)

        if after is not None:
            next_run = after.astimezone(timezone.utc) + interval_delta
        else:
            start_time = normalize_start_time(
                cfg.get("start_time") or DEFAULT_START_TIME
            )
            hour, minute = (int(part) for part in start_time.split(":"))
            local_now = now_utc.astimezone(SCHEDULE_TIMEZONE)
            local_anchor = local_now.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            next_run = local_anchor.astimezone(timezone.utc)

        # Missed occurrences are skipped rather than submitted in a burst.
        while next_run <= now_utc:
            next_run += interval_delta
        return next_run

    def _run_pipeline_job(self) -> str | None:
        from .actions import build_pipeline_command

        cfg = self.load_config()
        opts = cfg.get("pipeline_options", {})
        steps = cfg.get("steps", {})
        try:
            validate_pipeline_steps(steps)
            label, command, metadata = build_pipeline_command(
                config_path=opts.get("config_path", "config.local.json"),
                cmc_incremental=bool(opts.get("cmc_incremental", True)),
                emtc_incremental=bool(opts.get("emtc_incremental", True)),
                phase1a_config_path=opts.get("phase1a_config_path", "phase1a/examples/phase1a_from_sync.example.json"),
                cmc_limit=int(opts.get("cmc_limit", 0) or 0),
                emtc_limit=int(opts.get("emtc_limit", 0) or 0),
                cmc_workers=int(opts.get("cmc_workers", 3) or 3),
                emtc_workers=int(opts.get("emtc_workers", 3) or 3),
                westerman_limit=int(opts.get("westerman_limit", 0) or 0),
                westerman_workers=int(opts.get("westerman_workers", 3) or 3),
                skip_cmc=not steps.get("cmc_sync", True),
                skip_emtc=not steps.get("emtc_sync", True),
                skip_westerman=not steps.get("westerman_sync", True),
                skip_phase1a=not steps.get("phase1a", True),
                skip_reports=not steps.get("refresh_reports", True),
                skip_odoo_push=not steps.get("odoo_push", False),
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
        with self._lock:
            # Every uvicorn worker owns a scheduler thread. Hold one shared
            # process lock across due-check, job creation, and next-run commit
            # so only one worker can claim a given scheduled occurrence.
            with exclusive_file_lock(self.claim_path):
                cfg = self.load_config()
                if not cfg.get("enabled", False):
                    return

                next_run_at = _parse_iso(cfg.get("next_run_at"))
                now = _utc_now()
                if next_run_at is None:
                    next_run = self.compute_next_run(cfg, now=now)
                    cfg["next_run_at"] = _fmt_iso(next_run) if next_run else None
                    self._save_config_raw(cfg)
                    return
                if next_run_at <= now:
                    attempt_at = _fmt_iso(now)
                    job_id = self._run_pipeline_job()
                    cfg["last_attempt_at"] = attempt_at
                    cfg["last_attempt_ok"] = bool(job_id)
                    if job_id:
                        cfg["last_run_at"] = attempt_at
                        cfg["last_run_job_id"] = job_id
                        cfg["last_run_ok"] = None  # Will be reconciled later if needed
                    # Advance from the claimed scheduled occurrence, not the
                    # actual start time, so slow jobs do not move the cadence.
                    cfg["last_scheduled_at"] = _fmt_iso(next_run_at)
                    next_run = self.compute_next_run(
                        cfg,
                        now=now,
                        after=next_run_at,
                    )
                    cfg["next_run_at"] = _fmt_iso(next_run) if next_run else None
                    self._save_config_raw(cfg)
                    if job_id:
                        self._log(f"Tick executed. Next run at {cfg['next_run_at']}")
                    else:
                        self._log(
                            "Tick could not create a job. "
                            f"Retry scheduled at {cfg['next_run_at']}"
                        )

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
            with exclusive_file_lock(self.claim_path):
                cfg = self.load_config()
                if cfg.get("enabled", False) and not cfg.get("next_run_at"):
                    nxt = self.compute_next_run(cfg)
                    if nxt:
                        cfg["next_run_at"] = _fmt_iso(nxt)
                        self._save_config_raw(cfg)
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
            "start_time": cfg.get("start_time", DEFAULT_START_TIME),
            "schedule_timezone": SCHEDULE_TIMEZONE_NAME,
            "schedule_timezone_label": "香港时间",
            "next_run_at": next_run_at,
            "next_run_display": _display_local_datetime(next_run_at),
            "last_run_at": cfg.get("last_run_at"),
            "last_run_display": _display_local_datetime(cfg.get("last_run_at")),
            "last_run_job_id": cfg.get("last_run_job_id"),
            "last_run_ok": cfg.get("last_run_ok"),
            "last_attempt_at": cfg.get("last_attempt_at"),
            "last_attempt_display": _display_local_datetime(cfg.get("last_attempt_at")),
            "last_attempt_ok": cfg.get("last_attempt_ok"),
            "is_running": self._thread is not None and self._thread.is_alive(),
            "steps": cfg.get("steps", {}),
            "step_definitions": DEFAULT_STEPS,
            "pipeline_options": cfg.get("pipeline_options", {}),
        }
