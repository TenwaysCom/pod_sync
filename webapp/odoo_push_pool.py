"""Durable incremental queue for POD writes to Odoo.

The Phase 1A payload is intentionally a complete business snapshot.  Replaying
that snapshot directly over XML-RPC is correct but expensive, so this module
keeps the delivery target/fingerprint state locally and only leases targets
which are new, changed, or due for a periodic audit.

Only operational metadata is persisted.  Signed storage URLs and Odoo
credentials are never stored in the pool.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from webapp.actions import resolve_project_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL_PATH = PROJECT_ROOT / "runtime" / "odoo_push" / "push_pool.sqlite3"
POOL_STATES = frozenset(
    {"pending", "leased", "completed", "retry", "review", "failed"}
)

_PERSISTED_ITEM_FIELDS = (
    "payload_item_id",
    "document_uid",
    "primary_document_uid",
    "source_file_id",
    "identifier_status",
    "preferred_key_type",
    "preferred_key",
    "local_path",
    "source_name",
    "source_type",
    "source_modified_at",
    "shipment_reference",
    "tracking_number",
    "file_name",
    "_cross_source_conflict",
    "conflict_sources",
    "conflict_providers",
    "conflict_documents",
)


def _utc_datetime(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime | str | None = None) -> str:
    return (
        _utc_datetime(value)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_int(
    value,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        parsed = int(default if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def resolve_push_pool_settings(
    full_cfg: dict,
    *,
    config_path: str = "config.local.json",
) -> dict:
    cfg = full_cfg.get("odoo_push") or {}
    raw_path = str(cfg.get("pool_path") or "").strip()
    if raw_path:
        pool_path = Path(raw_path).expanduser()
        if not pool_path.is_absolute():
            config_file = resolve_project_path(config_path, "config.local.json")
            pool_path = config_file.parent / pool_path
        pool_path = pool_path.resolve()
    else:
        pool_path = DEFAULT_POOL_PATH
    return {
        "enabled": cfg.get("incremental_pool_enabled", True) is not False,
        "pool_path": pool_path,
        "batch_limit": _positive_int(
            cfg.get("batch_limit"),
            default=500,
            minimum=1,
            maximum=10000,
            label="odoo_push.batch_limit",
        ),
        "lease_seconds": _positive_int(
            cfg.get("lease_seconds"),
            default=21600,
            minimum=60,
            maximum=86400,
            label="odoo_push.lease_seconds",
        ),
        "not_found_retry_seconds": _positive_int(
            cfg.get("not_found_retry_hours"),
            default=24,
            minimum=1,
            maximum=720,
            label="odoo_push.not_found_retry_hours",
        )
        * 3600,
        "review_retry_seconds": _positive_int(
            cfg.get("review_retry_hours"),
            default=168,
            minimum=1,
            maximum=8760,
            label="odoo_push.review_retry_hours",
        )
        * 3600,
        "completed_audit_seconds": _positive_int(
            cfg.get("completed_audit_hours"),
            default=168,
            minimum=1,
            maximum=8760,
            label="odoo_push.completed_audit_hours",
        )
        * 3600,
        "retry_base_seconds": _positive_int(
            cfg.get("retry_base_seconds"),
            default=900,
            minimum=60,
            maximum=86400,
            label="odoo_push.retry_base_seconds",
        ),
        "retry_max_seconds": _positive_int(
            cfg.get("retry_max_seconds"),
            default=86400,
            minimum=60,
            maximum=604800,
            label="odoo_push.retry_max_seconds",
        ),
    }


def build_candidate_key(provider: str, key_type: str, key_value: str) -> str:
    value = "\x1f".join((provider.strip(), key_type.strip(), key_value.strip()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_candidate_fingerprint(
    *,
    provider: str,
    key_type: str,
    key_value: str,
    item: dict,
    target_url: str,
    scope_identity: str = "",
) -> str:
    canonical = {
        "provider": provider,
        "key_type": key_type,
        "key_value": key_value,
        "document_uid": str(
            item.get("document_uid")
            or item.get("primary_document_uid")
            or item.get("source_file_id")
            or item.get("payload_item_id")
            or ""
        ),
        "source_modified_at": str(item.get("source_modified_at") or ""),
        "scope_identity_sha256": hashlib.sha256(
            str(scope_identity or "").encode("utf-8")
        ).hexdigest(),
        "target_url_sha256": hashlib.sha256(
            str(target_url or "").encode("utf-8")
        ).hexdigest(),
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_item(item: dict) -> dict:
    return {key: item[key] for key in _PERSISTED_ITEM_FIELDS if key in item}


class OdooPushPool:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_POOL_PATH)
        self.init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path), timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
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

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS odoo_push_pool (
                    candidate_key TEXT PRIMARY KEY,
                    provider_identity TEXT NOT NULL,
                    key_type TEXT NOT NULL,
                    key_value TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    item_json TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN (
                            'pending', 'leased', 'completed',
                            'retry', 'review', 'failed'
                        )),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_error_code TEXT,
                    lease_owner TEXT,
                    lease_until TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_odoo_push_pool_generation_state
                    ON odoo_push_pool(generation, state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_odoo_push_pool_lease
                    ON odoo_push_pool(lease_until);
                CREATE TABLE IF NOT EXISTS odoo_push_pool_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def refresh_candidates(
        self,
        candidates: Iterable[dict],
        *,
        generation: str,
        now: datetime | str | None = None,
    ) -> dict:
        now_iso = _utc_iso(now)
        prepared = []
        for candidate in candidates:
            state = str(candidate.get("initial_state") or "pending")
            if state not in POOL_STATES - {"leased"}:
                raise ValueError(f"invalid initial Odoo push pool state: {state}")
            prepared.append(
                {
                    "candidate_key": str(candidate["candidate_key"]),
                    "provider_identity": str(candidate["provider_identity"]),
                    "key_type": str(candidate["key_type"]),
                    "key_value": str(candidate["key_value"]),
                    "fingerprint": str(candidate["fingerprint"]),
                    "item_json": json.dumps(
                        _safe_item(candidate["item"]),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "initial_state": state,
                }
            )

        with self._transaction() as connection:
            existing = {
                row["candidate_key"]: row["fingerprint"]
                for row in connection.execute(
                    "SELECT candidate_key, fingerprint FROM odoo_push_pool"
                )
            }
            inserted = sum(
                1 for row in prepared if row["candidate_key"] not in existing
            )
            changed = sum(
                1
                for row in prepared
                if row["candidate_key"] in existing
                and existing[row["candidate_key"]] != row["fingerprint"]
            )
            connection.executemany(
                """
                INSERT INTO odoo_push_pool(
                    candidate_key, provider_identity, key_type, key_value,
                    fingerprint, item_json, generation, state,
                    first_seen_at, last_seen_at, next_attempt_at
                ) VALUES(
                    :candidate_key, :provider_identity, :key_type, :key_value,
                    :fingerprint, :item_json, :generation, :initial_state,
                    :now, :now, :now
                )
                ON CONFLICT(candidate_key) DO UPDATE SET
                    provider_identity=excluded.provider_identity,
                    key_type=excluded.key_type,
                    key_value=excluded.key_value,
                    item_json=excluded.item_json,
                    generation=excluded.generation,
                    last_seen_at=excluded.last_seen_at,
                    fingerprint=excluded.fingerprint,
                    state=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN excluded.state ELSE odoo_push_pool.state END,
                    next_attempt_at=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN excluded.next_attempt_at
                        ELSE odoo_push_pool.next_attempt_at END,
                    consecutive_failure_count=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN 0 ELSE odoo_push_pool.consecutive_failure_count END,
                    last_status=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN NULL ELSE odoo_push_pool.last_status END,
                    last_error_code=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN NULL ELSE odoo_push_pool.last_error_code END,
                    lease_owner=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN NULL ELSE odoo_push_pool.lease_owner END,
                    lease_until=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN NULL ELSE odoo_push_pool.lease_until END,
                    completed_at=CASE
                        WHEN odoo_push_pool.fingerprint != excluded.fingerprint
                        THEN NULL ELSE odoo_push_pool.completed_at END
                """,
                [dict(row, generation=generation, now=now_iso) for row in prepared],
            )
            connection.execute(
                """
                INSERT INTO odoo_push_pool_meta(key, value)
                VALUES('current_generation', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (generation,),
            )
            connection.execute(
                """
                INSERT INTO odoo_push_pool_meta(key, value)
                VALUES('last_refresh_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now_iso,),
            )
        return {
            "candidate_count": len(prepared),
            "inserted_count": inserted,
            "changed_count": changed,
            "unchanged_count": len(prepared) - inserted - changed,
        }

    def recover_expired_leases(
        self, *, now: datetime | str | None = None
    ) -> int:
        now_iso = _utc_iso(now)
        with self._transaction() as connection:
            return connection.execute(
                """
                UPDATE odoo_push_pool
                SET state='pending', next_attempt_at=?, lease_owner=NULL,
                    lease_until=NULL, last_status='lease_expired'
                WHERE state='leased' AND lease_until <= ?
                """,
                (now_iso, now_iso),
            ).rowcount

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["item"] = json.loads(result.pop("item_json"))
        return result

    def due_candidates(
        self,
        *,
        generation: str,
        now: datetime | str | None = None,
    ) -> list[dict]:
        now_iso = _utc_iso(now)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM odoo_push_pool
                WHERE generation=? AND state != 'leased'
                  AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, candidate_key ASC
                """,
                (generation, now_iso),
            ).fetchall()
            return [self._row(row) for row in rows]
        finally:
            connection.close()

    def set_evaluation(
        self,
        candidate_key: str,
        *,
        state: str,
        status: str,
        delay_seconds: int,
        now: datetime | str | None = None,
    ) -> None:
        self.set_evaluations(
            [
                {
                    "candidate_key": candidate_key,
                    "state": state,
                    "status": status,
                    "delay_seconds": delay_seconds,
                }
            ],
            now=now,
        )

    def set_evaluations(
        self,
        evaluations: Iterable[dict],
        *,
        now: datetime | str | None = None,
    ) -> None:
        """Persist one cache-reconciliation generation atomically."""

        now_dt = _utc_datetime(now)
        prepared = []
        for evaluation in evaluations:
            state = str(evaluation["state"])
            status = str(evaluation["status"])
            delay_seconds = int(evaluation["delay_seconds"])
            if state not in {
                "pending",
                "completed",
                "retry",
                "review",
                "failed",
            }:
                raise ValueError(
                    f"invalid evaluated Odoo push pool state: {state}"
                )
            prepared.append(
                {
                    "candidate_key": str(evaluation["candidate_key"]),
                    "state": state,
                    "status": status,
                    "next_attempt_at": _utc_iso(
                        now_dt + timedelta(seconds=max(0, delay_seconds))
                    ),
                    "completed_at": (
                        _utc_iso(now_dt) if state == "completed" else None
                    ),
                }
            )
        if not prepared:
            return
        with self._transaction() as connection:
            connection.executemany(
                """
                UPDATE odoo_push_pool
                SET state=:state, last_status=:status, last_error_code=NULL,
                    next_attempt_at=:next_attempt_at,
                    lease_owner=NULL, lease_until=NULL,
                    consecutive_failure_count=CASE
                        WHEN :state='completed' THEN 0
                        ELSE consecutive_failure_count END,
                    completed_at=CASE
                        WHEN :state='completed' THEN :completed_at
                        ELSE completed_at END
                WHERE candidate_key=:candidate_key
                """,
                prepared,
            )

    def claim_pending(
        self,
        *,
        generation: str,
        limit: int,
        lease_seconds: int,
        now: datetime | str | None = None,
        lease_owner: str | None = None,
    ) -> tuple[str, list[dict]]:
        now_dt = _utc_datetime(now)
        owner = lease_owner or f"odoo-push-{uuid.uuid4().hex}"
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM odoo_push_pool
                WHERE generation=? AND state='pending'
                  AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, candidate_key ASC
                LIMIT ?
                """,
                (generation, _utc_iso(now_dt), int(limit)),
            ).fetchall()
            keys = [row["candidate_key"] for row in rows]
            if keys:
                connection.executemany(
                    """
                    UPDATE odoo_push_pool
                    SET state='leased', lease_owner=?, lease_until=?,
                        attempt_count=attempt_count+1, next_attempt_at=NULL
                    WHERE candidate_key=? AND state='pending'
                    """,
                    [
                        (
                            owner,
                            _utc_iso(now_dt + timedelta(seconds=lease_seconds)),
                            key,
                        )
                        for key in keys
                    ],
                )
            claimed = connection.execute(
                "SELECT * FROM odoo_push_pool WHERE lease_owner=? ORDER BY candidate_key",
                (owner,),
            ).fetchall()
        return owner, [self._row(row) for row in claimed]

    def finish_claim(
        self,
        candidate_key: str,
        *,
        lease_owner: str,
        status: str,
        retryable: bool,
        settings: dict,
        now: datetime | str | None = None,
    ) -> None:
        now_dt = _utc_datetime(now)
        if status in {"pushed_tracking", "created_tracking", "skipped_same"}:
            state = "completed"
            delay = settings["completed_audit_seconds"]
        elif status == "not_found" or (
            status == "missing_required" and retryable
        ):
            state = "retry"
            delay = settings["not_found_retry_seconds"]
        elif status in {
            "multiple_tracking_matches",
            "multiple_picking_matches",
            "multiple_tracking_records",
            "needs_review",
            "cross_source_conflict",
            "missing_required",
        }:
            state = "review"
            delay = settings["review_retry_seconds"]
        else:
            state = "failed"
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT consecutive_failure_count FROM odoo_push_pool WHERE candidate_key=?",
                    (candidate_key,),
                ).fetchone()
                failures = int(row[0] if row else 0) + 1
            finally:
                connection.close()
            delay = min(
                settings["retry_max_seconds"],
                settings["retry_base_seconds"] * (2 ** min(failures - 1, 16)),
            )
        next_attempt = _utc_iso(now_dt + timedelta(seconds=delay))
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE odoo_push_pool
                SET state=?, next_attempt_at=?, last_status=?,
                    last_error_code=?, lease_owner=NULL, lease_until=NULL,
                    consecutive_failure_count=CASE
                        WHEN ?='failed' THEN consecutive_failure_count+1 ELSE 0 END,
                    completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END
                WHERE candidate_key=? AND state='leased' AND lease_owner=?
                """,
                (
                    state,
                    next_attempt,
                    status,
                    None if state == "completed" else status,
                    state,
                    state,
                    _utc_iso(now_dt),
                    candidate_key,
                    lease_owner,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeError(
                    "Odoo push pool lease was lost before result persistence"
                )

    def summary(
        self,
        *,
        generation: str,
        now: datetime | str | None = None,
    ) -> dict:
        now_iso = _utc_iso(now)
        connection = self._connect()
        try:
            state_counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM odoo_push_pool WHERE generation=? GROUP BY state
                    """,
                    (generation,),
                )
            }
            status_counts = {
                str(row["last_status"] or "unclassified"): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT last_status, COUNT(*) AS count
                    FROM odoo_push_pool WHERE generation=?
                    GROUP BY last_status
                    """,
                    (generation,),
                )
            }
            due_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM odoo_push_pool
                    WHERE generation=? AND state != 'leased'
                      AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                    """,
                    (generation, now_iso),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        return {
            "pool_path": str(self.db_path),
            "generation": generation,
            "total": sum(state_counts.values()),
            "due_count": due_count,
            "states": state_counts,
            "statuses": status_counts,
        }
