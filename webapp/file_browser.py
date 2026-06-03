from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path


SOURCE_LABELS = {
    "CMC": "CMC OneDrive",
    "EMTC": "EMTC Google Drive",
    "Westerman": "Westerman API",
    "_integrity": "完整性日志",
}


def source_label(source_key: str) -> str:
    return SOURCE_LABELS.get(source_key, source_key or "未分类")


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


def build_storage_listing(
    storage_root: Path | str,
    *,
    source: str = "",
    query: str = "",
    include_internal: bool = False,
    page: int = 1,
    page_size: int = 25,
):
    root = Path(storage_root).resolve()
    rows = []
    source_counts: Counter[str] = Counter()
    query_text = query.strip().lower()
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

            source_counts[source_key] += 1
            if source and source_key != source:
                continue

            stat = path.stat()
            stored_at = datetime.fromtimestamp(stat.st_mtime).astimezone()
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
                    "stored_ts": stat.st_mtime,
                    "modified_at": stored_at_label,
                    "modified_ts": stat.st_mtime,
                    "extension": path.suffix.lower(),
                }
            )
            total_size += stat.st_size
            if latest_mtime is None or stat.st_mtime > latest_mtime:
                latest_mtime = stat.st_mtime

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
