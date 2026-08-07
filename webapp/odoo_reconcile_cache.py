from __future__ import annotations

import fcntl
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from xmlrpc.client import ProtocolError

from clients.westerman_api.candidates import connect_models
from webapp.actions import resolve_project_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = PROJECT_ROOT / "runtime" / "odoo_reconcile" / "odoo_reconcile.sqlite3"
READ_BACKENDS = frozenset({"querydeluxe", "shadow", "xmlrpc"})
TRANSIENT_HTTP_CODES = frozenset({502, 503, 504})
SCHEMA_VERSION = 2
DEFAULT_SOURCE_DELIVERY_TYPES = {
    "emtc": "spare",
    "cmc": "cmc",
    "westerman": "westman",
}

PICKING_FIELDS = [
    "id",
    "name",
    "carrier_id",
    "picking_type_id",
    "state",
    "carrier_tracking_ref",
    "x_studio_pod_link",
    "date_done",
    "scheduled_date",
    "create_date",
    "write_date",
]
TRACKING_FIELDS = [
    "id",
    "tracking_number",
    "pod_link",
    "pod_time",
    "pod_state",
    "api_state",
    "picking_id",
    "write_date",
]
TRACKING_PICKING_FIELDS = ["id", "name", "carrier_tracking_ref", "carrier_id", "write_date"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _odoo_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value: Any) -> int | None:
    if value in (None, False, ""):
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value in (None, False):
        return ""
    return str(value).strip()


def load_config(config_path: str = "config.local.json") -> dict:
    path = resolve_project_path(config_path, "config.local.json")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_read_backend(full_cfg: dict) -> str:
    """Resolve the single authoritative read-mode switch.

    Missing configuration deliberately keeps the historic QueryDeluxe behavior.
    An explicitly present but invalid value fails closed before any network call.
    """
    odoo_cfg = full_cfg.get("odoo") or {}
    if "read_backend" not in odoo_cfg:
        return "querydeluxe"
    backend = odoo_cfg.get("read_backend")
    if not isinstance(backend, str) or backend.strip().lower() not in READ_BACKENDS:
        allowed = ", ".join(sorted(READ_BACKENDS))
        raise ValueError(f"odoo.read_backend must be one of: {allowed}")
    return backend.strip().lower()


def resolve_source_delivery_types(full_cfg: dict) -> dict[str, str]:
    configured = (full_cfg.get("odoo_reconcile") or {}).get("source_delivery_types")
    result = dict(DEFAULT_SOURCE_DELIVERY_TYPES)
    if configured is not None:
        if not isinstance(configured, dict):
            raise ValueError("odoo_reconcile.source_delivery_types must be an object")
        for source_slug in result:
            if source_slug in configured:
                value = _text(configured[source_slug])
                if not value:
                    raise ValueError(
                        f"odoo_reconcile.source_delivery_types.{source_slug} must not be empty"
                    )
                result[source_slug] = value

    # Keep the provider's existing stable selector as the default source of
    # truth when no explicit reconcile override is supplied.
    westerman_type = _text((full_cfg.get("westerman_api") or {}).get("carrier_delivery_type"))
    if westerman_type and not (configured and "westerman" in configured):
        result["westerman"] = westerman_type
    if len(set(result.values())) != len(result):
        raise ValueError("Odoo reconcile source delivery types must be unique")
    return result


def resolve_cache_path(
    full_cfg: dict | None = None,
    *,
    config_path: str = "config.local.json",
    db_path: str | Path | None = None,
) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    cfg = full_cfg or {}
    raw_path = _text((cfg.get("odoo_reconcile") or {}).get("cache_path"))
    if not raw_path:
        return DEFAULT_CACHE_PATH
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    config_file = resolve_project_path(config_path, "config.local.json")
    return (config_file.parent / path).resolve()


