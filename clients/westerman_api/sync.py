#!/usr/bin/env python3
"""Refresh and process the durable Westerman POD pull pool."""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    ODOO_MUTATION_LOCK_PATH,
    exclusive_file_lock,
    normalize_runtime_config,
    resolve_config_relative_path,
)
from clients.westerman_api import probe
from clients.westerman_api.candidates import fetch_candidates
from clients.westerman_api.pool import (
    DEFAULT_FAILED_REQUEUE_SECONDS,
    DEFAULT_POOL_PATH,
    WestermanPodPool,
)


DEFAULT_BATCH_LIMIT = 100
DEFAULT_WORKERS = 3
DEFAULT_LEASE_SECONDS = 6 * 60 * 60
DEFAULT_NO_POD_RETRY_SECONDS = 24 * 60 * 60
DEFAULT_RETRY_BASE_SECONDS = 15 * 60
DEFAULT_RETRY_MAX_SECONDS = 24 * 60 * 60
DEFAULT_AUTH_RETRY_SECONDS = 6 * 60 * 60

_SAFE_CODE = re.compile(r"[^A-Z0-9_.:-]+")


def _bounded_int(value, *, default, minimum, maximum, label):
    if value in (None, ""):
        value = default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _hours_as_seconds(value, *, default_hours, maximum_hours, label):
    hours = _bounded_int(
        value,
        default=default_hours,
        minimum=1,
        maximum=maximum_hours,
        label=label,
    )
    return hours * 60 * 60


def _safe_error_code(prefix, value=None):
    suffix = ""
    if value not in (None, ""):
        suffix = _SAFE_CODE.sub("_", str(value).upper()).strip("_")[:80]
    return f"{prefix}_{suffix}" if suffix else prefix


def load_runtime_config(config_path):
    resolved = Path(config_path).resolve()
    with resolved.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("configuration root must be an object")
    return normalize_runtime_config(payload, resolved), resolved


def resolve_pool_path(source_cfg, config_path, explicit_pool_path=None):
    raw_path = explicit_pool_path or source_cfg.get("pool_path")
    if raw_path:
        return resolve_config_relative_path(raw_path, config_path)
    return DEFAULT_POOL_PATH


def _settings(source_cfg, *, limit, workers):
    configured_limit = _bounded_int(
        source_cfg.get("pool_batch_limit"),
        default=DEFAULT_BATCH_LIMIT,
        minimum=1,
        maximum=5000,
        label="westerman_api.pool_batch_limit",
    )
    effective_limit = configured_limit if limit in (None, 0) else _bounded_int(
        limit,
        default=configured_limit,
        minimum=1,
        maximum=5000,
        label="limit",
    )
    configured_workers = _bounded_int(
        source_cfg.get("pool_workers"),
        default=DEFAULT_WORKERS,
        minimum=1,
        maximum=16,
        label="westerman_api.pool_workers",
    )
    effective_workers = configured_workers if workers in (None, 0) else _bounded_int(
        workers,
        default=configured_workers,
        minimum=1,
        maximum=16,
        label="workers",
    )
    return {
        "limit": effective_limit,
        "workers": effective_workers,
        "lease_seconds": _bounded_int(
            source_cfg.get("pool_lease_seconds"),
            default=DEFAULT_LEASE_SECONDS,
            minimum=60,
            maximum=24 * 60 * 60,
            label="westerman_api.pool_lease_seconds",
        ),
        "no_pod_retry_seconds": _hours_as_seconds(
            source_cfg.get("no_pod_retry_hours"),
            default_hours=24,
            maximum_hours=24 * 30,
            label="westerman_api.no_pod_retry_hours",
        ),
        "retry_base_seconds": _bounded_int(
            source_cfg.get("retry_base_seconds"),
            default=DEFAULT_RETRY_BASE_SECONDS,
            minimum=60,
            maximum=24 * 60 * 60,
            label="westerman_api.retry_base_seconds",
        ),
        "retry_max_seconds": _bounded_int(
            source_cfg.get("retry_max_seconds"),
            default=DEFAULT_RETRY_MAX_SECONDS,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
            label="westerman_api.retry_max_seconds",
        ),
        "auth_retry_seconds": _bounded_int(
            source_cfg.get("auth_retry_seconds"),
            default=DEFAULT_AUTH_RETRY_SECONDS,
            minimum=5 * 60,
            maximum=7 * 24 * 60 * 60,
            label="westerman_api.auth_retry_seconds",
        ),
        "failed_requeue_seconds": _hours_as_seconds(
            source_cfg.get("failed_requeue_hours"),
            default_hours=DEFAULT_FAILED_REQUEUE_SECONDS // (60 * 60),
            maximum_hours=24 * 30,
            label="westerman_api.failed_requeue_hours",
        ),
    }


