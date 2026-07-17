"""Shared fail-closed gate for Westerman Phase and Odoo push inputs."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CANDIDATE_REFRESH_MAX_AGE_HOURS = 48
MAX_CANDIDATE_REFRESH_MAX_AGE_HOURS = 24 * 30
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5 * 60


def _max_age_hours(value) -> float:
    if value in (None, ""):
        return float(DEFAULT_CANDIDATE_REFRESH_MAX_AGE_HOURS)
    if isinstance(value, bool):
        raise ValueError("candidate refresh max age must be a number")
    try:
        hours = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate refresh max age must be a number") from exc
    if (
        not math.isfinite(hours)
        or hours < 1
        or hours > MAX_CANDIDATE_REFRESH_MAX_AGE_HOURS
    ):
        raise ValueError(
            "candidate refresh max age must be between 1 and "
            f"{MAX_CANDIDATE_REFRESH_MAX_AGE_HOURS} hours"
        )
    return hours


def _utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        value = datetime.fromisoformat(normalized)
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be an ISO-8601 value")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _base_result(max_age_hours) -> dict:
    try:
        normalized_max_age = _max_age_hours(max_age_hours)
    except ValueError:
        normalized_max_age = None
    return {
        "status": "invalid_freshness_config" if normalized_max_age is None else "unknown",
        "candidate_refresh_status": "unknown",
        "candidate_refresh_updated_at": None,
        "candidate_refresh_age_seconds": None,
        "candidate_refresh_max_age_hours": normalized_max_age,
        "eligible_references": set(),
    }


def evaluate_westerman_pool_gate(
    pool_path: str | Path | None,
    *,
    max_age_hours=None,
    now: datetime | str | None = None,
) -> dict:
    """Return the current Westerman allow-list and fail-closed gate status.

    A pool is usable only when its most recent candidate refresh completed and
    the completion timestamp is recent enough.  Operational failures are
    represented as status values so one provider cannot abort other sources.
    """

    result = _base_result(max_age_hours)
    if result["status"] == "invalid_freshness_config":
        return result
    if not pool_path:
        result["status"] = "missing_pool_path"
        return result

    try:
        resolved_pool_path = Path(pool_path).resolve()
    except (OSError, TypeError, ValueError):
        result["status"] = "pool_unavailable"
        return result
    result["pool_path"] = str(resolved_pool_path)
    try:
        pool_exists = resolved_pool_path.is_file()
    except OSError:
        result["status"] = "pool_unavailable"
        return result
    if not pool_exists:
        result["status"] = "missing_pool"
        return result

    try:
        connection = sqlite3.connect(
            f"{resolved_pool_path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            refresh_row = connection.execute(
                """
                SELECT meta_value, updated_at
                FROM pod_pool_meta
                WHERE meta_key = 'candidate_refresh_status'
                """
            ).fetchone()
            refresh_status = str(refresh_row[0] if refresh_row else "unknown")
            refresh_updated_at = refresh_row[1] if refresh_row else None
            result["candidate_refresh_status"] = refresh_status
            result["candidate_refresh_updated_at"] = refresh_updated_at
            if refresh_status != "complete":
                result["status"] = "candidate_refresh_incomplete"
                return result

            try:
                refreshed_at = _utc_datetime(refresh_updated_at)
                current_time = _utc_datetime(now)
            except (TypeError, ValueError):
                result["status"] = "candidate_refresh_stale"
                return result

            raw_age_seconds = (current_time - refreshed_at).total_seconds()
            if raw_age_seconds < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
                result["status"] = "candidate_refresh_stale"
                return result
            age_seconds = max(0.0, raw_age_seconds)
            result["candidate_refresh_age_seconds"] = int(age_seconds)
            if age_seconds > result["candidate_refresh_max_age_hours"] * 60 * 60:
                result["status"] = "candidate_refresh_stale"
                return result

            rows = connection.execute(
                "SELECT shipment_reference, pull_state, push_state FROM pod_pull_pool"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        result["status"] = "pool_unavailable"
        return result

    result["status"] = "ok"
    result["eligible_references"] = {
        str(reference).strip()
        for reference, pull_state, push_state in rows
        if str(reference or "").strip()
        and pull_state == "downloaded"
        and push_state == "pending"
    }
    return result