def _positive_int(value: Any, *, default: int, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(default if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _settings(full_cfg: dict) -> dict:
    cfg = full_cfg.get("odoo_reconcile") or {}
    return {
        "page_size": _positive_int(
            cfg.get("page_size"), default=2000, minimum=100, maximum=5000,
            label="odoo_reconcile.page_size",
        ),
        "overlap_seconds": _positive_int(
            cfg.get("incremental_overlap_seconds"), default=300, minimum=0, maximum=3600,
            label="odoo_reconcile.incremental_overlap_seconds",
        ),
        "full_refresh_hours": _positive_int(
            cfg.get("full_refresh_interval_hours"), default=168, minimum=1, maximum=8760,
            label="odoo_reconcile.full_refresh_interval_hours",
        ),
    }


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    existing_meta = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if existing_meta:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        existing_version = str(row[0]) if row else ""
        if existing_version != str(SCHEMA_VERSION):
            # This cache is fully derived from Odoo. Rebuilding is safer than
            # retaining v1 rows which omitted incomplete tracking state and
            # therefore cannot prove POD write idempotency.
            connection.executescript(
                """
                DROP TABLE IF EXISTS pickings;
                DROP TABLE IF EXISTS tracking;
                DROP TABLE IF EXISTS tracking_pickings;
                DROP TABLE IF EXISTS meta;
                """
            )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pickings (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            carrier_id INTEGER NOT NULL,
            carrier_name TEXT,
            carrier_delivery_type TEXT NOT NULL,
            source_slug TEXT NOT NULL,
            state TEXT,
            business_datetime TEXT,
            tracking_number TEXT,
            legacy_pod_link TEXT,
            write_date TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pickings_source_date
            ON pickings(source_slug, business_datetime, state);
        CREATE INDEX IF NOT EXISTS idx_pickings_name ON pickings(name);
        CREATE INDEX IF NOT EXISTS idx_pickings_tracking ON pickings(tracking_number);

        CREATE TABLE IF NOT EXISTS tracking (
            id INTEGER PRIMARY KEY,
            tracking_number TEXT,
            picking_id INTEGER,
            pod_link TEXT,
            pod_time TEXT,
            pod_state TEXT,
            api_state TEXT,
            write_date TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tracking_picking ON tracking(picking_id);
        CREATE INDEX IF NOT EXISTS idx_tracking_number ON tracking(tracking_number);

        CREATE TABLE IF NOT EXISTS tracking_pickings (
            id INTEGER PRIMARY KEY,
            name TEXT,
            carrier_tracking_ref TEXT,
            carrier_id INTEGER,
            write_date TEXT
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    # Generation numbers are only monotonic within one physical cache
    # incarnation.  Schema rebuilds and replacement database files restart the
    # counter, so persist an ordered cache identity alongside the generation.
    # The timestamp prefix lets concurrent writers reject a late result from an
    # older cache incarnation without treating generation 40 as newer than a
    # freshly rebuilt generation 1.
    cache_epoch = f"{_utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('cache_epoch', ?)",
        (cache_epoch,),
    )
    connection.commit()


def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}


def _set_meta(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    connection.executemany(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(key, str(value)) for key, value in values.items()],
    )


def _execute_read_only(models, odoo_cfg: dict, model: str, method: str, args: list, kwargs: dict):
    if method not in {"search_read", "read"}:
        raise AssertionError(f"Refusing non-read-only Odoo method: {method}")
    for attempt in range(1, 4):
        try:
            return models.execute_kw(
                odoo_cfg["db"], odoo_cfg["uid"], odoo_cfg["api_key"],
                model, method, args, kwargs,
            )
        except ProtocolError as exc:
            if exc.errcode not in TRANSIENT_HTTP_CODES or attempt >= 3:
                raise
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _search_read_pages(
    models,
    odoo_cfg: dict,
    *,
    model: str,
    domain: list,
    fields: list[str],
    page_size: int,
    context: dict | None = None,
) -> list[dict]:
    rows: list[dict] = []
    last_id = 0
    while True:
        page_domain = list(domain) + [("id", ">", last_id)]
        kwargs: dict[str, Any] = {
            "fields": fields,
            "limit": page_size,
            "order": "id ASC",
        }
        if context:
            kwargs["context"] = dict(context)
        page = _execute_read_only(models, odoo_cfg, model, "search_read", [page_domain], kwargs)
        if not isinstance(page, list):
            raise ValueError(f"Odoo {model}.search_read returned an unexpected response")
        if not page:
            break
        page_ids = [_as_int(row.get("id")) for row in page]
        if any(record_id is None for record_id in page_ids):
            raise ValueError(f"Odoo {model}.search_read returned a row without id")
        new_last_id = max(int(record_id) for record_id in page_ids if record_id is not None)
        if new_last_id <= last_id:
            raise ValueError(f"Odoo {model}.search_read cursor did not advance")
        rows.extend(row for row in page if isinstance(row, dict))
        last_id = new_last_id
        if len(page) < page_size:
            break
    return rows


def _read_ids(
    models,
    odoo_cfg: dict,
    *,
    model: str,
    ids: Iterable[int],
    fields: list[str],
    batch_size: int,
) -> list[dict]:
    ordered = sorted({int(value) for value in ids if value})
    rows: list[dict] = []
    for start in range(0, len(ordered), batch_size):
        batch = ordered[start : start + batch_size]
        page = _execute_read_only(
            models, odoo_cfg, model, "read", [batch], {"fields": fields}
        )
        if not isinstance(page, list):
            raise ValueError(f"Odoo {model}.read returned an unexpected response")
        rows.extend(row for row in page if isinstance(row, dict))
    return rows


def _business_datetime(row: dict) -> str:
    return _text(row.get("date_done") or row.get("scheduled_date") or row.get("create_date"))


def _canonical_picking(
    row: dict,
    *,
    carrier_by_id: dict[int, dict],
    source_by_delivery_type: dict[str, str],
) -> dict | None:
    record_id = _as_int(row.get("id"))
    carrier_id = _as_int(row.get("carrier_id"))
    carrier = carrier_by_id.get(carrier_id or 0)
    if not record_id or not carrier:
        return None
    delivery_type = _text(carrier.get("delivery_type"))
    source_slug = source_by_delivery_type.get(delivery_type)
    if not source_slug:
        return None
    return {
        "id": record_id,
        "name": _text(row.get("name")),
        "carrier_id": carrier_id,
        "carrier_name": _text(carrier.get("name")),
        "carrier_delivery_type": delivery_type,
        "source_slug": source_slug,
        "state": _text(row.get("state")),
        "business_datetime": _business_datetime(row),
        "tracking_number": _text(row.get("carrier_tracking_ref")),
        "legacy_pod_link": _text(row.get("x_studio_pod_link")),
        "write_date": _text(row.get("write_date")),
    }


def _canonical_tracking(row: dict) -> dict | None:
    record_id = _as_int(row.get("id"))
    picking_id = _as_int(row.get("picking_id"))
    if not record_id or not picking_id:
        return None
    return {
        "id": record_id,
        "tracking_number": _text(row.get("tracking_number")),
        "picking_id": picking_id,
        "pod_link": _text(row.get("pod_link")),
        "pod_time": _text(row.get("pod_time")),
        "pod_state": _text(row.get("pod_state")),
        "api_state": _text(row.get("api_state")),
        "write_date": _text(row.get("write_date")),
    }


def _canonical_tracking_picking(row: dict) -> dict | None:
    record_id = _as_int(row.get("id"))
    if not record_id:
        return None
    return {
        "id": record_id,
        "name": _text(row.get("name")),
        "carrier_tracking_ref": _text(row.get("carrier_tracking_ref")),
        "carrier_id": _as_int(row.get("carrier_id")),
        "write_date": _text(row.get("write_date")),
    }


def _insert_pickings(connection: sqlite3.Connection, rows: Iterable[dict]) -> None:
    connection.executemany(
        """
        INSERT INTO pickings(
            id, name, carrier_id, carrier_name, carrier_delivery_type, source_slug,
            state, business_datetime, tracking_number, legacy_pod_link, write_date
        ) VALUES(
            :id, :name, :carrier_id, :carrier_name, :carrier_delivery_type, :source_slug,
            :state, :business_datetime, :tracking_number, :legacy_pod_link, :write_date
        )
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, carrier_id=excluded.carrier_id,
            carrier_name=excluded.carrier_name,
            carrier_delivery_type=excluded.carrier_delivery_type,
            source_slug=excluded.source_slug, state=excluded.state,
            business_datetime=excluded.business_datetime,
            tracking_number=excluded.tracking_number,
            legacy_pod_link=excluded.legacy_pod_link, write_date=excluded.write_date
        """,
        list(rows),
    )


def _insert_tracking(connection: sqlite3.Connection, rows: Iterable[dict]) -> None:
    connection.executemany(
        """
        INSERT INTO tracking(
            id, tracking_number, picking_id, pod_link, pod_time,
            pod_state, api_state, write_date
        )
        VALUES(
            :id, :tracking_number, :picking_id, :pod_link, :pod_time,
            :pod_state, :api_state, :write_date
        )
        ON CONFLICT(id) DO UPDATE SET
            tracking_number=excluded.tracking_number, picking_id=excluded.picking_id,
            pod_link=excluded.pod_link, pod_time=excluded.pod_time,
            pod_state=excluded.pod_state, api_state=excluded.api_state,
            write_date=excluded.write_date
        """,
        list(rows),
    )


def _insert_tracking_pickings(connection: sqlite3.Connection, rows: Iterable[dict]) -> None:
    connection.executemany(
        """
        INSERT INTO tracking_pickings(id, name, carrier_tracking_ref, carrier_id, write_date)
        VALUES(:id, :name, :carrier_tracking_ref, :carrier_id, :write_date)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, carrier_tracking_ref=excluded.carrier_tracking_ref,
            carrier_id=excluded.carrier_id, write_date=excluded.write_date
        """,
        list(rows),
    )


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("pickings", "tracking", "tracking_pickings")
    }


def refresh_reconcile_cache(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    models=None,
    force_full: bool = False,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Refresh the ordinary XML-RPC cache without mutating Odoo.

    Network reads finish before the SQLite write transaction starts. A failed
    read therefore leaves the last successful generation fully usable.
    """
    full_cfg = full_cfg or load_config(config_path)
    settings = _settings(full_cfg)
    source_types = resolve_source_delivery_types(full_cfg)
    source_by_delivery_type = {value: key for key, value in source_types.items()}
    cache_path = resolve_cache_path(full_cfg, config_path=config_path, db_path=db_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    requested_now = now

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        connection = _connect(cache_path)
        try:
            _ensure_schema(connection)
            current_meta = _meta(connection)
            high_water = (requested_now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
            last_success = _parse_datetime(current_meta.get("last_success_at"))
            if last_success and high_water < last_success:
                high_water = last_success
            current_signature = json.dumps(source_types, sort_keys=True, separators=(",", ":"))
            last_full = _parse_datetime(current_meta.get("last_full_at"))
            full_due = not last_full or high_water - last_full >= timedelta(
                hours=settings["full_refresh_hours"]
            )
            refresh_kind = "full" if (
                force_full
                or not last_success
                or full_due
                or current_meta.get("source_signature") != current_signature
                or current_meta.get("schema_version") != str(SCHEMA_VERSION)
            ) else "incremental"

            odoo_cfg = full_cfg.get("odoo") or {}
            models = models or connect_models(odoo_cfg)
            page_size = settings["page_size"]

            carrier_rows = _search_read_pages(
                models, odoo_cfg,
                model="delivery.carrier",
                domain=[("delivery_type", "in", list(source_types.values()))],
                fields=["id", "name", "delivery_type", "active"],
                page_size=page_size,
                context={"active_test": False},
            )
            carrier_by_id = {
                int(record_id): row
                for row in carrier_rows
                if (record_id := _as_int(row.get("id"))) is not None
            }
            visible_types = {_text(row.get("delivery_type")) for row in carrier_rows}
            missing_types = sorted(set(source_types.values()) - visible_types)
            if missing_types:
                raise ValueError(
                    "No visible delivery.carrier for delivery_type: " + ", ".join(missing_types)
                )

            picking_type_rows = _search_read_pages(
                models, odoo_cfg,
                model="stock.picking.type",
                domain=[("code", "=", "outgoing")],
                fields=["id", "code"],
                page_size=page_size,
            )
            outgoing_type_ids = {
                int(record_id)
                for row in picking_type_rows
                if (record_id := _as_int(row.get("id"))) is not None
            }
            if not outgoing_type_ids:
                raise ValueError("No visible outgoing stock.picking.type records")

            scope_signature = json.dumps(
                {
                    "carriers": sorted(
                        (
                            carrier_id,
                            _text(row.get("delivery_type")),
                            _text(row.get("name")),
                        )
                        for carrier_id, row in carrier_by_id.items()
                    ),
                    "outgoing_type_ids": sorted(outgoing_type_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            previous_carrier_ids = {
                int(value)
                for value in json.loads(current_meta.get("target_carrier_ids") or "[]")
            }
            if (
                (previous_carrier_ids and previous_carrier_ids != set(carrier_by_id))
                or (
                    current_meta.get("scope_signature")
                    and current_meta.get("scope_signature") != scope_signature
                )
            ):
                refresh_kind = "full"

            if refresh_kind == "full":
                picking_domain = [
                    ("picking_type_id.code", "=", "outgoing"),
                    ("carrier_id.delivery_type", "in", list(source_types.values())),
                ]
                tracking_domain = [
                    ("picking_id", "!=", False),
                    (
                        "picking_id.carrier_id.delivery_type",
                        "in",
                        list(source_types.values()),
                    ),
                    ("picking_id.picking_type_id.code", "=", "outgoing"),
                ]
            else:
                lower = last_success - timedelta(seconds=settings["overlap_seconds"])
                window = [
                    ("write_date", ">=", _odoo_datetime(lower)),
                    ("write_date", "<=", _odoo_datetime(high_water)),
                ]
                # Read all changed pickings so a row moved out of target scope
                # can be deleted from the local target-only cache.
                picking_domain = window
                # Read empty pod_link updates too, so removals delete the cache row.
                tracking_domain = window

            raw_pickings = _search_read_pages(
                models, odoo_cfg, model="stock.picking", domain=picking_domain,
                fields=PICKING_FIELDS, page_size=page_size,
            )
            canonical_pickings = []
            for row in raw_pickings:
                carrier_id = _as_int(row.get("carrier_id"))
                picking_type_id = _as_int(row.get("picking_type_id"))
                if carrier_id not in carrier_by_id or picking_type_id not in outgoing_type_ids:
                    continue
                canonical = _canonical_picking(
                    row,
                    carrier_by_id=carrier_by_id,
                    source_by_delivery_type=source_by_delivery_type,
                )
                if canonical:
                    canonical_pickings.append(canonical)

            raw_tracking = _search_read_pages(
                models, odoo_cfg, model="stock.tracking", domain=tracking_domain,
                fields=TRACKING_FIELDS, page_size=page_size,
            )
            cached_target_picking_ids = {
                int(row[0]) for row in connection.execute("SELECT id FROM pickings")
            }
            changed_picking_ids = {
                int(record_id)
                for row in raw_pickings
                if (record_id := _as_int(row.get("id"))) is not None
            }
            current_target_picking_ids = (
                cached_target_picking_ids - changed_picking_ids
            ) | {int(row["id"]) for row in canonical_pickings}
            if refresh_kind == "full":
                current_target_picking_ids = {
                    int(row["id"]) for row in canonical_pickings
                }
            canonical_tracking = [
                item
                for row in raw_tracking
                if (item := _canonical_tracking(row)) is not None
                and int(item["picking_id"]) in current_target_picking_ids
            ]

            new_tracking_picking_ids = {row["picking_id"] for row in canonical_tracking}
            if refresh_kind == "full":
                tracking_picking_rows = _read_ids(
                    models, odoo_cfg, model="stock.picking", ids=new_tracking_picking_ids,
                    fields=TRACKING_PICKING_FIELDS, batch_size=page_size,
                )
            else:
                cached_tracking_picking_ids = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT picking_id FROM tracking WHERE picking_id IS NOT NULL"
                    )
                }
                existing_hydrated_ids = {
                    int(row[0]) for row in connection.execute("SELECT id FROM tracking_pickings")
                }
                raw_pickings_by_id = {
                    int(record_id): row
                    for row in raw_pickings
                    if (record_id := _as_int(row.get("id"))) is not None
                }
                referenced_changed_ids = set(raw_pickings_by_id) & (
                    cached_tracking_picking_ids | new_tracking_picking_ids
                )
                tracking_picking_rows = [
                    raw_pickings_by_id[record_id]
                    for record_id in sorted(referenced_changed_ids)
                ]
                missing_new_ids = (
                    new_tracking_picking_ids
                    - existing_hydrated_ids
                    - set(raw_pickings_by_id)
                )
                tracking_picking_rows.extend(
                    _read_ids(
                        models, odoo_cfg, model="stock.picking", ids=missing_new_ids,
                        fields=TRACKING_PICKING_FIELDS, batch_size=page_size,
                    )
                )
            canonical_tracking_pickings = [
                item
                for row in tracking_picking_rows
                if (item := _canonical_tracking_picking(row)) is not None
                and int(item["id"]) in current_target_picking_ids
            ]

            connection.execute("BEGIN IMMEDIATE")
            if refresh_kind == "full":
                connection.execute("DELETE FROM pickings")
                connection.execute("DELETE FROM tracking")
                connection.execute("DELETE FROM tracking_pickings")
            else:
                changed_picking_ids = [
                    int(record_id)
                    for row in raw_pickings
                    if (record_id := _as_int(row.get("id"))) is not None
                ]
                changed_tracking_ids = [
                    int(record_id)
                    for row in raw_tracking
                    if (record_id := _as_int(row.get("id"))) is not None
                ]
                connection.executemany("DELETE FROM pickings WHERE id=?", [(value,) for value in changed_picking_ids])
                connection.executemany("DELETE FROM tracking WHERE id=?", [(value,) for value in changed_tracking_ids])
                connection.executemany(
                    "DELETE FROM tracking_pickings WHERE id=?",
                    [(value,) for value in changed_picking_ids],
                )

            _insert_pickings(connection, canonical_pickings)
            _insert_tracking(connection, canonical_tracking)
            _insert_tracking_pickings(connection, canonical_tracking_pickings)
            generation = int(current_meta.get("generation") or 0) + 1
            meta_values = {
                "schema_version": SCHEMA_VERSION,
                "generation": generation,
                "last_success_at": _iso_datetime(high_water),
                "refreshed_at": _iso_datetime(_utc_now()),
                "refresh_kind": refresh_kind,
                "source_signature": current_signature,
                "target_carrier_ids": json.dumps(sorted(carrier_by_id)),
                "scope_signature": scope_signature,
            }
            if refresh_kind == "full":
                meta_values["last_full_at"] = _iso_datetime(high_water)
            _set_meta(connection, meta_values)
            connection.commit()
            counts = _row_counts(connection)
            return {
                "status": "ok",
                "read_only": True,
                "backend": "xmlrpc",
                "refresh_kind": refresh_kind,
                "generation": generation,
                "cache_epoch": current_meta.get("cache_epoch") or "",
                "cache_path": str(cache_path),
                "source_delivery_types": source_types,
                "target_carrier_ids": sorted(carrier_by_id),
                "row_counts": counts,
                "window_end": _iso_datetime(high_water),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def cache_status(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    db_path: str | Path | None = None,
) -> dict:
    full_cfg = full_cfg or (load_config(config_path) if db_path is None else {})
    path = resolve_cache_path(full_cfg, config_path=config_path, db_path=db_path)
    if not path.exists():
        return {"available": False, "generation": 0, "cache_path": str(path)}
    connection = _connect(path)
    try:
        _ensure_schema(connection)
        metadata = _meta(connection)
        return {
            "available": bool(int(metadata.get("generation") or 0)),
            "generation": int(metadata.get("generation") or 0),
            "cache_epoch": metadata.get("cache_epoch") or "",
            "cache_path": str(path),
            "last_success_at": metadata.get("last_success_at"),
            "last_full_at": metadata.get("last_full_at"),
            "refresh_kind": metadata.get("refresh_kind"),
            "target_carrier_ids": json.loads(metadata.get("target_carrier_ids") or "[]"),
            "row_counts": _row_counts(connection),
        }
    finally:
        connection.close()


def _picking_result_row(row: sqlite3.Row) -> dict:
    return {
        "carrier_id": row["carrier_id"],
        "carrier_name": row["carrier_name"],
        "carrier_delivery_type": row["carrier_delivery_type"],
        "source_slug": row["source_slug"],
        "odoo_picking_id": row["id"],
        "picking_name": row["name"],
        "state": row["state"],
        "business_datetime": row["business_datetime"],
        "tracking_number": row["tracking_number"] or None,
        "carrier_tracking_ref": row["tracking_number"] or None,
        "x_studio_pod_link": row["legacy_pod_link"] or None,
        "write_date": row["write_date"],
    }


def load_cached_picking_state(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    source_slug: str | None = None,
    db_path: str | Path | None = None,
) -> dict:
    full_cfg = full_cfg or (load_config(config_path) if db_path is None else {})
    path = resolve_cache_path(full_cfg, config_path=config_path, db_path=db_path)
    if not path.exists():
        return {
            "available": False,
            "generation": 0,
            "last_success_at": None,
            "target_carrier_ids": [],
            "rows": [],
        }
    connection = _connect(path)
    try:
        _ensure_schema(connection)
        connection.execute("BEGIN")
        metadata = _meta(connection)
        sql = "SELECT * FROM pickings"
        params: tuple[Any, ...] = ()
        if source_slug:
            sql += " WHERE source_slug=?"
            params = (source_slug,)
        sql += " ORDER BY id ASC"
        result = [_picking_result_row(row) for row in connection.execute(sql, params)]
        connection.commit()
        generation = int(metadata.get("generation") or 0)
        return {
            "available": bool(generation),
            "generation": generation,
            "cache_epoch": metadata.get("cache_epoch") or "",
            "last_success_at": metadata.get("last_success_at"),
            "target_carrier_ids": json.loads(metadata.get("target_carrier_ids") or "[]"),
            "rows": result,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_cached_picking_rows(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    source_slug: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    return load_cached_picking_state(
        config_path,
        full_cfg=full_cfg,
        source_slug=source_slug,
        db_path=db_path,
    )["rows"]


def load_cached_pod_state(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    db_path: str | Path | None = None,
) -> dict:
    full_cfg = full_cfg or (load_config(config_path) if db_path is None else {})
    path = resolve_cache_path(full_cfg, config_path=config_path, db_path=db_path)
    if not path.exists():
        return {"available": False, "generation": 0, "rows": [], "picking_ids": []}
    connection = _connect(path)
    try:
        _ensure_schema(connection)
        connection.execute("BEGIN")
        metadata = _meta(connection)
        rows = [dict(row) for row in connection.execute(
            """
            SELECT t.id, t.tracking_number, t.pod_link, t.picking_id, t.write_date,
                   p.name AS picking_name, p.carrier_tracking_ref
            FROM tracking AS t
            LEFT JOIN tracking_pickings AS p ON p.id=t.picking_id
            WHERE COALESCE(t.pod_link, '') != ''
            ORDER BY t.id ASC
            """
        )]
        picking_ids = [int(row[0]) for row in connection.execute(
            """
            SELECT DISTINCT picking_id FROM tracking
            WHERE picking_id IS NOT NULL AND COALESCE(pod_link, '') != ''
            ORDER BY picking_id
            """
        )]
        connection.commit()
        generation = int(metadata.get("generation") or 0)
        return {
            "available": bool(generation),
            "generation": generation,
            "cache_epoch": metadata.get("cache_epoch") or "",
            "last_success_at": metadata.get("last_success_at"),
            "rows": rows,
            "picking_ids": picking_ids,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_cached_tracking_rows(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    return load_cached_pod_state(
        config_path, full_cfg=full_cfg, db_path=db_path
    )["rows"]


def cached_stock_tracking_picking_ids(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    db_path: str | Path | None = None,
) -> list[int]:
    return load_cached_pod_state(
        config_path, full_cfg=full_cfg, db_path=db_path
    )["picking_ids"]


def load_cached_push_resolution_state(
    config_path: str = "config.local.json",
    *,
    full_cfg: dict | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Load the complete carrier-scoped state used by the push work pool.

    Unlike ``load_cached_pod_state``, this includes tracking rows without a POD.
    Those rows are required to distinguish a safe update from a create and to
    prove that one Odoo target is unique before skipping a historical item.
    """

    full_cfg = full_cfg or (load_config(config_path) if db_path is None else {})
    path = resolve_cache_path(full_cfg, config_path=config_path, db_path=db_path)
    if not path.exists():
        return {
            "available": False,
            "generation": 0,
            "last_success_at": None,
            "pickings": [],
            "tracking": [],
        }
    connection = _connect(path)
    try:
        _ensure_schema(connection)
        connection.execute("BEGIN")
        metadata = _meta(connection)
        pickings = [dict(row) for row in connection.execute(
            """
            SELECT id, name, tracking_number, source_slug,
                   carrier_delivery_type, write_date
            FROM pickings ORDER BY id ASC
            """
        )]
        tracking = [dict(row) for row in connection.execute(
            """
            SELECT t.id, t.tracking_number, t.picking_id, t.pod_link,
                   t.pod_time, t.pod_state, t.api_state, t.write_date,
                   p.name AS picking_name, p.source_slug,
                   p.carrier_delivery_type
            FROM tracking AS t
            JOIN pickings AS p ON p.id=t.picking_id
            ORDER BY t.id ASC
            """
        )]
        connection.commit()
        generation = int(metadata.get("generation") or 0)
        return {
            "available": bool(generation),
            "generation": generation,
            "cache_epoch": metadata.get("cache_epoch") or "",
            "last_success_at": metadata.get("last_success_at"),
            "pickings": pickings,
            "tracking": tracking,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
