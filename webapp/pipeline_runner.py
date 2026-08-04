#!/usr/bin/env python3
"""POD Sync Pipeline Runner.

Executes the full chain:
  1. Westerman API candidate refresh / POD download
  2. CMC OneDrive sync (incremental or full)
  3. EMTC Google Drive sync (incremental or full)
  4. Phase 1A parsing / matching
  5. Push POD links to Odoo
  6. Refresh Odoo POD index and unified reports

Usage:
    python webapp/pipeline_runner.py --config config.local.json
    python webapp/pipeline_runner.py --config config.local.json --full-sync
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.actions import (
    DEFAULT_PHASE1A_CONFIG_REL,
    DEFAULT_SYNC_CONFIG_REL,
    PROJECT_ROOT as ACTIONS_PROJECT_ROOT,
    resolve_project_path,
)


def _run_step(label: str, command: list[str]) -> tuple[int, str]:
    print(f"\n{'=' * 60}")
    print(f"STEP: {label}")
    print(f"{'=' * 60}")
    print(f"$ {' '.join(command)}\n")
    start = time.time()
    process = subprocess.Popen(
        command,
        cwd=str(ACTIONS_PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines = []
    for line in process.stdout:
        output_lines.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    rc = process.wait()
    elapsed = time.time() - start
    output = "".join(output_lines)
    print(f"\n[STEP DONE] {label} -> exit_code={rc}, elapsed={elapsed:.1f}s")
    return rc, output


def _parse_sync_summary(output: str) -> dict | None:
    """Parse sync_summary JSON from probe.py output."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("event") == "sync_summary":
                return data
        except json.JSONDecodeError:
            continue
    return None


def _parse_onedrive_cursor_stats(output: str) -> dict[str, int]:
    event_to_key = {
        "collection_cursor_restart": "cursor_restart_count",
        "collection_cursor_recovered": "cursor_recovered_count",
        "collection_cursor_failed": "cursor_failure_count",
    }
    stats = {key: 0 for key in event_to_key.values()}
    for line in output.splitlines():
        try:
            data = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("source") != "onedrive":
            continue
        key = event_to_key.get(data.get("event"))
        if key:
            stats[key] += 1
    return stats


