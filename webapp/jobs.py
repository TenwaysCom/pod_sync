from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .actions import PROJECT_ROOT
from .phase1a_runs import resolve_phase1a_summary_path


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_command(command):
    return " ".join(shlex.quote(part) for part in command)


class JobStore:
    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _job_path(self, job_id: str):
        return self.root_dir / f"{job_id}.json"

    def _log_path(self, job_id: str):
        return self.root_dir / f"{job_id}.log"

    def _write_job(self, payload):
        path = self._job_path(payload["job_id"])
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def get_job(self, job_id: str):
        path = self._job_path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_jobs(self, limit: int = 30):
        jobs = []
        for path in self.root_dir.glob("*.json"):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        jobs.sort(key=lambda item: (item.get("created_at") or "", item["job_id"]), reverse=True)
        return jobs[:limit]

    def read_log(self, job_id: str, max_chars: int | None = None):
        path = self._log_path(job_id)
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        if max_chars is not None and len(content) > max_chars:
            return content[-max_chars:]
        return content

    def start_job(self, *, action: str, label: str, command, metadata=None):
        metadata = metadata or {}
        job_id = f"job-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "job_id": job_id,
            "action": action,
            "label": label,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "cwd": str(PROJECT_ROOT),
            "command": list(command),
            "command_display": format_command(command),
            "log_path": str(self._log_path(job_id)),
            "metadata": metadata,
        }
        with self._lock:
            self._write_job(payload)
        thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        thread.start()
        return payload

    def _update_job(self, job_id: str, **changes):
        with self._lock:
            payload = self.get_job(job_id)
            if not payload:
                return None
            payload.update(changes)
            self._write_job(payload)
            return payload

    def _append_log(self, job_id: str, text: str):
        with self._log_path(job_id).open("a", encoding="utf-8") as handle:
            handle.write(text)

    def _attach_phase1a_artifacts(self, payload):
        metadata = dict(payload.get("metadata") or {})
        config_path = metadata.get("phase1a_config_path")
        if not config_path:
            return payload
        summary_path = resolve_phase1a_summary_path(config_path)
        if not summary_path:
            return payload
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return payload
        payload["phase1a_run_name"] = summary_path.parent.name
        payload["phase1a_summary_path"] = str(summary_path.resolve())
        payload["phase1a_artifacts"] = summary.get("artifacts", {})
        return payload

    def _run_job(self, job_id: str):
        payload = self.get_job(job_id)
        if not payload:
            return

        self._update_job(job_id, status="running", started_at=utc_now())
        command = payload["command"]
        env = os.environ.copy()
        log_path = self._log_path(job_id)
        try:
            with log_path.open("a", encoding="utf-8") as log_handle:
                log_handle.write(f"$ {payload['command_display']}\n\n")
                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
                exit_code = process.wait()

            current = self.get_job(job_id) or payload
            status = "success" if exit_code == 0 else "failed"
            current["status"] = status
            current["exit_code"] = exit_code
            current["finished_at"] = utc_now()
            current = self._attach_phase1a_artifacts(current)
            with self._lock:
                self._write_job(current)
        except Exception:
            self._append_log(job_id, "\n\n--- INTERNAL ERROR ---\n")
            self._append_log(job_id, traceback.format_exc())
            self._update_job(
                job_id,
                status="failed",
                finished_at=utc_now(),
                exit_code=-1,
            )
