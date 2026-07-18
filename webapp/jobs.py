from __future__ import annotations

import json
import os
import re
import signal
import shlex
import subprocess
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .actions import PROJECT_ROOT
from .phase1a_runs import resolve_phase1a_summary_path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps legacy behavior.
    fcntl = None


DEFAULT_MAX_CONCURRENT_JOBS = int(os.environ.get("POD_SYNC_MAX_CONCURRENT_JOBS", "2"))
DEFAULT_JOB_TIMEOUT_SECONDS = int(os.environ.get("POD_SYNC_JOB_TIMEOUT_SECONDS", str(6 * 60 * 60)))
DEFAULT_MAX_LOG_READ_CHARS = int(os.environ.get("POD_SYNC_MAX_LOG_READ_CHARS", "200000"))

MICROSOFT_TEMP_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:my\.microsoftpersonalcontent\.com|onedrive\.live\.com|1drv\.ms)"
    r"[^\s\"'<>]*(?:tempauth|authkey)=[^\s\"'<>]+",
    re.IGNORECASE,
)
SENSITIVE_QUERY_PARAM_RE = re.compile(
    r"(?i)([?&](?:tempauth|authkey|access_token|refresh_token|id_token|client_secret|sig|signature|token)=)"
    r"[^&\s\"'<>]+"
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_command(command):
    return " ".join(shlex.quote(part) for part in command)


def process_exists(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def redact_log_text(text: str) -> str:
    text = MICROSOFT_TEMP_URL_RE.sub("[REDACTED_MICROSOFT_DOWNLOAD_URL]", text)
    return SENSITIVE_QUERY_PARAM_RE.sub(r"\1[REDACTED]", text)


def read_text_tail(path: Path, max_chars: int | None) -> str:
    if max_chars is not None and max_chars <= 0:
        return ""
    if max_chars is None:
        return path.read_text(encoding="utf-8", errors="replace")

    # Read enough bytes to cover max_chars for UTF-8 text without loading huge logs.
    max_bytes = max(4096, max_chars * 4)
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        content = handle.read()
    text = content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def terminate_process_tree(process: subprocess.Popen, *, force: bool = False):
    if os.name != "nt":
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass

    if force:
        process.kill()
    else:
        process.terminate()


class JobStore:
    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._job_timeout_seconds = max(0, DEFAULT_JOB_TIMEOUT_SECONDS)
        self._max_log_read_chars = max(0, DEFAULT_MAX_LOG_READ_CHARS)
        self._semaphore = threading.Semaphore(max(1, DEFAULT_MAX_CONCURRENT_JOBS))
        # Jobs owned by worker threads in this process must not be reclaimed in
        # the short interval between subprocess exit and final report persist.
        # After a process restart this set is empty, so genuine stale jobs are
        # still recovered from their durable pid/status fields.
        self._active_job_ids: set[str] = set()

    def _job_path(self, job_id: str):
        return self.root_dir / f"{job_id}.json"

    def _log_path(self, job_id: str):
        return self.root_dir / f"{job_id}.log"

    def _owner_lock_path(self, job_id: str):
        return self.root_dir / f"{job_id}.owner.lock"

    def _acquire_owner_lock(self, job_id: str, *, blocking: bool):
        handle = self._owner_lock_path(job_id).open("a+")
        if fcntl is None:
            return handle
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    @staticmethod
    def _release_owner_lock(handle):
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _write_job(self, payload):
        path = self._job_path(payload["job_id"])
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def _read_job_raw(self, job_id: str):
        path = self._job_path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_job(self, job_id: str):
        payload = self._read_job_raw(job_id)
        if not payload:
            return None
        return self._reconcile_job_state(payload)

    def list_jobs(self, limit: int = 30):
        jobs = []
        for path in self.root_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                jobs.append(self._reconcile_job_state(payload))
            except (OSError, json.JSONDecodeError):
                continue
        jobs.sort(key=lambda item: (item.get("created_at") or "", item["job_id"]), reverse=True)
        return jobs[:limit]

    def read_log(self, job_id: str, max_chars: int | None = None):
        path = self._log_path(job_id)
        if not path.exists():
            return ""
        if max_chars is None and self._max_log_read_chars:
            max_chars = self._max_log_read_chars
        return redact_log_text(read_text_tail(path, max_chars))

    def delete_job(self, job_id: str):
        with self._lock:
            payload = self.get_job(job_id)
            if not payload:
                return False, "未找到任务。"
            if payload.get("status") in {"queued", "running"}:
                return False, "运行中的任务不能删除。"

            removed = False
            for path in (self._job_path(job_id), self._log_path(job_id)):
                if path.exists():
                    path.unlink()
                    removed = True
            return removed, None

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
            "pid": None,
            "cwd": str(PROJECT_ROOT),
            "command": list(command),
            "command_display": format_command(command),
            "log_path": str(self._log_path(job_id)),
            "metadata": metadata,
        }
        with self._lock:
            self._write_job(payload)
            self._active_job_ids.add(job_id)
        thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        thread.start()
        return payload

    def _update_job(self, job_id: str, **changes):
        with self._lock:
            payload = self._read_job_raw(job_id)
            if not payload:
                return None
            payload.update(changes)
            self._write_job(payload)
            return payload

    def _append_log(self, job_id: str, text: str):
        with self._log_path(job_id).open("a", encoding="utf-8") as handle:
            handle.write(text)

    def _reconcile_job_state(self, payload):
        status = payload.get("status")
        if status not in {"queued", "running"}:
            return payload

        with self._lock:
            if payload.get("job_id") in self._active_job_ids:
                return payload

        owner_lock = self._acquire_owner_lock(payload["job_id"], blocking=False)
        if owner_lock is None:
            return payload

        try:
            # The owner may have completed while this reconciler waited for
            # the lock. Only recover from the durable state read under the
            # exclusive owner lock.
            latest = self._read_job_raw(payload["job_id"])
            if not latest:
                return payload
            payload = latest
            status = payload.get("status")
            if status not in {"queued", "running"}:
                return payload

            pid = payload.get("pid")
            started_at = payload.get("started_at")
            if status == "running" and pid and not process_exists(pid):
                payload = dict(payload)
                payload["status"] = "failed"
                payload["exit_code"] = -2
                payload["finished_at"] = payload.get("finished_at") or utc_now()
                payload["status_note"] = "任务进程已不存在，已按中断任务回收。"
                with self._lock:
                    self._write_job(payload)
                self._append_log(
                    payload["job_id"],
                    "\n\n--- JOB RECOVERED ---\n任务进程已不存在，状态已自动回收为 failed。\n",
                )
            elif status == "running" and not pid:
                started_dt = parse_utc(started_at)
                if started_dt and (datetime.now(timezone.utc) - started_dt).total_seconds() >= 120:
                    payload = dict(payload)
                    payload["status"] = "failed"
                    payload["exit_code"] = -2
                    payload["finished_at"] = payload.get("finished_at") or utc_now()
                    payload["status_note"] = "任务长期未记录 pid，已按中断任务回收。"
                    with self._lock:
                        self._write_job(payload)
                    self._append_log(
                        payload["job_id"],
                        "\n\n--- JOB RECOVERED ---\n任务长期未记录 pid，状态已自动回收为 failed。\n",
                    )
        finally:
            self._release_owner_lock(owner_lock)
        return payload

    def _extract_report_from_log(self, job_id: str) -> dict | None:
        path = self._log_path(job_id)
        if not path.exists():
            return None
        try:
            content = read_text_tail(path, self._max_log_read_chars or DEFAULT_MAX_LOG_READ_CHARS)
            # Fast path for probes and commands that print one compact JSON
            # object on their last non-empty line. Do not scan further upward:
            # an earlier probe object must not override a later pretty report.
            lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass

            # pipeline_runner prints its final business report as indented JSON.
            # Decode root objects that begin at column zero, newest first. Old
            # logs may contain the buffered "$ command" banner after child
            # output, so valid trailing runner text must not hide the report.
            # Nested step objects are indented and therefore do not match.
            decoder = json.JSONDecoder()
            for match in reversed(list(re.finditer(r"(?m)^\{", content))):
                try:
                    parsed, _consumed = decoder.raw_decode(content[match.start() :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                return parsed
        except Exception:
            pass
        return None

    @staticmethod
    def _phase1a_output_root(config_path: str | Path):
        resolved_config = Path(config_path).resolve()
        try:
            config = json.loads(resolved_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(config, dict) or not config.get("output_dir"):
            return None
        output_root = Path(str(config["output_dir"]))
        if not output_root.is_absolute():
            output_root = resolved_config.parent / output_root
        return output_root.resolve()

    def _validated_reported_phase1a_summary_path(
        self,
        raw_summary_path,
        *,
        config_path: str | Path,
    ):
        if not isinstance(raw_summary_path, (str, Path)) or not str(raw_summary_path).strip():
            return None
        output_root = self._phase1a_output_root(config_path)
        if output_root is None:
            return None
        summary_path = Path(str(raw_summary_path)).resolve()
        if summary_path.name != "phase1a_summary.json":
            return None
        try:
            summary_path.relative_to(output_root)
        except ValueError:
            return None
        if not summary_path.is_file():
            return None
        return summary_path

    def _phase1a_summary_path_for_job(self, payload, *, config_path: str | Path):
        report = payload.get("report")
        if isinstance(report, dict):
            data_report = report.get("data_report")
            if isinstance(data_report, dict):
                if "phase1a" not in data_report:
                    # A current pipeline that skipped or never reached Phase1A
                    # must not inherit another job's globally latest run.
                    return None
                phase1a_report = data_report.get("phase1a")
                if not isinstance(phase1a_report, dict):
                    return None
                if "summary_path" in phase1a_report:
                    # New reports are authoritative. Invalid reported paths fail
                    # closed instead of silently binding a concurrent run.
                    return self._validated_reported_phase1a_summary_path(
                        phase1a_report.get("summary_path"),
                        config_path=config_path,
                    )
                # Compatibility for reports persisted before summary_path was
                # added to data_report.phase1a.
                return resolve_phase1a_summary_path(config_path)

            if payload.get("action") == "run_pipeline":
                return None

        if payload.get("action") == "run_pipeline":
            # A current pipeline without a final report must not inherit the
            # globally latest Phase1A directory from another concurrent run.
            return None

        # Standalone/legacy Phase1A jobs had no nested pipeline report.
        return resolve_phase1a_summary_path(config_path)

    def _attach_phase1a_artifacts(self, payload):
        metadata = dict(payload.get("metadata") or {})
        config_path = metadata.get("phase1a_config_path")
        if not config_path:
            return payload
        summary_path = self._phase1a_summary_path_for_job(
            payload,
            config_path=config_path,
        )
        if not summary_path:
            return payload
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return payload
        if not isinstance(summary, dict):
            return payload
        payload["phase1a_run_name"] = summary_path.parent.name
        payload["phase1a_summary_path"] = str(summary_path.resolve())
        payload["phase1a_artifacts"] = summary.get("artifacts", {})
        return payload

    def _run_job(self, job_id: str):
        owner_lock = self._acquire_owner_lock(job_id, blocking=True)
        try:
            self._run_job_as_owner(job_id)
        finally:
            self._release_owner_lock(owner_lock)

    def _run_job_as_owner(self, job_id: str):
        payload = self._read_job_raw(job_id)
        if not payload:
            return

        command = payload["command"]
        env = os.environ.copy()
        log_path = self._log_path(job_id)
        acquired = False
        try:
            self._semaphore.acquire()
            acquired = True
            with log_path.open("a", encoding="utf-8") as log_handle:
                log_handle.write(f"$ {payload['command_display']}\n\n")
                # Flush the runner banner before the child inherits the same
                # file descriptor; otherwise buffered parent text can appear
                # after the child's final JSON report.
                log_handle.flush()
                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    start_new_session=(os.name != "nt"),
                )
                self._update_job(job_id, status="running", started_at=utc_now(), pid=process.pid)
                try:
                    if self._job_timeout_seconds:
                        exit_code = process.wait(timeout=self._job_timeout_seconds)
                    else:
                        exit_code = process.wait()
                except subprocess.TimeoutExpired:
                    log_handle.write(
                        f"\n\n--- JOB TIMEOUT ---\n任务超过 {self._job_timeout_seconds} 秒，正在终止。\n"
                    )
                    self._update_job(job_id, pid=None, status_note="任务超时，正在终止。")
                    terminate_process_tree(process)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        terminate_process_tree(process, force=True)
                        process.wait()
                    exit_code = -9

            current = self._read_job_raw(job_id) or payload
            status = "success" if exit_code == 0 else "failed"
            current["status"] = status
            current["exit_code"] = exit_code
            current["finished_at"] = utc_now()
            current["pid"] = None

            # Capture compact probe JSON or the pipeline's final indented JSON
            # business report from the end of the log.
            report = self._extract_report_from_log(job_id)
            if report:
                current["report"] = report
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
                pid=None,
            )
        finally:
            if acquired:
                self._semaphore.release()
            with self._lock:
                self._active_job_ids.discard(job_id)
