"""Durable work queue for Westerman POD polling.

The queue intentionally stores only operational identifiers and state.  API
tokens, response bodies, and exception messages must not be persisted here.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POOL_PATH = PROJECT_ROOT / "runtime" / "westerman" / "pod_pull_pool.sqlite3"
DEFAULT_FAILED_REQUEUE_SECONDS = 24 * 60 * 60

PULL_STATES = frozenset(
    {
        "pending",
        "checking",
        "waiting_pod",
        "downloaded",
        "retry",
        "failed",
        "expired",
        "completed",
    }
)
PUSH_STATES = frozenset(
    {"not_ready", "pending", "pushed", "needs_review", "failed", "completed"}
)
CLAIMABLE_PULL_STATES = ("pending", "waiting_pod", "retry")
CANDIDATE_REFRESH_STATES = frozenset(
    {"running", "complete", "failed", "truncated", "skipped"}
)

_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def utc_now_iso() -> str:
    """Return the current time as a sortable UTC ISO-8601 value."""

    return _as_utc_iso(datetime.now(timezone.utc))


def _as_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("UTC timestamp must not be empty")
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("UTC timestamp must be a valid ISO-8601 value") from exc
    if not isinstance(value, datetime):
        raise TypeError("UTC timestamp must be a datetime, ISO-8601 string, or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_iso(value: datetime | str | None) -> str:
    value_utc = _as_utc_datetime(value)
    return value_utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _future_iso(
    *,
    now: datetime | str | None,
    next_check_at: datetime | str | None,
    delay_seconds: int,
) -> str:
    if next_check_at is not None:
        return _as_utc_iso(next_check_at)
    if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or delay_seconds < 0:
        raise ValueError("delay_seconds must be a non-negative integer")
    return _as_utc_iso(_as_utc_datetime(now) + timedelta(seconds=delay_seconds))


def _required_text(value: object, field_name: str, *, max_length: int = 255) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return normalized


def _optional_text(value: object, field_name: str, *, max_length: int = 255) -> str | None:
    if value in (None, ""):
        return None
    return _required_text(value, field_name, max_length=max_length)


def _safe_error_code(value: object) -> str | None:
    if value in (None, ""):
        return None
    code = str(value).strip()
    if not _SAFE_CODE_RE.fullmatch(code):
        raise ValueError(
            "error_code must be an operational code containing only letters, digits, '.', ':', '-', or '_'"
        )
    return code


def _http_status(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("http_status must be an integer between 100 and 599")
    try:
        status = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("http_status must be an integer between 100 and 599") from exc
    if status < 100 or status > 599:
        raise ValueError("http_status must be an integer between 100 and 599")
    return status


def _document_count(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("document_count must be a non-negative integer")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("document_count must be a non-negative integer") from exc
    if count < 0:
        raise ValueError("document_count must be a non-negative integer")
    return count


class WestermanPodPool:
    """SQLite-backed pool of shipments that may acquire a Westerman POD."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        busy_timeout_seconds: int = 30,
        initialize: bool = True,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_POOL_PATH
        if not isinstance(busy_timeout_seconds, int) or busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be a positive integer")
        self.busy_timeout_seconds = busy_timeout_seconds
        if initialize:
            self.init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_seconds * 1000:d}")
        return connection

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init(self) -> "WestermanPodPool":
        """Create the database and schema if they do not already exist."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pod_pull_pool (
                    shipment_reference TEXT PRIMARY KEY NOT NULL,
                    odoo_picking_id TEXT,
                    tracking_number TEXT,
                    picking_state TEXT,
                    wms_status TEXT,
                    pull_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (pull_state IN (
                            'pending', 'checking', 'waiting_pod', 'downloaded',
                            'retry', 'failed', 'expired', 'completed'
                        )),
                    push_state TEXT NOT NULL DEFAULT 'not_ready'
                        CHECK (push_state IN (
                            'not_ready', 'pending', 'pushed', 'needs_review',
                            'failed', 'completed'
                        )),
                    first_seen_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    next_check_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    consecutive_failure_count INTEGER NOT NULL DEFAULT 0
                        CHECK (consecutive_failure_count >= 0),
                    last_http_status INTEGER,
                    last_error_code TEXT,
                    document_count INTEGER CHECK (document_count IS NULL OR document_count >= 0),
                    lease_owner TEXT,
                    lease_until TEXT,
                    downloaded_at TEXT,
                    completed_at TEXT,
                    deferred_terminal_state TEXT
                        CHECK (deferred_terminal_state IS NULL OR deferred_terminal_state IN (
                            'expired', 'completed'
                        )),
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pod_pull_pool)").fetchall()
            }
            if "tracking_number" not in columns:
                try:
                    connection.execute("ALTER TABLE pod_pull_pool ADD COLUMN tracking_number TEXT")
                except sqlite3.OperationalError:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(pod_pull_pool)").fetchall()
                    }
                    if "tracking_number" not in columns:
                        raise
            if "consecutive_failure_count" not in columns:
                try:
                    connection.execute(
                        "ALTER TABLE pod_pull_pool "
                        "ADD COLUMN consecutive_failure_count INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (consecutive_failure_count >= 0)"
                    )
                except sqlite3.OperationalError:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(pod_pull_pool)").fetchall()
                    }
                    if "consecutive_failure_count" not in columns:
                        raise
            if "deferred_terminal_state" not in columns:
                try:
                    connection.execute(
                        "ALTER TABLE pod_pull_pool "
                        "ADD COLUMN deferred_terminal_state TEXT "
                        "CHECK (deferred_terminal_state IS NULL OR "
                        "deferred_terminal_state IN ('expired', 'completed'))"
                    )
                except sqlite3.OperationalError:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(pod_pull_pool)").fetchall()
                    }
                    if "deferred_terminal_state" not in columns:
                        raise
            # Pre-v4 failed rows did not have a retry timestamp. Preserve the
            # original failure age when possible, so an old failure can be
            # reconsidered by the next complete refresh without becoming a
            # hot loop. Missing/malformed history starts a fresh safe cooldown.
            migration_time = datetime.now(timezone.utc)
            legacy_failed_rows = connection.execute(
                """
                SELECT shipment_reference, last_checked_at
                FROM pod_pull_pool
                WHERE pull_state = 'failed'
                  AND next_check_at IS NULL
                """
            ).fetchall()
            legacy_failed_schedules = []
            for row in legacy_failed_rows:
                last_checked_at = row["last_checked_at"]
                try:
                    failed_at = (
                        _as_utc_datetime(last_checked_at)
                        if last_checked_at not in (None, "")
                        else migration_time
                    )
                except (TypeError, ValueError):
                    failed_at = migration_time
                legacy_failed_schedules.append(
                    (
                        _as_utc_iso(
                            failed_at
                            + timedelta(seconds=DEFAULT_FAILED_REQUEUE_SECONDS)
                        ),
                        row["shipment_reference"],
                    )
                )
            connection.executemany(
                """
                UPDATE pod_pull_pool
                SET next_check_at = ?
                WHERE shipment_reference = ?
                  AND pull_state = 'failed'
                  AND next_check_at IS NULL
                """,
                legacy_failed_schedules,
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pod_pull_pool_due
                ON pod_pull_pool (pull_state, next_check_at, first_seen_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pod_pull_pool_lease
                ON pod_pull_pool (pull_state, lease_until)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pod_pool_meta (
                    meta_key TEXT PRIMARY KEY NOT NULL,
                    meta_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("PRAGMA user_version = 5")
        finally:
            connection.close()
        return self

    def set_candidate_refresh_status(
        self,
        status: str,
        *,
        now: datetime | str | None = None,
    ) -> None:
        """Persist whether the most recent Odoo candidate scan was complete."""

        normalized = str(status or "").strip().casefold()
        if normalized not in CANDIDATE_REFRESH_STATES:
            raise ValueError("unknown candidate refresh status")
        now_iso = _as_utc_iso(now)
        with self._immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO pod_pool_meta (meta_key, meta_value, updated_at)
                VALUES ('candidate_refresh_status', ?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET
                    meta_value = excluded.meta_value,
                    updated_at = excluded.updated_at
                """,
                (normalized, now_iso),
            )

    def candidate_refresh_status(self) -> dict[str, str | None]:
        connection = self._connect()
        try:
            try:
                row = connection.execute(
                    """
                    SELECT meta_value, updated_at
                    FROM pod_pool_meta
                    WHERE meta_key = 'candidate_refresh_status'
                    """
                ).fetchone()
            except sqlite3.OperationalError:
                # A read-only dashboard may open a pre-v3 pool before the next
                # sync process performs the normal schema migration.
                row = None
            if row is None:
                return {"status": "unknown", "updated_at": None}
            return {"status": row["meta_value"], "updated_at": row["updated_at"]}
        finally:
            connection.close()

    @staticmethod
    def _candidate(candidate: str | Mapping[str, object]) -> dict[str, str | None]:
        if isinstance(candidate, str):
            candidate = {"shipment_reference": candidate}
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be a shipment reference or mapping")
        reference = candidate.get("shipment_reference")
        if reference in (None, ""):
            reference = candidate.get("reference")
        if reference in (None, ""):
            reference = candidate.get("name")
        picking_id = candidate.get("odoo_picking_id")
        if picking_id in (None, ""):
            picking_id = candidate.get("picking_id")
        return {
            "shipment_reference": _required_text(reference, "shipment_reference"),
            "odoo_picking_id": _optional_text(picking_id, "odoo_picking_id", max_length=128),
            "tracking_number": _optional_text(
                candidate.get("tracking_number"), "tracking_number", max_length=255
            ),
            "picking_state": _optional_text(candidate.get("picking_state"), "picking_state", max_length=64),
            "wms_status": _optional_text(candidate.get("wms_status"), "wms_status", max_length=64),
        }

    def upsert_candidates(
        self,
        candidates: Iterable[str | Mapping[str, object]],
        *,
        reactivate_failed: bool = False,
        now: datetime | str | None = None,
    ) -> int:
        """Insert or refresh allow-listed Odoo candidate metadata.

        Existing processing state, attempt counters, and scheduling are normally
        preserved. A caller that completed the full Odoo scan may opt in to
        reactivating failed rows whose cooldown has elapsed. The return value is
        the number of unique shipment references processed.
        """

        if not isinstance(reactivate_failed, bool):
            raise TypeError("reactivate_failed must be a boolean")
        now_iso = _as_utc_iso(now)
        normalized = {}
        for candidate in candidates:
            item = self._candidate(candidate)
            normalized[item["shipment_reference"]] = item
        if not normalized:
            return 0
        failed_requeue_condition = (
            "pod_pull_pool.pull_state = 'failed' "
            "AND pod_pull_pool.next_check_at IS NOT NULL "
            "AND pod_pull_pool.next_check_at <= excluded.updated_at"
            if reactivate_failed
            else "0"
        )
        with self._immediate_transaction() as connection:
            connection.executemany(
                f"""
                INSERT INTO pod_pull_pool (
                    shipment_reference, odoo_picking_id, tracking_number, picking_state, wms_status,
                    pull_state, push_state, first_seen_at, next_check_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 'not_ready', ?, ?, ?)
                ON CONFLICT(shipment_reference) DO UPDATE SET
                    odoo_picking_id = COALESCE(excluded.odoo_picking_id, pod_pull_pool.odoo_picking_id),
                    tracking_number = COALESCE(excluded.tracking_number, pod_pull_pool.tracking_number),
                    picking_state = COALESCE(excluded.picking_state, pod_pull_pool.picking_state),
                    wms_status = COALESCE(excluded.wms_status, pod_pull_pool.wms_status),
                    pull_state = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN 'pending'
                        WHEN {failed_requeue_condition} THEN 'pending'
                        ELSE pod_pull_pool.pull_state
                    END,
                    push_state = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN 'not_ready'
                        WHEN {failed_requeue_condition} THEN 'not_ready'
                        ELSE pod_pull_pool.push_state
                    END,
                    next_check_at = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN excluded.next_check_at
                        WHEN {failed_requeue_condition} THEN excluded.next_check_at
                        ELSE pod_pull_pool.next_check_at
                    END,
                    consecutive_failure_count = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN 0
                        ELSE pod_pull_pool.consecutive_failure_count
                    END,
                    last_http_status = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN NULL
                        WHEN {failed_requeue_condition} THEN NULL
                        ELSE pod_pull_pool.last_http_status
                    END,
                    last_error_code = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN NULL
                        WHEN {failed_requeue_condition} THEN NULL
                        ELSE pod_pull_pool.last_error_code
                    END,
                    document_count = CASE
                        WHEN pod_pull_pool.pull_state = 'expired' THEN NULL
                        WHEN {failed_requeue_condition} THEN NULL
                        ELSE pod_pull_pool.document_count
                    END,
                    deferred_terminal_state = NULL,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        item["shipment_reference"],
                        item["odoo_picking_id"],
                        item["tracking_number"],
                        item["picking_state"],
                        item["wms_status"],
                        now_iso,
                        now_iso,
                        now_iso,
                    )
                    for item in normalized.values()
                ],
            )
        return len(normalized)

    @staticmethod
    def _recover_stale(connection: sqlite3.Connection, now_iso: str) -> int:
        cursor = connection.execute(
            """
            UPDATE pod_pull_pool
            SET pull_state = CASE deferred_terminal_state
                    WHEN 'expired' THEN 'expired'
                    WHEN 'completed' THEN 'completed'
                    ELSE 'retry'
                END,
                push_state = CASE deferred_terminal_state
                    WHEN 'expired' THEN 'not_ready'
                    WHEN 'completed' THEN 'completed'
                    ELSE push_state
                END,
                next_check_at = CASE
                    WHEN deferred_terminal_state IS NULL THEN ?
                    ELSE NULL
                END,
                consecutive_failure_count = CASE deferred_terminal_state
                    WHEN 'completed' THEN 0
                    WHEN 'expired' THEN consecutive_failure_count
                    ELSE consecutive_failure_count + 1
                END,
                lease_owner = NULL,
                lease_until = NULL,
                last_error_code = CASE deferred_terminal_state
                    WHEN 'expired' THEN 'ODOO_CANDIDATE_EXPIRED'
                    WHEN 'completed' THEN last_error_code
                    ELSE 'STALE_LEASE'
                END,
                completed_at = CASE deferred_terminal_state
                    WHEN 'completed' THEN COALESCE(completed_at, ?)
                    ELSE completed_at
                END,
                deferred_terminal_state = NULL,
                updated_at = ?
            WHERE pull_state = 'checking'
              AND lease_until IS NOT NULL
              AND lease_until <= ?
            """,
            (now_iso, now_iso, now_iso, now_iso),
        )
        return cursor.rowcount

    def recover_stale_leases(self, *, now: datetime | str | None = None) -> int:
        """Return expired in-flight work to the retry queue."""

        now_iso = _as_utc_iso(now)
        with self._immediate_transaction() as connection:
            return self._recover_stale(connection, now_iso)

    def claim_due(
        self,
        *,
        lease_owner: str,
        limit: int = 100,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        """Atomically lease due candidates and increment their attempt count."""

        owner = _required_text(lease_owner, "lease_owner", max_length=128)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now_utc = _as_utc_datetime(now)
        now_iso = _as_utc_iso(now_utc)
        lease_until = _as_utc_iso(now_utc + timedelta(seconds=lease_seconds))
        with self._immediate_transaction() as connection:
            self._recover_stale(connection, now_iso)
            rows = connection.execute(
                """
                SELECT shipment_reference
                FROM pod_pull_pool
                WHERE pull_state IN ('pending', 'waiting_pod', 'retry')
                  AND (next_check_at IS NULL OR next_check_at <= ?)
                ORDER BY COALESCE(next_check_at, first_seen_at), first_seen_at, shipment_reference
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
            references = [row["shipment_reference"] for row in rows]
            if not references:
                return []
            placeholders = ", ".join("?" for _ in references)
            connection.execute(
                f"""
                UPDATE pod_pull_pool
                SET pull_state = 'checking',
                    last_checked_at = ?,
                    attempt_count = attempt_count + 1,
                    lease_owner = ?,
                    lease_until = ?,
                    updated_at = ?
                WHERE shipment_reference IN ({placeholders})
                """,
                (now_iso, owner, lease_until, now_iso, *references),
            )
            claimed = connection.execute(
                f"""
                SELECT *
                FROM pod_pull_pool
                WHERE shipment_reference IN ({placeholders})
                ORDER BY COALESCE(next_check_at, first_seen_at), first_seen_at, shipment_reference
                """,
                references,
            ).fetchall()
            return [dict(row) for row in claimed]

    @staticmethod
    def _lease_where(lease_owner: str) -> tuple[str, Sequence[object]]:
        owner = _required_text(lease_owner, "lease_owner", max_length=128)
        return "pull_state = 'checking' AND lease_owner = ?", (owner,)

    def _finish_claim(
        self,
        shipment_reference: str,
        *,
        lease_owner: str,
        assignments: Mapping[str, object],
        increment_consecutive_failure: bool = False,
    ) -> bool:
        reference = _required_text(shipment_reference, "shipment_reference")
        lease_clause, lease_values = self._lease_where(lease_owner)
        if not assignments:
            raise ValueError("at least one assignment is required")
        allowed_columns = {
            "pull_state",
            "push_state",
            "last_checked_at",
            "next_check_at",
            "last_http_status",
            "last_error_code",
            "document_count",
            "lease_owner",
            "lease_until",
            "downloaded_at",
            "completed_at",
            "consecutive_failure_count",
            "deferred_terminal_state",
            "updated_at",
        }
        if not set(assignments).issubset(allowed_columns):
            raise ValueError("unsupported pool state assignment")
        set_parts = [f"{column} = ?" for column in assignments]
        if increment_consecutive_failure:
            set_parts.append(
                "consecutive_failure_count = consecutive_failure_count + 1"
            )
        set_clause = ", ".join(set_parts)
        values = list(assignments.values())
        with self._immediate_transaction() as connection:
            leased_row = connection.execute(
                f"""
                SELECT *
                FROM pod_pull_pool
                WHERE shipment_reference = ? AND {lease_clause}
                """,
                (reference, *lease_values),
            ).fetchone()
            if leased_row is None:
                return False
            deferred_terminal_state = leased_row["deferred_terminal_state"]
            if deferred_terminal_state in {"expired", "completed"}:
                terminal_time = assignments.get("updated_at") or utc_now_iso()

                def final_value(column: str) -> object:
                    if column in assignments:
                        return assignments[column]
                    return leased_row[column]

                terminal_failure_count = final_value(
                    "consecutive_failure_count"
                )
                if increment_consecutive_failure:
                    terminal_failure_count = int(terminal_failure_count or 0) + 1
                terminal_assignments = {
                    "pull_state": deferred_terminal_state,
                    "push_state": (
                        "completed"
                        if deferred_terminal_state == "completed"
                        else (
                            "needs_review"
                            if assignments.get("pull_state") == "downloaded"
                            else "not_ready"
                        )
                    ),
                    "last_checked_at": final_value("last_checked_at"),
                    "next_check_at": None,
                    "last_http_status": final_value("last_http_status"),
                    "last_error_code": (
                        final_value("last_error_code")
                        if deferred_terminal_state == "completed"
                        else "ODOO_CANDIDATE_EXPIRED"
                    ),
                    "document_count": final_value("document_count"),
                    "lease_owner": None,
                    "lease_until": None,
                    "downloaded_at": final_value("downloaded_at"),
                    "completed_at": (
                        leased_row["completed_at"] or terminal_time
                        if deferred_terminal_state == "completed"
                        else leased_row["completed_at"]
                    ),
                    "consecutive_failure_count": (
                        0
                        if deferred_terminal_state == "completed"
                        else terminal_failure_count
                    ),
                    "deferred_terminal_state": None,
                    "updated_at": terminal_time,
                }
                set_parts = [
                    f"{column} = ?" for column in terminal_assignments
                ]
                set_clause = ", ".join(set_parts)
                values = list(terminal_assignments.values())
            cursor = connection.execute(
                f"""
                UPDATE pod_pull_pool
                SET {set_clause}
                WHERE shipment_reference = ? AND {lease_clause}
                """,
                (*values, reference, *lease_values),
            )
            return cursor.rowcount == 1

    def mark_waiting(
        self,
        shipment_reference: str,
        *,
        lease_owner: str,
        next_check_at: datetime | str | None = None,
        wait_seconds: int = 24 * 60 * 60,
        document_count: int | None = 0,
        http_status: int | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        """Mark a successful lookup with no currently available POD."""

        now_iso = _as_utc_iso(now)
        return self._finish_claim(
            shipment_reference,
            lease_owner=lease_owner,
            assignments={
                "pull_state": "waiting_pod",
                "push_state": "not_ready",
                "last_checked_at": now_iso,
                "next_check_at": _future_iso(
                    now=now_iso,
                    next_check_at=next_check_at,
                    delay_seconds=wait_seconds,
                ),
                "last_http_status": _http_status(http_status),
                "last_error_code": None,
                "document_count": _document_count(document_count),
                "consecutive_failure_count": 0,
                "lease_owner": None,
                "lease_until": None,
                "updated_at": now_iso,
            },
        )

    def mark_downloaded(
        self,
        shipment_reference: str,
        *,
        lease_owner: str,
        document_count: int | None = None,
        http_status: int | None = None,
        error_code: str | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        """Mark POD files as durably downloaded and ready for Phase 1A."""

        now_iso = _as_utc_iso(now)
        return self._finish_claim(
            shipment_reference,
            lease_owner=lease_owner,
            assignments={
                "pull_state": "downloaded",
                "push_state": "pending",
                "last_checked_at": now_iso,
                "next_check_at": None,
                "last_http_status": _http_status(http_status),
                "last_error_code": _safe_error_code(error_code),
                "document_count": _document_count(document_count),
                "consecutive_failure_count": 0,
                "lease_owner": None,
                "lease_until": None,
                "downloaded_at": now_iso,
                "updated_at": now_iso,
            },
        )

    def mark_retry(
        self,
        shipment_reference: str,
        *,
        lease_owner: str,
        error_code: str,
        next_check_at: datetime | str | None = None,
        retry_seconds: int = 15 * 60,
        http_status: int | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        """Release a transient failure back to the due queue."""

        now_iso = _as_utc_iso(now)
        return self._finish_claim(
            shipment_reference,
            lease_owner=lease_owner,
            assignments={
                "pull_state": "retry",
                "last_checked_at": now_iso,
                "next_check_at": _future_iso(
                    now=now_iso,
                    next_check_at=next_check_at,
                    delay_seconds=retry_seconds,
                ),
                "last_http_status": _http_status(http_status),
                "last_error_code": _safe_error_code(error_code),
                "lease_owner": None,
                "lease_until": None,
                "updated_at": now_iso,
            },
            increment_consecutive_failure=True,
        )

    def mark_failed(
        self,
        shipment_reference: str,
        *,
        lease_owner: str,
        error_code: str,
        next_check_at: datetime | str | None = None,
        retry_seconds: int = DEFAULT_FAILED_REQUEUE_SECONDS,
        http_status: int | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        """Park a permanent failure until a later complete candidate refresh.

        ``failed`` rows are deliberately not claimable.  They receive a
        cooldown timestamp so only a later *complete* candidate refresh can
        reactivate them once the cooldown has elapsed; ordinary pool claims
        therefore cannot create a permanent-failure hot loop.
        """

        now_iso = _as_utc_iso(now)
        return self._finish_claim(
            shipment_reference,
            lease_owner=lease_owner,
            assignments={
                "pull_state": "failed",
                "last_checked_at": now_iso,
                "next_check_at": _future_iso(
                    now=now_iso,
                    next_check_at=next_check_at,
                    delay_seconds=retry_seconds,
                ),
                "last_http_status": _http_status(http_status),
                "last_error_code": _safe_error_code(error_code),
                "lease_owner": None,
                "lease_until": None,
                "updated_at": now_iso,
            },
            increment_consecutive_failure=True,
        )

    def mark_completed(
        self,
        shipment_reference: str,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        """Mark a shipment terminal when Odoo already has a stock.tracking POD.

        Odoo is the terminal source of truth, including PODs added outside this
        process.  For an actively leased row, preserve the lease and defer the
        terminal transition until that worker atomically releases its claim.
        """

        reference = _required_text(shipment_reference, "shipment_reference")
        now_iso = _as_utc_iso(now)
        with self._immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE pod_pull_pool
                SET pull_state = 'completed',
                    push_state = 'completed',
                    next_check_at = NULL,
                    consecutive_failure_count = 0,
                    lease_owner = NULL,
                    lease_until = NULL,
                    completed_at = COALESCE(completed_at, ?),
                    deferred_terminal_state = NULL,
                    updated_at = ?
                WHERE shipment_reference = ?
                  AND pull_state != 'checking'
                """,
                (now_iso, now_iso, reference),
            )
            if cursor.rowcount == 1:
                return True
            cursor = connection.execute(
                """
                UPDATE pod_pull_pool
                SET deferred_terminal_state = 'completed',
                    updated_at = ?
                WHERE shipment_reference = ?
                  AND pull_state = 'checking'
                """,
                (now_iso, reference),
            )
            return cursor.rowcount == 1

    def expire_missing_candidates(
        self,
        active_references: Iterable[str],
        *,
        now: datetime | str | None = None,
    ) -> int:
        """Stop polling rows absent from a complete Odoo candidate refresh.

        Active leases retain their processing state but receive a deferred
        terminal marker. Completed rows are never changed. Downloaded rows
        become ``needs_review`` so a lifecycle change cannot silently push a
        POD after the picking left the approved scope.
        """

        references = sorted(
            {
                _required_text(value, "shipment_reference")
                for value in active_references
            }
        )
        now_iso = _as_utc_iso(now)
        with self._immediate_transaction() as connection:
            connection.execute(
                "CREATE TEMP TABLE active_westerman_candidates "
                "(shipment_reference TEXT PRIMARY KEY NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO active_westerman_candidates (shipment_reference) VALUES (?)",
                ((reference,) for reference in references),
            )
            cursor = connection.execute(
                """
                UPDATE pod_pull_pool
                SET pull_state = 'expired',
                    push_state = CASE
                        WHEN push_state = 'pending' THEN 'needs_review'
                        ELSE 'not_ready'
                    END,
                    next_check_at = NULL,
                    lease_owner = NULL,
                    lease_until = NULL,
                    last_error_code = 'ODOO_CANDIDATE_EXPIRED',
                    updated_at = ?
                WHERE pull_state NOT IN ('checking', 'completed', 'expired')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM active_westerman_candidates active
                      WHERE active.shipment_reference = pod_pull_pool.shipment_reference
                  )
                """,
                (now_iso,),
            )
            connection.execute(
                """
                UPDATE pod_pull_pool
                SET deferred_terminal_state = 'expired',
                    updated_at = ?
                WHERE pull_state = 'checking'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM active_westerman_candidates active
                      WHERE active.shipment_reference = pod_pull_pool.shipment_reference
                  )
                """,
                (now_iso,),
            )
            return cursor.rowcount

    def get_item(self, shipment_reference: str) -> dict[str, object] | None:
        reference = _required_text(shipment_reference, "shipment_reference")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM pod_pull_pool WHERE shipment_reference = ?",
                (reference,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def list_items(
        self,
        *,
        pull_state: str | None = None,
        push_state: str | None = None,
        due_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        if pull_state is not None and pull_state not in PULL_STATES:
            raise ValueError("unknown pull_state")
        if push_state is not None and push_state not in PUSH_STATES:
            raise ValueError("unknown push_state")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        clauses = []
        values: list[object] = []
        if pull_state is not None:
            clauses.append("pull_state = ?")
            values.append(pull_state)
        if push_state is not None:
            clauses.append("push_state = ?")
            values.append(push_state)
        if due_only:
            clauses.append("pull_state IN ('pending', 'waiting_pod', 'retry')")
            clauses.append("(next_check_at IS NULL OR next_check_at <= ?)")
            values.append(_as_utc_iso(now))
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT *
                FROM pod_pull_pool
                {where_clause}
                ORDER BY first_seen_at, shipment_reference
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def summary(self, *, now: datetime | str | None = None) -> dict[str, object]:
        now_iso = _as_utc_iso(now)
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM pod_pull_pool").fetchone()[0]
            pull_rows = connection.execute(
                "SELECT pull_state, COUNT(*) AS item_count FROM pod_pull_pool GROUP BY pull_state"
            ).fetchall()
            push_rows = connection.execute(
                "SELECT push_state, COUNT(*) AS item_count FROM pod_pull_pool GROUP BY push_state"
            ).fetchall()
            due = connection.execute(
                """
                SELECT COUNT(*)
                FROM pod_pull_pool
                WHERE pull_state IN ('pending', 'waiting_pod', 'retry')
                  AND (next_check_at IS NULL OR next_check_at <= ?)
                """,
                (now_iso,),
            ).fetchone()[0]
            leased = connection.execute(
                """
                SELECT COUNT(*)
                FROM pod_pull_pool
                WHERE pull_state = 'checking'
                  AND lease_until IS NOT NULL
                  AND lease_until > ?
                """,
                (now_iso,),
            ).fetchone()[0]
            return {
                "total": total,
                "due_count": due,
                "leased_count": leased,
                "pull_states": {row["pull_state"]: row["item_count"] for row in pull_rows},
                "push_states": {row["push_state"]: row["item_count"] for row in push_rows},
                "candidate_refresh": self.candidate_refresh_status(),
            }
        finally:
            connection.close()


def init_pool(db_path: str | Path | None = None) -> WestermanPodPool:
    """Convenience factory used by command-line and scheduler integration."""

    return WestermanPodPool(db_path)


__all__ = [
    "CLAIMABLE_PULL_STATES",
    "DEFAULT_FAILED_REQUEUE_SECONDS",
    "DEFAULT_POOL_PATH",
    "PULL_STATES",
    "PUSH_STATES",
    "WestermanPodPool",
    "init_pool",
    "utc_now_iso",
]