def _retry_seconds(settings, consecutive_failure_count):
    """Return exponential delay for this consecutive failure only."""

    exponent = min(max(int(consecutive_failure_count or 1) - 1, 0), 8)
    return min(
        settings["retry_max_seconds"],
        settings["retry_base_seconds"] * (2**exponent),
    )


def _next_failure_count(item):
    """Return the count that the pending failure transition will persist."""

    return int(item.get("consecutive_failure_count") or 0) + 1


def _transition_result(ok, outcome, **values):
    if not ok:
        return {"outcome": "lease_conflict", "checked": values.pop("checked", 0), **values}
    return {"outcome": outcome, **values}


def _process_claim(
    item,
    *,
    pool,
    lease_owner,
    source_cfg,
    download_cfg,
    settings,
    auth_event,
    session_getter,
    probe_runner,
):
    reference = item["shipment_reference"]
    if auth_event.is_set():
        updated = pool.mark_retry(
            reference,
            lease_owner=lease_owner,
            error_code="AUTH_BLOCKED",
            retry_seconds=settings["auth_retry_seconds"],
        )
        return _transition_result(
            updated,
            "retry",
            checked=0,
            retry_count=1,
            auth_blocked_count=1,
        )

    args = SimpleNamespace(download=True, output_dir=None)
    try:
        result = probe_runner(
            args,
            source_cfg,
            download_cfg,
            reference,
            session=session_getter(),
            emit=False,
            raise_on_document_failure=False,
            fallback_tracking_number=item.get("tracking_number"),
        )
    except probe.WestermanError as exc:
        if exc.auth_error:
            auth_event.set()
            updated = pool.mark_retry(
                reference,
                lease_owner=lease_owner,
                error_code="PROVIDER_AUTH_ERROR",
                retry_seconds=settings["auth_retry_seconds"],
                http_status=exc.http_status,
            )
            return _transition_result(
                updated,
                "retry",
                checked=1,
                retry_count=1,
                provider_auth_failed=1,
            )
        if exc.retryable:
            updated = pool.mark_retry(
                reference,
                lease_owner=lease_owner,
                error_code=_safe_error_code("LOOKUP_RETRY", exc.error_code),
                retry_seconds=_retry_seconds(settings, _next_failure_count(item)),
                http_status=exc.http_status,
            )
            return _transition_result(updated, "retry", checked=1, retry_count=1)
        updated = pool.mark_failed(
            reference,
            lease_owner=lease_owner,
            error_code=_safe_error_code("LOOKUP_FAILED", exc.error_code),
            retry_seconds=settings["failed_requeue_seconds"],
            http_status=exc.http_status,
        )
        return _transition_result(updated, "failed", checked=1, failed_count=1)
    except SystemExit:
        updated = pool.mark_failed(
            reference,
            lease_owner=lease_owner,
            error_code="PROBE_VALIDATION_FAILED",
            retry_seconds=settings["failed_requeue_seconds"],
        )
        return _transition_result(updated, "failed", checked=1, failed_count=1)
    except Exception:
        updated = pool.mark_retry(
            reference,
            lease_owner=lease_owner,
            error_code="UNEXPECTED_WORKER_ERROR",
            retry_seconds=_retry_seconds(settings, _next_failure_count(item)),
        )
        return _transition_result(updated, "retry", checked=1, retry_count=1)

    downloaded_count = int(result.get("downloaded_count") or 0)
    already_downloaded_count = int(result.get("already_downloaded_count") or 0)
    ready_count = downloaded_count + already_downloaded_count
    failed_documents = [
        value for value in (result.get("failed_documents") or []) if isinstance(value, dict)
    ]
    if failed_documents:
        http_status = next(
            (value.get("http_status") for value in failed_documents if value.get("http_status")),
            result.get("http_status"),
        )
        has_auth_error = any(value.get("auth_error") for value in failed_documents)
        has_retryable_error = any(value.get("retryable") for value in failed_documents)
        if ready_count:
            # A verified POD is sufficient for the business flow regardless
            # of how another attachment failed. Authentication failures still
            # trip the batch circuit breaker, but must not hide this valid POD.
            if has_auth_error:
                auth_event.set()
            updated = pool.mark_downloaded(
                reference,
                lease_owner=lease_owner,
                document_count=ready_count,
                http_status=http_status,
                error_code="PARTIAL_DOCUMENT_FAILURE",
            )
            return _transition_result(
                updated,
                "downloaded",
                checked=1,
                downloaded_shipment_count=1,
                downloaded_count=downloaded_count,
                already_downloaded_count=already_downloaded_count,
                partial_document_failure_count=1,
                provider_auth_failed=1 if has_auth_error else 0,
            )
        if has_auth_error:
            auth_event.set()
            updated = pool.mark_retry(
                reference,
                lease_owner=lease_owner,
                error_code="DOCUMENT_AUTH_ERROR",
                retry_seconds=settings["auth_retry_seconds"],
                http_status=http_status,
            )
            return _transition_result(
                updated,
                "retry",
                checked=1,
                retry_count=1,
                provider_auth_failed=1,
                downloaded_count=downloaded_count,
                already_downloaded_count=already_downloaded_count,
            )
        if has_retryable_error:
            updated = pool.mark_retry(
                reference,
                lease_owner=lease_owner,
                error_code="DOCUMENT_DOWNLOAD_RETRY",
                retry_seconds=_retry_seconds(settings, _next_failure_count(item)),
                http_status=http_status,
            )
            return _transition_result(
                updated,
                "retry",
                checked=1,
                retry_count=1,
                downloaded_count=downloaded_count,
                already_downloaded_count=already_downloaded_count,
            )
        updated = pool.mark_failed(
            reference,
            lease_owner=lease_owner,
            error_code="DOCUMENT_DOWNLOAD_FAILED",
            retry_seconds=settings["failed_requeue_seconds"],
            http_status=http_status,
        )
        return _transition_result(updated, "failed", checked=1, failed_count=1)

    if ready_count:
        document_count = max(
            ready_count,
            int(result.get("pod_document_count") or 0),
            int(result.get("download_candidate_count") or 0),
        )
        updated = pool.mark_downloaded(
            reference,
            lease_owner=lease_owner,
            document_count=document_count,
            http_status=result.get("http_status"),
        )
        return _transition_result(
            updated,
            "downloaded",
            checked=1,
            downloaded_shipment_count=1,
            downloaded_count=downloaded_count,
            already_downloaded_count=already_downloaded_count,
        )

    updated = pool.mark_waiting(
        reference,
        lease_owner=lease_owner,
        wait_seconds=settings["no_pod_retry_seconds"],
        document_count=int(result.get("document_count") or 0),
        http_status=result.get("http_status"),
    )
    return _transition_result(updated, "waiting_pod", checked=1, waiting_pod_count=1)


