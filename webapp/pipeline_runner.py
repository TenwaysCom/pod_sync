#!/usr/bin/env python3
"""POD Sync Pipeline Runner.

Executes the full chain:
  1. CMC OneDrive sync (incremental or full)
  2. EMTC Google Drive sync (incremental or full)
  3. Phase 1A parsing / matching
  4. Push POD links to Odoo
  5. Refresh Odoo POD index and unified reports

Usage:
    python webapp/pipeline_runner.py --config config.local.json
    python webapp/pipeline_runner.py --config config.local.json --full-sync
"""
from __future__ import annotations

import argparse
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


def _get_latest_phase1a_summary() -> dict | None:
    summary_paths = sorted(
        (PROJECT_ROOT / "phase1a" / "out").glob("*/phase1a_summary.json"),
        reverse=True,
    )
    if not summary_paths:
        return None
    try:
        return json.loads(summary_paths[0].read_text(encoding="utf-8"))
    except Exception:
        return None


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
    skip_cmc: bool = False,
    skip_emtc: bool = False,
    skip_phase1a: bool = False,
    skip_reports: bool = False,
    skip_odoo_push: bool = False,
    odoo_push_limit: int = 0,
) -> dict:
    resolved_sync_config = resolve_project_path(config_path, DEFAULT_SYNC_CONFIG_REL)
    resolved_phase1a_config = resolve_project_path(
        phase1a_config_path or DEFAULT_PHASE1A_CONFIG_REL,
        DEFAULT_PHASE1A_CONFIG_REL,
    )

    steps: list[dict] = []
    data_report: dict[str, dict] = {}
    overall_start = time.time()

    def record(step: str, ok: bool, detail: str = ""):
        steps.append({"step": step, "ok": ok, "detail": detail})

    # Step 1: CMC OneDrive sync
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
        if summary:
            data_report["cmc_sync"] = {
                "downloaded_files": summary.get("downloaded_files", 0),
                "skipped_files": summary.get("skipped_files", 0),
                "failed_files": summary.get("failed_files", 0),
                "downloaded_bytes": summary.get("downloaded_bytes", 0),
            }
        record("cmc_sync", rc == 0, f"exit_code={rc}")
        if rc != 0:
            return {"ok": False, "failed_step": "cmc_sync", "steps": steps, "data_report": data_report, "elapsed_seconds": round(time.time() - overall_start, 1)}

    # Step 2: EMTC Google Drive sync
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
        if summary:
            data_report["emtc_sync"] = {
                "downloaded_files": summary.get("downloaded_files", 0),
                "skipped_files": summary.get("skipped_files", 0),
                "failed_files": summary.get("failed_files", 0),
                "downloaded_bytes": summary.get("downloaded_bytes", 0),
            }
        record("emtc_sync", rc == 0, f"exit_code={rc}")
        if rc != 0:
            return {"ok": False, "failed_step": "emtc_sync", "steps": steps, "data_report": data_report, "elapsed_seconds": round(time.time() - overall_start, 1)}

    # Step 3: Phase 1A
    if not skip_phase1a:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "phase1a" / "run_phase1a.py"),
            "--config", str(resolved_phase1a_config),
        ]
        rc, _ = _run_step("Phase 1A 识别", cmd)
        phase1a_summary = _get_latest_phase1a_summary()
        if phase1a_summary:
            data_report["phase1a"] = {
                "document_count": phase1a_summary.get("document_count", 0),
                "ready_document_count": phase1a_summary.get("ready_document_count", 0),
                "review_document_count": phase1a_summary.get("review_document_count", 0),
                "payload_item_count": phase1a_summary.get("payload_item_count", 0),
                "matched_document_count": phase1a_summary.get("matched_document_count", 0),
                "unmatched_document_count": phase1a_summary.get("unmatched_document_count", 0),
                "run_name": phase1a_summary.get("run_name", ""),
            }
        record("phase1a", rc == 0, f"exit_code={rc}")
        if rc != 0:
            return {"ok": False, "failed_step": "phase1a", "steps": steps, "data_report": data_report, "elapsed_seconds": round(time.time() - overall_start, 1)}

    # Step 4: Push POD links to Odoo
    if not skip_odoo_push:
        print("\n" + "=" * 60)
        print("STEP: 推送 POD 链接到 Odoo")
        print("=" * 60)
        payload_dir = PROJECT_ROOT / "phase1a" / "out"
        payload_files = sorted(payload_dir.glob("*/odoo_backfill_payload.json"), reverse=True)
        if not payload_files:
            print("[SKIP] 未找到 odoo_backfill_payload.json，跳过推送")
            record("odoo_push", True, "no_payload_found")
            data_report["odoo_push"] = {"status": "skipped", "reason": "no_payload_found"}
        else:
            latest_payload = str(payload_files[0])
            print(f"Using payload: {latest_payload}")
            try:
                from webapp.odoo_push import push_payload
                result = push_payload(
                    config_path=config_path,
                    payload_path=latest_payload,
                    dry_run=False,
                    limit=odoo_push_limit,
                )
                if result.get("ok"):
                    data_report["odoo_push"] = {
                        "total": result.get("total", 0),
                        "tracking_pushed": result.get("tracking_pushed", 0),
                        "tracking_skipped_same": result.get("tracking_skipped_same", 0),
                        "tracking_not_found": result.get("tracking_not_found", 0),
                        "picking_pushed": result.get("picking_pushed", 0),
                        "picking_skipped_same": result.get("picking_skipped_same", 0),
                        "picking_not_found": result.get("picking_not_found", 0),
                        "skipped_existing": result.get("skipped_existing", 0),
                        "failed": result.get("failed", 0),
                        "by_source": result.get("by_source", {}),
                    }
                    detail = f"total={result.get('total',0)}, pushed={result.get('tracking_pushed',0)+result.get('picking_pushed',0)}, skipped={result.get('skipped_existing',0)+result.get('tracking_skipped_same',0)+result.get('picking_skipped_same',0)}, not_found={result.get('tracking_not_found',0)+result.get('picking_not_found',0)}, failed={result.get('failed',0)}"
                    print(f"[OK] 推送完成: {detail}")
                    record("odoo_push", True, detail)
                else:
                    err = result.get("error", "unknown")
                    print(f"[FAILED] 推送失败: {err}")
                    record("odoo_push", False, err)
                    return {
                        "ok": False,
                        "failed_step": "odoo_push",
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
                    "failed_step": "odoo_push",
                    "error": str(exc),
                    "steps": steps,
                    "data_report": data_report,
                    "elapsed_seconds": round(time.time() - overall_start, 1),
                }

    # Step 5: Refresh reports after any Odoo write, so reconcile pages do not show stale POD state.
    if not skip_reports:
        print("\n" + "=" * 60)
        print("STEP: 刷新 Odoo POD 索引和统一报告")
        print("=" * 60)
        try:
            from webapp.odoo_missing_report import refresh_odoo_missing_snapshot
            from webapp.odoo_pod_compare import refresh_pod_odoo_compare_snapshot

            should_refresh_pod_index = (
                not skip_odoo_push
                and data_report.get("odoo_push", {}).get("status") != "skipped"
            )
            if should_refresh_pod_index:
                from webapp.odoo_pull import fetch_and_save_index

                index = fetch_and_save_index(config_path)
                data_report["odoo_pod_index"] = index.summary()
                print("[OK] Odoo POD 索引已刷新")
                record("refresh_odoo_pod_index", True)

            refresh_odoo_missing_snapshot()
            print("[OK] Odoo 缺失追踪报告已刷新")
            record("refresh_odoo_missing", True)

            refresh_pod_odoo_compare_snapshot()
            print("[OK] POD vs Odoo 对比报告已刷新")
            record("refresh_pod_compare", True)
        except Exception as exc:
            print(f"[FAILED] 刷新报告失败: {exc}")
            record("refresh_reports", False, str(exc))
            return {
                "ok": False,
                "failed_step": "refresh_reports",
                "error": str(exc),
                "steps": steps,
                "data_report": data_report,
                "elapsed_seconds": round(time.time() - overall_start, 1),
            }

    elapsed = round(time.time() - overall_start, 1)
    print(f"\n{'=' * 60}")
    print(f"PIPELINE FINISHED: {len(steps)} steps, {elapsed}s")
    print(f"{'=' * 60}")
    return {"ok": True, "steps": steps, "data_report": data_report, "elapsed_seconds": elapsed}