def _parse_last_json_object(output: str) -> dict | None:
    """Return the final top-level JSON object printed by a command.

    Westerman's batch sync prints a human-readable, possibly multi-line JSON
    summary after any progress output. Only accept an object when everything
    after it is whitespace, so nested objects are not mistaken for the final
    summary.
    """
    decoder = json.JSONDecoder()
    result = None
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(output, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not output[end:].strip():
            result = value
    return result


def _resolve_phase1a_output_root(config_path: Path) -> tuple[Path, bool]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir = config.get("output_dir")
    if not output_dir:
        raise ValueError("Phase 1A config is missing output_dir")
    root = Path(str(output_dir))
    if not root.is_absolute():
        root = config_path.parent / root
    return root.resolve(), bool(config.get("auto_run_dir"))


def _phase1a_summary_snapshot(
    config_path: Path,
) -> dict[Path, tuple[int, int, str]]:
    """Capture only summaries that the configured Phase run can overwrite."""

    output_root, auto_run_dir = _resolve_phase1a_output_root(config_path)
    paths = (
        output_root.glob("*/phase1a_summary.json")
        if auto_run_dir
        else [output_root / "phase1a_summary.json"]
    )
    snapshot: dict[Path, tuple[int, int, str]] = {}
    for path in paths:
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        snapshot[path.resolve()] = (stat.st_mtime_ns, stat.st_size, digest)
    return snapshot


def _validate_phase1a_payload(
    payload_path: str | Path,
    *,
    summary: dict | None = None,
) -> Path:
    candidate = Path(payload_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ValueError(f"Phase 1A payload does not exist: {candidate}")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Phase 1A payload is not valid JSON: {candidate}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError(f"Phase 1A payload has no items list: {candidate}")

    if summary is not None:
        output_dir = Path(str(summary.get("output_dir") or "")).resolve()
        artifacts = summary.get("artifacts")
        artifact_path = (
            artifacts.get("payload_json") if isinstance(artifacts, dict) else None
        )
        if not artifact_path or Path(str(artifact_path)).resolve() != candidate:
            raise ValueError("Phase 1A summary payload artifact does not match")
        if candidate.parent != output_dir:
            raise ValueError("Phase 1A payload is outside the reported output_dir")
        for key in ("job_id", "generated_at"):
            expected = summary.get(key)
            if expected and payload.get(key) != expected:
                raise ValueError(f"Phase 1A payload {key} does not match this run")
    return candidate


def _current_phase1a_result(
    *,
    output: str,
    config_path: Path,
    before: dict[Path, tuple[int, int, str]],
) -> tuple[dict, Path]:
    """Resolve the summary/payload produced by this exact Phase subprocess.

    The subprocess JSON is authoritative. A changed-summary fallback exists for
    wrappers that suppress stdout, but a pre-existing global latest artifact is
    never accepted.
    """

    output_root, auto_run_dir = _resolve_phase1a_output_root(config_path)
    summary = _parse_last_json_object(output)
    summary_path: Path | None = None
    if isinstance(summary, dict):
        artifacts = summary.get("artifacts")
        reported_summary = (
            artifacts.get("summary_json") if isinstance(artifacts, dict) else None
        )
        if reported_summary:
            summary_path = Path(str(reported_summary)).resolve()
    if summary_path is None:
        after = _phase1a_summary_snapshot(config_path)
        changed = [
            path for path, signature in after.items() if before.get(path) != signature
        ]
        if len(changed) != 1:
            raise ValueError(
                "cannot identify exactly one Phase 1A summary created by this run"
            )
        summary_path = changed[0]
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("current Phase 1A summary is unreadable") from exc

    try:
        stat = summary_path.stat()
        digest = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("current Phase 1A summary file is missing") from exc
    if before.get(summary_path) == (stat.st_mtime_ns, stat.st_size, digest):
        raise ValueError("Phase 1A summary was not produced by this run")

    reported_output = Path(str(summary.get("output_dir") or "")).resolve()
    if summary_path.parent != reported_output:
        raise ValueError("Phase 1A summary is outside its reported output_dir")
    if auto_run_dir:
        if reported_output.parent != output_root:
            raise ValueError("Phase 1A summary is outside the configured output root")
    elif reported_output != output_root:
        raise ValueError("Phase 1A summary does not match configured output_dir")

    artifacts = summary.get("artifacts")
    payload_path = artifacts.get("payload_json") if isinstance(artifacts, dict) else None
    if not payload_path:
        raise ValueError("current Phase 1A summary has no payload_json artifact")
    return summary, _validate_phase1a_payload(payload_path, summary=summary)


def _westerman_is_opted_in(config_path: Path) -> bool:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    westerman_cfg = config.get("westerman_api") if isinstance(config, dict) else None
    return (
        isinstance(westerman_cfg, dict)
        and westerman_cfg.get("enabled") is True
    )


def run_pipeline(
    *,
    config_path: str,
    cmc_incremental: bool = True,
    emtc_incremental: bool = True,
    phase1a_config_path: str | None = None,
    cmc_limit: int = 0,
    emtc_limit: int = 0,
    cmc_workers: int = 3,
    emtc_workers: int = 3,
    westerman_limit: int = 0,
    westerman_workers: int = 3,
    skip_cmc: bool = False,
    skip_emtc: bool = False,
    skip_westerman: bool = False,
    skip_phase1a: bool = False,
    skip_reports: bool = False,
    skip_odoo_push: bool = False,
    odoo_push_limit: int = 0,
    odoo_push_payload_path: str | None = None,
) -> dict:
    resolved_sync_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    resolved_phase1a_config = resolve_project_path(
        phase1a_config_path or DEFAULT_PHASE1A_CONFIG_REL,
        DEFAULT_PHASE1A_CONFIG_REL,
    )
    skip_westerman = skip_westerman or not _westerman_is_opted_in(
        resolved_sync_config
    )

    steps: list[dict] = []
    data_report: dict[str, dict] = {}
    soft_failed_steps: list[str] = []
    business_attention_steps: list[str] = []
    warnings: list[dict] = []
    excluded_push_source_types = {"westerman_api"} if skip_westerman else set()
    overall_start = time.time()
    current_phase1a_payload: Path | None = None

    if not skip_odoo_push and skip_phase1a and not odoo_push_payload_path:
        return {
            "ok": False,
            "status": "failed",
            "degraded": False,
            "failed_step": "odoo_push_payload",
            "soft_failed_steps": [],
            "error": (
                "Odoo push requires the payload produced by this Pipeline run; "
                "when Phase 1A is skipped, pass --odoo-push-payload explicitly"
            ),
            "steps": [],
            "data_report": {},
            "elapsed_seconds": 0.0,
        }
    if (
        not skip_odoo_push
        and not skip_phase1a
        and odoo_push_payload_path
    ):
        return {
            "ok": False,
            "status": "failed",
            "degraded": False,
            "failed_step": "odoo_push_payload",
            "soft_failed_steps": [],
            "error": (
                "--odoo-push-payload is only valid together with --skip-phase1a"
            ),
            "steps": [],
            "data_report": {},
            "elapsed_seconds": 0.0,
        }

    def record(
        step: str,
        ok: bool,
        detail: str = "",
        *,
        status: str | None = None,
    ):
        entry = {"step": step, "ok": ok, "detail": detail}
        if status:
            entry["status"] = status
        steps.append(entry)

    # Step 1: Westerman runs first so a CMC/EMTC outage cannot starve its
    # independent POD polling schedule. A provider failure is soft: other
    # sources continue, while Westerman writes are decided by the fresh,
    # per-shipment pool gate. An incomplete candidate refresh still fails
    # closed for the whole Westerman source.
    if not skip_westerman:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "clients" / "westerman_api" / "sync.py"),
            "--config", str(resolved_sync_config),
            "--limit", str(westerman_limit),
            "--workers", str(westerman_workers),
        ]
        rc, output = _run_step("Westerman API 同步", cmd)
        summary = _parse_last_json_object(output) or {}
        summary_keys = (
            "candidate_count",
            "expired_count",
            "due_count",
            "checked_count",
            "waiting_pod_count",
            "downloaded_count",
            "already_downloaded_count",
            "partial_document_failure_count",
            "retry_count",
            "failed_count",
        )
        westerman_report = {key: summary.get(key, 0) for key in summary_keys}
        westerman_report.update({
            "status": "ok" if rc == 0 else "failed",
            "exit_code": rc,
            "scan_truncated": bool(summary.get("scan_truncated")),
            "candidate_refresh_status": summary.get(
                "candidate_refresh_status", "unknown"
            ),
            "error_code": summary.get("error_code"),
        })
        data_report["westerman_sync"] = westerman_report
        detail = (
            f"exit_code={rc}, candidates={westerman_report['candidate_count']}, "
            f"checked={westerman_report['checked_count']}, "
            f"waiting={westerman_report['waiting_pod_count']}, "
            f"downloaded={westerman_report['downloaded_count']}, "
            f"retry={westerman_report['retry_count']}, "
            f"failed={westerman_report['failed_count']}"
        )
        record("westerman_sync", rc == 0, detail)
        if rc != 0:
            soft_failed_steps.append("westerman_sync")
            if summary.get("candidate_refresh_status") != "complete":
                excluded_push_source_types.add("westerman_api")

    # Step 2: CMC OneDrive sync
    if not skip_cmc:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "clients" / "cmc_onedrive" / "probe.py"),
            "--config", str(resolved_sync_config),
            "--download-all-files",
            "--recursive",
        ]
        if cmc_incremental:
            cmd.append("--incremental")
        cmd.extend(["--limit", str(cmc_limit)])
        if cmc_workers != 1:
            cmd.extend(["--workers", str(cmc_workers)])
        rc, output = _run_step("CMC OneDrive 同步", cmd)
        summary = _parse_sync_summary(output)
        cursor_stats = _parse_onedrive_cursor_stats(output)
        cmc_report = {
            "status": "ok" if rc == 0 else "failed",
            "exit_code": rc,
            **cursor_stats,
        }
        if summary:
            cmc_report.update({
                "downloaded_files": summary.get("downloaded_files", 0),
                "skipped_files": summary.get("skipped_files", 0),
                "failed_files": summary.get("failed_files", 0),
                "downloaded_bytes": summary.get("downloaded_bytes", 0),
                "cursor_restart_count": summary.get(
                    "cursor_restart_count", cursor_stats["cursor_restart_count"]
                ),
                "cursor_recovered_count": summary.get(
                    "cursor_recovered_count", cursor_stats["cursor_recovered_count"]
                ),
                "cursor_failure_count": summary.get(
                    "cursor_failure_count", cursor_stats["cursor_failure_count"]
                ),
            })
        data_report["cmc_sync"] = cmc_report
        detail = f"exit_code={rc}"
        if summary or any(cursor_stats.values()):
            detail += (
                f", cursor_restarts={cmc_report['cursor_restart_count']}, "
                f"cursor_recovered={cmc_report['cursor_recovered_count']}, "
                f"cursor_failures={cmc_report['cursor_failure_count']}"
            )
        record("cmc_sync", rc == 0, detail)
        if rc != 0:
            soft_failed_steps.append("cmc_sync")
            excluded_push_source_types.add("onedrive")

    # Step 3: EMTC Google Drive sync
    if not skip_emtc:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "clients" / "emtc_gdrive" / "probe.py"),
            "--config", str(resolved_sync_config),
            "--download-all-files",
            "--recursive",
        ]
        if emtc_incremental:
            cmd.append("--incremental")
        cmd.extend(["--limit", str(emtc_limit)])
        if emtc_workers != 1:
            cmd.extend(["--workers", str(emtc_workers)])
        rc, output = _run_step("EMTC Google Drive 同步", cmd)
        summary = _parse_sync_summary(output)
        emtc_report = {
            "status": "ok" if rc == 0 else "failed",
            "exit_code": rc,
        }
        if summary:
            emtc_report.update({
                "downloaded_files": summary.get("downloaded_files", 0),
                "skipped_files": summary.get("skipped_files", 0),
                "failed_files": summary.get("failed_files", 0),
                "downloaded_bytes": summary.get("downloaded_bytes", 0),
            })
        data_report["emtc_sync"] = emtc_report
        record("emtc_sync", rc == 0, f"exit_code={rc}")
        if rc != 0:
            soft_failed_steps.append("emtc_sync")
            excluded_push_source_types.add("gdrive")

    # Step 4: Phase 1A
    if not skip_phase1a:
        try:
            phase1a_before = _phase1a_summary_snapshot(resolved_phase1a_config)
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "failed_step": "phase1a",
                "degraded": bool(soft_failed_steps),
                "soft_failed_steps": soft_failed_steps,
                "error": f"cannot snapshot Phase 1A output: {exc}",
                "steps": steps,
                "data_report": data_report,
                "elapsed_seconds": round(time.time() - overall_start, 1),
            }
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "phase1a" / "run_phase1a.py"),
            "--config", str(resolved_phase1a_config),
        ]
        rc, phase1a_output = _run_step("Phase 1A 识别", cmd)
        phase1a_summary = None
        phase1a_payload_error = None
        if rc == 0:
            try:
                phase1a_summary, current_phase1a_payload = _current_phase1a_result(
                    output=phase1a_output,
                    config_path=resolved_phase1a_config,
                    before=phase1a_before,
                )
            except Exception as exc:
                phase1a_payload_error = str(exc)
        if phase1a_summary:
            phase1a_output_dir = Path(
                str(phase1a_summary.get("output_dir") or current_phase1a_payload.parent)
            ).resolve()
            phase1a_report = {
                "document_count": phase1a_summary.get("document_count", 0),
                "ready_document_count": phase1a_summary.get("ready_document_count", 0),
                "review_document_count": phase1a_summary.get("review_document_count", 0),
                "payload_item_count": phase1a_summary.get("payload_item_count", 0),
                "matched_document_count": phase1a_summary.get("matched_document_count", 0),
                "unmatched_document_count": phase1a_summary.get("unmatched_document_count", 0),
                "run_name": phase1a_summary.get("run_name", ""),
                # Bind the Job report to this exact Phase subprocess.  The Job
                # UI must not rediscover the globally latest run later because
                # another concurrent pipeline may have finished in between.
                "summary_path": str(phase1a_output_dir / "phase1a_summary.json"),
                "output_dir": str(phase1a_output_dir),
                "payload_path": str(current_phase1a_payload.resolve()),
            }
            westerman_gate = phase1a_summary.get("westerman_source_gate")
            if isinstance(westerman_gate, dict):
                gate_report = {
                    key: westerman_gate.get(key)
                    for key in (
                        "status",
                        "filtered_record_count",
                        "candidate_refresh_status",
                        "candidate_refresh_updated_at",
                        "candidate_refresh_age_seconds",
                        "candidate_refresh_max_age_hours",
                    )
                    if key in westerman_gate
                }
                phase1a_report["westerman_source_gate"] = gate_report
                try:
                    phase_filtered_count = int(
                        westerman_gate.get("filtered_record_count") or 0
                    )
                except (TypeError, ValueError):
                    phase_filtered_count = 0
                if (
                    phase_filtered_count > 0
                    and westerman_gate.get("status")
                    not in {"ok", "not_applicable"}
                    and "westerman_pool_gate" not in soft_failed_steps
                ):
                    soft_failed_steps.append("westerman_pool_gate")
            data_report["phase1a"] = phase1a_report
        record("phase1a", rc == 0, f"exit_code={rc}")
        if rc != 0:
            return {
                "ok": False,
                "status": "failed",
                "failed_step": "phase1a",
                "degraded": bool(soft_failed_steps),
                "soft_failed_steps": soft_failed_steps,
                "steps": steps,
                "data_report": data_report,
                "elapsed_seconds": round(time.time() - overall_start, 1),
            }
        if phase1a_payload_error:
            record("phase1a_payload", False, phase1a_payload_error)
            return {
                "ok": False,
                "status": "failed",
                "failed_step": "phase1a_payload",
                "degraded": bool(soft_failed_steps),
                "soft_failed_steps": soft_failed_steps,
                "error": phase1a_payload_error,
                "steps": steps,
                "data_report": data_report,
                "elapsed_seconds": round(time.time() - overall_start, 1),
            }

    # Step 5: Push POD links to Odoo
    if not skip_odoo_push:
        print("\n" + "=" * 60)
        print("STEP: 推送 POD 链接到 Odoo")
        print("=" * 60)
        try:
            if skip_phase1a:
                selected_payload = _validate_phase1a_payload(
                    odoo_push_payload_path or ""
                )
                payload_origin = "explicit"
            elif current_phase1a_payload is not None:
                selected_payload = current_phase1a_payload
                payload_origin = "current_phase1a_run"
            else:
                raise ValueError("current Phase 1A run did not produce a payload")
        except Exception as exc:
            err = str(exc)
            print(f"[FAILED] 无法绑定本轮推送 payload: {err}")
            record("odoo_push", False, err)
            data_report["odoo_push"] = {
                "status": "failed",
                "payload_origin": "unresolved",
                "error": err,
            }
            return {
                "ok": False,
                "status": "failed",
                "failed_step": "odoo_push_payload",
                "degraded": bool(soft_failed_steps),
                "soft_failed_steps": soft_failed_steps,
                "error": err,
                "steps": steps,
                "data_report": data_report,
                "elapsed_seconds": round(time.time() - overall_start, 1),
            }
        else:
            selected_payload_str = str(selected_payload)
            print(
                f"Using payload ({payload_origin}): {selected_payload_str}"
            )
            try:
                from webapp.odoo_push import push_payload
                result = push_payload(
                    config_path=config_path,
                    payload_path=selected_payload_str,
                    dry_run=False,
                    limit=odoo_push_limit,
                    excluded_source_types=excluded_push_source_types or None,
                    incremental_pool=True,
                )
                push_status = (
                    result.get("execution_status")
                    or result.get("status")
                    or ("ok" if result.get("ok") else "failed")
                )
                push_business_status = result.get("business_status") or (
                    "attention"
                    if result.get("incomplete", 0) and not result.get("failed", 0)
                    else "complete"
                )
                if push_status in {"ok", "partial"}:
                    data_report["odoo_push"] = {
                        "status": push_status,
                        "execution_status": push_status,
                        "business_status": push_business_status,
                        "business_pending": result.get("business_pending", 0),
                        "technical_failure_count": result.get(
                            "technical_failure_count", result.get("failed", 0)
                        ),
                        "payload_path": selected_payload_str,
                        "payload_origin": payload_origin,
                        "total": result.get("total", 0),
                        "input_total": result.get("input_total", result.get("total", 0)),
                        "eligible_document_count": result.get(
                            "eligible_document_count", result.get("input_total", 0)
                        ),
                        "selected_target_count": result.get(
                            "selected_target_count", result.get("total", 0)
                        ),
                        "coalesced_count": result.get("coalesced_count", 0),
                        "superseded_count": result.get("superseded_count", 0),
                        "attempted": result.get("attempted", result.get("total", 0)),
                        "completed": result.get("completed", 0),
                        "incomplete": result.get("incomplete", 0),
                        "retryable": result.get("retryable", 0),
                        "manual_review": result.get("manual_review", 0),
                        "tracking_pushed": result.get("tracking_pushed", 0),
                        "tracking_skipped_same": result.get("tracking_skipped_same", 0),
                        "tracking_not_found": result.get("tracking_not_found", 0),
                        "picking_pushed": result.get("picking_pushed", 0),
                        "picking_skipped_same": result.get("picking_skipped_same", 0),
                        "picking_not_found": result.get("picking_not_found", 0),
                        "skipped_existing": result.get("skipped_existing", 0),
                        "source_excluded": result.get("source_excluded", 0),
                        "ambiguous": result.get("ambiguous", 0),
                        "needs_review": result.get("needs_review", 0),
                        "missing_required": result.get("missing_required", 0),
                        "failed": result.get("failed", 0),
                        "incremental_pool_enabled": result.get(
                            "incremental_pool_enabled", False
                        ),
                        "pool_path": result.get("pool_path"),
                        "pool_candidate_count": result.get(
                            "pool_candidate_count", 0
                        ),
                        "pool_inserted_count": result.get(
                            "pool_inserted_count", 0
                        ),
                        "pool_changed_count": result.get(
                            "pool_changed_count", 0
                        ),
                        "pool_evaluated_count": result.get(
                            "pool_evaluated_count", 0
                        ),
                        "pool_claimed_count": result.get(
                            "pool_claimed_count", 0
                        ),
                        "pool_deferred_count": result.get(
                            "pool_deferred_count", 0
                        ),
                        "pool_due_count": result.get("pool_due_count", 0),
                        "pool_states": result.get("pool_states", {}),
                        "pool_completed_total": result.get(
                            "pool_completed_total", 0
                        ),
                        "pool_cache_generation": result.get(
                            "pool_cache_generation", 0
                        ),
                        "pool_cache_refresh_kind": result.get(
                            "pool_cache_refresh_kind"
                        ),
                        "westerman_pool_gate_status": result.get(
                            "westerman_pool_gate_status", "not_applicable"
                        ),
                        "westerman_pool_filtered": result.get(
                            "westerman_pool_filtered", 0
                        ),
                        "westerman_candidate_refresh_status": result.get(
                            "westerman_candidate_refresh_status", "unknown"
                        ),
                        "westerman_candidate_refresh_updated_at": result.get(
                            "westerman_candidate_refresh_updated_at"
                        ),
                        "westerman_candidate_refresh_age_seconds": result.get(
                            "westerman_candidate_refresh_age_seconds"
                        ),
                        "westerman_candidate_refresh_max_age_hours": result.get(
                            "westerman_candidate_refresh_max_age_hours"
                        ),
                        "by_source": result.get("by_source", {}),
                    }
                    pool_states = result.get("pool_states") or {}
                    detail = f"status={push_status}, business_status={push_business_status}, input_documents={result.get('input_total',result.get('total',0))}, selected_targets={result.get('selected_target_count',result.get('total',0))}, coalesced={result.get('coalesced_count',0)}, evaluated={result.get('pool_evaluated_count',0)}, attempted={result.get('attempted',result.get('total',0))}, completed={result.get('completed',0)}, incomplete={result.get('incomplete',0)}, retryable={result.get('retryable',0)}, pool_retry={pool_states.get('retry',0)}, pool_review={pool_states.get('review',0)}, pushed={result.get('tracking_pushed',0)+result.get('picking_pushed',0)}, skipped={result.get('skipped_existing',0)+result.get('tracking_skipped_same',0)+result.get('picking_skipped_same',0)}, not_found={result.get('tracking_not_found',0)+result.get('picking_not_found',0)}, failed={result.get('failed',0)}"
                    if push_status == "ok":
                        if push_business_status == "attention":
                            print(f"[OK] 推送执行成功，有业务待处理数据: {detail}")
                            record("odoo_push", True, detail, status="attention")
                            business_attention_steps.append("odoo_push")
                        else:
                            print(f"[OK] 推送完成: {detail}")
                            record("odoo_push", True, detail)
                    else:
                        print(f"[PARTIAL] 推送部分完成，将继续刷新报告: {detail}")
                        record("odoo_push", False, detail)
                        if "odoo_push" not in soft_failed_steps:
                            soft_failed_steps.append("odoo_push")
                    westerman_gate_status = result.get(
                        "westerman_pool_gate_status", "not_applicable"
                    )
                    if (
                        result.get("westerman_pool_filtered", 0)
                        and westerman_gate_status not in {"ok", "not_applicable"}
                        and "westerman_pool_gate" not in soft_failed_steps
                    ):
                        soft_failed_steps.append("westerman_pool_gate")
                else:
                    data_report["odoo_push"] = {
                        "status": "failed",
                        "execution_status": result.get(
                            "execution_status", "failed"
                        ),
                        "business_status": result.get(
                            "business_status", "unknown"
                        ),
                        "business_pending": result.get("business_pending", 0),
                        "technical_failure_count": result.get(
                            "technical_failure_count", result.get("failed", 0)
                        ),
                        "payload_path": selected_payload_str,
                        "payload_origin": payload_origin,
                        "total": result.get("total", 0),
                        "input_total": result.get("input_total", result.get("total", 0)),
                        "eligible_document_count": result.get(
                            "eligible_document_count", result.get("input_total", 0)
                        ),
                        "selected_target_count": result.get(
                            "selected_target_count", result.get("total", 0)
                        ),
                        "coalesced_count": result.get("coalesced_count", 0),
                        "superseded_count": result.get("superseded_count", 0),
                        "attempted": result.get("attempted", result.get("total", 0)),
                        "completed": result.get("completed", 0),
                        "incomplete": result.get("incomplete", 0),
                        "retryable": result.get("retryable", 0),
                        "manual_review": result.get("manual_review", 0),
                        "ambiguous": result.get("ambiguous", 0),
                        "needs_review": result.get("needs_review", 0),
                        "missing_required": result.get("missing_required", 0),
                        "failed": result.get("failed", 0),
                        "by_source": result.get("by_source", {}),
                    }
                    err = result.get("error") or (
                        f"all selected POD items remain incomplete "
                        f"(incomplete={result.get('incomplete', 0)}, "
                        f"retryable={result.get('retryable', 0)})"
                    )
                    print(f"[FAILED] 推送失败: {err}")
                    record("odoo_push", False, err)
                    return {
                        "ok": False,
                        "status": "failed",
                        "failed_step": "odoo_push",
                        "degraded": bool(soft_failed_steps),
                        "soft_failed_steps": soft_failed_steps,
                        "error": err,
                        "steps": steps,
                        "data_report": data_report,
                        "elapsed_seconds": round(time.time() - overall_start, 1),
                    }
            except Exception as exc:
                print(f"[FAILED] 推送异常: {exc}")
                record("odoo_push", False, str(exc))
                return {
                    "ok": False,
                    "status": "failed",
                    "failed_step": "odoo_push",
                    "degraded": bool(soft_failed_steps),
                    "soft_failed_steps": soft_failed_steps,
                    "error": str(exc),
                    "steps": steps,
                    "data_report": data_report,
                    "elapsed_seconds": round(time.time() - overall_start, 1),
                }

    # Step 6: Refresh reports after any Odoo write, so reconcile pages do not show stale POD state.
    if not skip_reports:
        print("\n" + "=" * 60)
        print("STEP: 刷新 Odoo POD 索引和统一报告")
        print("=" * 60)
        from webapp.odoo_missing_report import refresh_odoo_missing_snapshot
        from webapp.odoo_pod_compare import refresh_pod_odoo_compare_snapshot

        # A report refresh is one generation: Query65-equivalent index,
        # Query64-equivalent picking snapshot, then the derived comparison.
        # Refresh the index even when this run did not push anything.
        should_refresh_pod_index = True
        pod_index_refreshed = False
        pod_index_refresh_ok = False
        if should_refresh_pod_index:
            try:
                from webapp.odoo_pull import fetch_and_save_index

                index = fetch_and_save_index(config_path)
                data_report["odoo_pod_index"] = index.summary()
                print("[OK] Odoo POD 索引已刷新")
                record("refresh_odoo_pod_index", True)
                pod_index_refreshed = True
                pod_index_refresh_ok = index.coverage_status == "complete"
                if index.coverage_status != "complete":
                    warnings.append(
                        {
                            "step": "refresh_odoo_pod_index_coverage",
                            "warning": index.coverage_status,
                        }
                    )
                if index.shadow_status in {
                    "differences",
                    "inconclusive_concurrent_changes",
                    "unavailable",
                }:
                    warnings.append(
                        {
                            "step": "refresh_odoo_pod_index_shadow",
                            "warning": index.shadow_status,
                        }
                    )
            except Exception as exc:
                print(f"[WARNING] Odoo POD 索引刷新失败，保留上一代索引: {exc}")
                record("refresh_odoo_pod_index", False, str(exc))
                warnings.append(
                    {"step": "refresh_odoo_pod_index", "error_type": type(exc).__name__}
                )

        missing_refresh_ok = False
        try:
            snapshot = refresh_odoo_missing_snapshot(
                config_path=config_path,
                refresh_xmlrpc_cache=not pod_index_refreshed,
            )
            data_report["odoo_missing"] = {
                "read_backend": snapshot.get("read_backend"),
                "authoritative_source": snapshot.get("authoritative_source"),
                "shadow_comparison": snapshot.get("shadow_comparison"),
                "warnings": snapshot.get("warnings") or [],
            }
            print("[OK] Odoo 缺失追踪报告已刷新")
            record("refresh_odoo_missing", True)
            missing_refresh_ok = True
            if snapshot.get("warnings"):
                warnings.append(
                    {"step": "refresh_odoo_missing_shadow", "warning": "unavailable"}
                )
            shadow_status = (snapshot.get("shadow_comparison") or {}).get("status")
            if shadow_status in {
                "differences",
                "inconclusive_concurrent_changes",
                "unavailable",
            }:
                warnings.append(
                    {
                        "step": "refresh_odoo_missing_shadow",
                        "warning": shadow_status,
                    }
                )
        except Exception as exc:
            print(f"[WARNING] Odoo 缺失追踪报告刷新失败，保留旧报告: {exc}")
            record("refresh_odoo_missing", False, str(exc))
            warnings.append(
                {"step": "refresh_odoo_missing", "error_type": type(exc).__name__}
            )

        if pod_index_refresh_ok and missing_refresh_ok:
            try:
                refresh_pod_odoo_compare_snapshot()
                print("[OK] POD vs Odoo 对比报告已刷新")
                record("refresh_pod_compare", True)
            except Exception as exc:
                print(f"[WARNING] POD vs Odoo 对比报告刷新失败，保留旧报告: {exc}")
                record("refresh_pod_compare", False, str(exc))
                warnings.append(
                    {"step": "refresh_pod_compare", "error_type": type(exc).__name__}
                )
        else:
            print("[WARNING] 上游报告未完整刷新，跳过 POD vs Odoo 对比以避免混合代数据")
            record("refresh_pod_compare", False, "skipped_due_to_upstream_refresh_failure")

    elapsed = round(time.time() - overall_start, 1)
    print(f"\n{'=' * 60}")
    print(f"PIPELINE FINISHED: {len(steps)} steps, {elapsed}s")
    print(f"{'=' * 60}")
    if soft_failed_steps:
        return {
            "ok": False,
            "status": "partial",
            "business_status": (
                "attention" if business_attention_steps else "complete"
            ),
            "business_attention_steps": business_attention_steps,
            "degraded": True,
            "failed_step": soft_failed_steps[0],
            "soft_failed_steps": soft_failed_steps,
            "warnings": warnings,
            "steps": steps,
            "data_report": data_report,
            "elapsed_seconds": elapsed,
        }
    business_steps = {
        "westerman_sync", "cmc_sync", "emtc_sync", "phase1a", "odoo_push"
    }
    successful_business_step = any(
        step.get("step") in business_steps and step.get("ok") for step in steps
    )
    if warnings and not successful_business_step:
        return {
            "ok": False,
            "status": "failed",
            "degraded": True,
            "failed_step": warnings[0]["step"],
            "soft_failed_steps": [],
            "warnings": warnings,
            "steps": steps,
            "data_report": data_report,
            "elapsed_seconds": elapsed,
        }
    return {
        "ok": True,
        "status": "ok",
        "business_status": (
            "attention" if business_attention_steps else "complete"
        ),
        "business_attention_steps": business_attention_steps,
        "degraded": bool(warnings),
        "soft_failed_steps": [],
        "warnings": warnings,
        "steps": steps,
        "data_report": data_report,
        "elapsed_seconds": elapsed,
    }


