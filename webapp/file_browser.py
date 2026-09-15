from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


SOURCE_LABELS = {
    "CMC": "CMC OneDrive",
    "EMTC": "EMTC Google Drive",
    "Westerman": "Westerman API",
    "_integrity": "完整性日志",
}

DIRECT_ODOO_PUSH_SOURCES = {"CMC", "EMTC"}
WESTERMAN_PUSH_BLOCK_REASON = "Westerman 文件必须走待拉取池流水线，不能单独推送"
INTERNAL_PUSH_BLOCK_REASON = "内部日志文件不能推送到 Odoo"
UNKNOWN_SOURCE_PUSH_BLOCK_REASON = "未知来源，无法单独推送到 Odoo"


def source_label(source_key: str) -> str:
    return SOURCE_LABELS.get(source_key, source_key or "未分类")


def odoo_push_eligibility(source_key: str) -> dict:
    if source_key in DIRECT_ODOO_PUSH_SOURCES:
        return {"odoo_push_allowed": True, "odoo_push_blocked_reason": ""}
    if source_key == "Westerman":
        return {
            "odoo_push_allowed": False,
            "odoo_push_blocked_reason": WESTERMAN_PUSH_BLOCK_REASON,
        }
    if source_key == "_integrity":
        return {
            "odoo_push_allowed": False,
            "odoo_push_blocked_reason": INTERNAL_PUSH_BLOCK_REASON,
        }
    return {
        "odoo_push_allowed": False,
        "odoo_push_blocked_reason": UNKNOWN_SOURCE_PUSH_BLOCK_REASON,
    }


def format_file_size(size_bytes: int) -> str:
    value = float(size_bytes)
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def resolve_storage_file(storage_root: Path | str, relative_path: str) -> Path | None:
    root = Path(storage_root).resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents and candidate != root:
        return None
    if not candidate.is_file():
        return None
    return candidate


def parse_stored_date_bounds(date_from: str = "", date_to: str = "") -> tuple[float | None, float | None]:
    """Parse YYYY-MM-DD bounds as a local [start, end) timestamp range."""
    start = _parse_local_date(date_from)
    end_day = _parse_local_date(date_to)
    start_ts = None if start is None else start.timestamp()
    end_ts = None if end_day is None else (end_day + timedelta(days=1)).timestamp()
    return start_ts, end_ts


def _parse_local_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def build_storage_listing(
    storage_root: Path | str,
    *,
    source: str = "",
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    include_internal: bool = False,
    page: int = 1,
    page_size: int = 25,
):
    root = Path(storage_root).resolve()
    rows = []
    source_counts: Counter[str] = Counter()
    query_text = query.strip().lower()
    start_ts, end_ts = parse_stored_date_bounds(date_from, date_to)
    latest_mtime = None
    total_size = 0

    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue

            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue

            relative_path = relative.as_posix()
            source_key = relative_path.split("/", 1)[0] if relative_path else ""
            if not include_internal and source_key == "_integrity":
                continue
            if query_text and query_text not in relative_path.lower():
                continue

            stat = path.stat()
            stored_ts = stat.st_mtime
            if start_ts is not None and stored_ts < start_ts:
                continue
            if end_ts is not None and stored_ts >= end_ts:
                continue

            source_counts[source_key] += 1
            if source and source_key != source:
                continue

            stored_at = datetime.fromtimestamp(stored_ts).astimezone()
            stored_at_label = stored_at.strftime("%Y-%m-%d %H:%M:%S")
            rows.append(
                {
                    "file_name": path.name,
                    "relative_path": relative_path,
                    "source_key": source_key,
                    "source_label": source_label(source_key),
                    "size_bytes": stat.st_size,
                    "size_label": format_file_size(stat.st_size),
                    "stored_at": stored_at_label,
                    "stored_ts": stored_ts,
                    "modified_at": stored_at_label,
                    "modified_ts": stored_ts,
                    "extension": path.suffix.lower(),
                    **odoo_push_eligibility(source_key),
                }
            )
            total_size += stat.st_size
            if latest_mtime is None or stored_ts > latest_mtime:
                latest_mtime = stored_ts

    rows.sort(key=lambda row: (row["stored_ts"], row["relative_path"]), reverse=True)
    filtered_total = len(rows)
    page_size = max(1, min(int(page_size or 25), 200))
    page = max(1, int(page or 1))
    total_pages = max(1, (filtered_total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    page_start = (page - 1) * page_size
    page_end = page_start + page_size
    paged_rows = rows[page_start:page_end]

    source_options = [
        {
            "key": key,
            "label": source_label(key),
            "count": count,
        }
        for key, count in sorted(source_counts.items(), key=lambda item: (item[0] == "_integrity", item[0]))
    ]

    latest_modified_at = None
    if latest_mtime is not None:
        latest_modified_at = datetime.fromtimestamp(latest_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "storage_root": str(root),
        "exists": root.exists(),
        "total_files": filtered_total,
        "total_size_bytes": total_size,
        "total_size_label": format_file_size(total_size),
        "latest_stored_at": latest_modified_at,
        "latest_modified_at": latest_modified_at,
        "default_sort": "stored_at_desc",
        "date_from": date_from.strip() if start_ts is not None else "",
        "date_to": date_to.strip() if end_ts is not None else "",
        "source_count": len(source_options),
        "source_options": source_options,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "page_from": page_start + 1 if filtered_total else 0,
        "page_to": min(page_end, filtered_total),
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "items": paged_rows,
    }