def main():
    parser = argparse.ArgumentParser(description="Run full POD sync pipeline.")
    parser.add_argument("--config", default="config.local.json", help="Path to sync config JSON")
    parser.add_argument("--phase1a-config", default=None, help="Path to Phase 1A config JSON")
    parser.add_argument("--full-sync", action="store_true", help="Disable incremental mode (full sync)")
    parser.add_argument("--cmc-limit", type=int, default=0, help="CMC download limit (0=unlimited)")
    parser.add_argument("--emtc-limit", type=int, default=0, help="EMTC download limit (0=unlimited)")
    parser.add_argument("--cmc-workers", type=int, default=3, help="CMC concurrent workers")
    parser.add_argument("--emtc-workers", type=int, default=3, help="EMTC concurrent workers")
    parser.add_argument("--skip-cmc", action="store_true")
    parser.add_argument("--skip-emtc", action="store_true")
    parser.add_argument("--skip-phase1a", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--skip-odoo-push", action="store_true", help="Skip pushing POD links to Odoo")
    parser.add_argument("--odoo-push-limit", type=int, default=0, help="Max items to push to Odoo (0=unlimited)")
    args = parser.parse_args()

    result = run_pipeline(
        config_path=args.config,
        cmc_incremental=not args.full_sync,
        emtc_incremental=not args.full_sync,
        phase1a_config_path=args.phase1a_config,
        cmc_limit=args.cmc_limit,
        emtc_limit=args.emtc_limit,
        cmc_workers=args.cmc_workers,
        emtc_workers=args.emtc_workers,
        skip_cmc=args.skip_cmc,
        skip_emtc=args.skip_emtc,
        skip_phase1a=args.skip_phase1a,
        skip_reports=args.skip_reports,
        skip_odoo_push=args.skip_odoo_push,
        odoo_push_limit=args.odoo_push_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