def main():
    parser = argparse.ArgumentParser(description="Run full POD sync pipeline.")
    parser.add_argument("--config", default="config.local.json", help="Path to sync config JSON")
    parser.add_argument("--phase1a-config", default=None, help="Path to Phase 1A config JSON")
    parser.add_argument("--full-sync", action="store_true", help="Disable incremental mode (full sync)")
    parser.add_argument(
        "--cmc-full-sync",
        action="store_true",
        help="Disable incremental mode for CMC only",
    )
    parser.add_argument(
        "--emtc-full-sync",
        action="store_true",
        help="Disable incremental mode for EMTC only",
    )
    parser.add_argument("--cmc-limit", type=int, default=0, help="CMC download limit (0=unlimited)")
    parser.add_argument("--emtc-limit", type=int, default=0, help="EMTC download limit (0=unlimited)")
    parser.add_argument("--cmc-workers", type=int, default=3, help="CMC concurrent workers")
    parser.add_argument("--emtc-workers", type=int, default=3, help="EMTC concurrent workers")
    parser.add_argument(
        "--westerman-limit",
        type=int,
        default=0,
        help="Westerman due-item limit (0=westerman_api.pool_batch_limit, default 100)",
    )
    parser.add_argument("--westerman-workers", type=int, default=3, help="Westerman concurrent workers")
    parser.add_argument("--skip-cmc", action="store_true")
    parser.add_argument("--skip-emtc", action="store_true")
    parser.add_argument("--skip-westerman", action="store_true")
    parser.add_argument("--skip-phase1a", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--skip-odoo-push", action="store_true", help="Skip pushing POD links to Odoo")
    parser.add_argument("--odoo-push-limit", type=int, default=0, help="Max items to push to Odoo (0=unlimited)")
    parser.add_argument(
        "--odoo-push-payload",
        default=None,
        help=(
            "Explicit Phase 1A payload to reuse; only valid with "
            "--skip-phase1a"
        ),
    )
    args = parser.parse_args()

    result = run_pipeline(
        config_path=args.config,
        cmc_incremental=not (args.full_sync or args.cmc_full_sync),
        emtc_incremental=not (args.full_sync or args.emtc_full_sync),
        phase1a_config_path=args.phase1a_config,
        cmc_limit=args.cmc_limit,
        emtc_limit=args.emtc_limit,
        cmc_workers=args.cmc_workers,
        emtc_workers=args.emtc_workers,
        westerman_limit=args.westerman_limit,
        westerman_workers=args.westerman_workers,
        skip_cmc=args.skip_cmc,
        skip_emtc=args.skip_emtc,
        skip_westerman=args.skip_westerman,
        skip_phase1a=args.skip_phase1a,
        skip_reports=args.skip_reports,
        skip_odoo_push=args.skip_odoo_push,
        odoo_push_limit=args.odoo_push_limit,
        odoo_push_payload_path=args.odoo_push_payload,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