def _base_summary(*, pool_path, settings):
    return {
        "status": "ok",
        "pool_path": str(pool_path),
        "candidate_refresh_status": "not_run",
        "scanned_count": 0,
        "scan_truncated": False,
        "candidate_count": 0,
        "already_completed_count": 0,
        "completed_count": 0,
        "expired_count": 0,
        "recovered_lease_count": 0,
        "due_count": 0,
        "checked_count": 0,
        "waiting_pod_count": 0,
        "downloaded_shipment_count": 0,
        "downloaded_count": 0,
        "already_downloaded_count": 0,
        "retry_count": 0,
        "failed_count": 0,
        "partial_document_failure_count": 0,
        "auth_blocked_count": 0,
        "lease_conflict_count": 0,
        "provider_auth_failed": False,
        "batch_limit": settings["limit"],
        "workers": settings["workers"],
    }


def run_sync(
    *,
    config_path,
    limit=0,
    workers=0,
    pool_path=None,
    skip_candidate_refresh=False,
    candidate_fetcher=fetch_candidates,
    probe_runner=probe._run_indicium,
    session_factory=probe.create_session,
):
    full_cfg, resolved_config = load_runtime_config(config_path)
    source_cfg = dict(full_cfg.get("westerman_api") or {})
    settings = _settings(source_cfg, limit=limit, workers=workers)
    resolved_pool = resolve_pool_path(source_cfg, resolved_config, pool_path)
    summary = _base_summary(pool_path=resolved_pool, settings=settings)

    if source_cfg.get("enabled") is not True:
        summary.update({"status": "disabled", "candidate_refresh_status": "disabled"})
        return summary, 0

    pool = WestermanPodPool(resolved_pool)
    # Recover old claims before candidate reconciliation so a shipment that
    # also left the Odoo scope can be expired in this same refresh.
    summary["recovered_lease_count"] = pool.recover_stale_leases()
    candidate_refresh_failed = False
    candidate_scan_truncated = False
    if skip_candidate_refresh:
        summary["candidate_refresh_status"] = "skipped"
        pool.set_candidate_refresh_status("skipped")
    else:
        # Candidate reconciliation and Odoo mutation use the same cross-
        # process lock. A picking cannot leave the approved pool scope between
        # the mutation gate check and the corresponding Odoo write.
        with exclusive_file_lock(ODOO_MUTATION_LOCK_PATH):
            # Persist an in-progress marker before the remote read. A crash
            # therefore fails closed until a full scan records ``complete``.
            pool.set_candidate_refresh_status("running")
            try:
                refresh = candidate_fetcher(full_cfg)
                summary.update(
                    {
                        "scanned_count": int(refresh.get("scanned_count") or 0),
                        "scan_truncated": bool(refresh.get("scan_truncated")),
                        "candidate_count": int(refresh.get("candidate_count") or 0),
                        "already_completed_count": int(refresh.get("already_completed_count") or 0),
                    }
                )
                candidate_scan_truncated = summary["scan_truncated"]
                if candidate_scan_truncated:
                    summary.update(
                        {
                            "status": "degraded",
                            "candidate_refresh_status": "truncated",
                            "error_code": "CANDIDATE_SCAN_TRUNCATED",
                        }
                    )
                pool.upsert_candidates(
                    refresh.get("candidates") or [],
                    # Failed candidates are intentionally reactivated only by
                    # a complete refresh and only after their cooldown.
                    reactivate_failed=not candidate_scan_truncated,
                )
                summary["completed_count"] = sum(
                    1
                    for reference in (refresh.get("completed_references") or [])
                    if pool.mark_completed(reference)
                )
                if not summary["scan_truncated"]:
                    active_references = {
                        str(candidate.get("shipment_reference") or "").strip()
                        for candidate in (refresh.get("candidates") or [])
                        if isinstance(candidate, dict)
                        and str(candidate.get("shipment_reference") or "").strip()
                    }
                    active_references.update(
                        str(reference).strip()
                        for reference in (refresh.get("completed_references") or [])
                        if str(reference).strip()
                    )
                    summary["expired_count"] = pool.expire_missing_candidates(
                        active_references
                    )
                    pool.set_candidate_refresh_status("complete")
                    summary["candidate_refresh_status"] = "complete"
                else:
                    pool.set_candidate_refresh_status("truncated")
            except Exception as exc:
                candidate_refresh_failed = True
                pool.set_candidate_refresh_status("failed")
                summary.update(
                    {
                        "status": "degraded",
                        "candidate_refresh_status": "failed",
                        "error_code": _safe_error_code(
                            "CANDIDATE_REFRESH_FAILED", type(exc).__name__
                        ),
                    }
                )

    try:
        api_mode = str(source_cfg.get("api_mode") or source_cfg.get("protocol") or "generic").casefold()
        if api_mode != "indicium":
            raise ValueError("batch sync requires Indicium mode")
        probe._configured_token(source_cfg)
        probe._endpoint_url(source_cfg, "order_info_path", "/get_order_info")
        probe._endpoint_url(source_cfg, "download_document_path", "/download_document")
        probe._validated_proxy_url(source_cfg)
        probe._provider_timezone(source_cfg)
    except (SystemExit, ValueError) as exc:
        summary.update(
            {
                "status": "failed",
                "error_code": _safe_error_code("CONFIG_INVALID", type(exc).__name__),
                "pool_summary": pool.summary(),
            }
        )
        return summary, 2

    due_before_claim = pool.summary()
    summary["due_total_before_claim"] = int(due_before_claim.get("due_count") or 0)
    lease_owner = f"run-{uuid.uuid4().hex}"
    claimed = pool.claim_due(
        lease_owner=lease_owner,
        limit=settings["limit"],
        lease_seconds=settings["lease_seconds"],
    )
    summary["due_count"] = len(claimed)
    auth_event = threading.Event()
    thread_local = threading.local()
    sessions = []
    sessions_lock = threading.Lock()

    def get_session():
        session = getattr(thread_local, "westerman_session", None)
        if session is None:
            session = session_factory(source_cfg)
            thread_local.westerman_session = session
            with sessions_lock:
                sessions.append(session)
        return session

    worker_crash_count = 0
    if claimed:
        with ThreadPoolExecutor(
            max_workers=min(settings["workers"], len(claimed)),
            thread_name_prefix="westerman-pod",
        ) as executor:
            futures = {
                executor.submit(
                    _process_claim,
                    item,
                    pool=pool,
                    lease_owner=lease_owner,
                    source_cfg=source_cfg,
                    download_cfg=full_cfg.get("download", {}),
                    settings=settings,
                    auth_event=auth_event,
                    session_getter=get_session,
                    probe_runner=probe_runner,
                ): item
                for item in claimed
            }
            for future in as_completed(futures):
                try:
                    outcome = future.result()
                except Exception:
                    item = futures[future]
                    try:
                        recovered = pool.mark_retry(
                            item["shipment_reference"],
                            lease_owner=lease_owner,
                            error_code="WORKER_CRASH",
                            retry_seconds=_retry_seconds(
                                settings, _next_failure_count(item)
                            ),
                        )
                    except Exception:
                        recovered = False
                    if recovered:
                        summary["retry_count"] += 1
                    else:
                        worker_crash_count += 1
                    continue
                summary["checked_count"] += int(outcome.get("checked") or 0)
                for key in (
                    "waiting_pod_count",
                    "downloaded_shipment_count",
                    "downloaded_count",
                    "already_downloaded_count",
                    "retry_count",
                    "failed_count",
                    "partial_document_failure_count",
                    "auth_blocked_count",
                    "provider_auth_failed",
                ):
                    summary[key] += int(outcome.get(key) or 0)
                if outcome.get("outcome") == "lease_conflict":
                    summary["lease_conflict_count"] += 1

    for session in sessions:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    summary["provider_auth_failed"] = bool(summary["provider_auth_failed"])
    summary["pool_summary"] = pool.summary()
    if worker_crash_count:
        summary["worker_crash_count"] = worker_crash_count
        summary["status"] = "failed"
        summary["error_code"] = "WORKER_CRASH"
        return summary, 4
    if summary["provider_auth_failed"]:
        summary["status"] = "degraded"
        summary["error_code"] = "PROVIDER_AUTH_ERROR"
        return summary, 3
    if summary["failed_count"] or summary["lease_conflict_count"]:
        summary["status"] = "degraded"
        summary["error_code"] = "ITEM_FAILURE"
        return summary, 4
    if candidate_refresh_failed or candidate_scan_truncated:
        return summary, 2
    return summary, 0


def main():
    parser = argparse.ArgumentParser(description="Refresh and process the Westerman POD pull pool.")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum due shipments this run (0=westerman_api.pool_batch_limit, default 100)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Concurrent Westerman workers (0=westerman_api.pool_workers, default 3)",
    )
    parser.add_argument("--pool-path", help="Override the configured SQLite pool path")
    parser.add_argument("--skip-candidate-refresh", action="store_true")
    args = parser.parse_args()

    try:
        result, exit_code = run_sync(
            config_path=args.config,
            limit=args.limit,
            workers=args.workers,
            pool_path=args.pool_path,
            skip_candidate_refresh=args.skip_candidate_refresh,
        )
    except Exception as exc:
        result = {
            "status": "failed",
            "error_code": _safe_error_code("STARTUP_FAILED", type(exc).__name__),
        }
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
