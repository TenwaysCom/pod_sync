#!/usr/bin/env python3
"""Annual POD coverage report: POD files vs Odoo outgoing orders by year."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .actions import PROJECT_ROOT, resolve_storage_dir
from .odoo_missing_report import load_odoo_missing_snapshot
from .phase1a_runs import get_latest_phase1a_run, resolve_artifact_path
from .xlsx_utils import build_xlsx_workbook


def _scan_pod_files_by_year(storage_dir: Path):
    """Scan storage/pod/{source}/ and group files by source + year."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for source_dir in storage_dir.iterdir():
        if not source_dir.is_dir() or source_dir.name.startswith("_"):
            continue
        source_name = source_dir.name.upper()
        for sub in source_dir.iterdir():
            if not sub.is_dir():
                continue
            # Directory name like "2024 POD" or "2025-09-27"
            year = sub.name[:4]
            if not year.isdigit():
                continue
            file_count = sum(1 for f in sub.rglob("*") if f.is_file())
            counts[(source_name, year)] += file_count
    return dict(counts)


def _scan_parsed_documents_by_year() -> dict[tuple[str, str], int]:
    """Read latest parsed_documents.json and count by source + year."""
    run = get_latest_phase1a_run()
    if not run:
        return {}
    parsed_path = resolve_artifact_path(run["run_name"], "parsed_documents_json")
    if not parsed_path or not parsed_path.exists():
        return {}
    try:
        rows = list(json.loads(parsed_path.read_text(encoding="utf-8")) or [])
    except Exception:
        return {}

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        source = str(row.get("source_name") or "unknown").upper()
        dt = str(row.get("source_modified_at") or "")
        if len(dt) >= 4 and dt[:4].isdigit():
            counts[(source, dt[:4])] += 1
    return dict(counts)


def _count_odoo_orders_by_year() -> dict[tuple[str, str], int]:
    """Count Odoo outgoing orders by carrier + year."""
    snapshot = load_odoo_missing_snapshot()
    if not snapshot:
        return {}

    # carrier_id -> source mapping from reconcile config
    carrier_map = {24: "EMTC", 25: "CMC"}

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in snapshot.get("total_detail_rows") or []:
        cid = row.get("carrier_id")
        source = carrier_map.get(cid)
        if not source:
            continue
        dt = str(row.get("business_datetime") or "")
        if len(dt) >= 4 and dt[:4].isdigit():
            counts[(source, dt[:4])] += 1
    return dict(counts)


def build_annual_pod_report_data() -> dict:
    """Build annual POD vs Odoo report data."""
    pod_counts = _scan_parsed_documents_by_year()
    odoo_counts = _count_odoo_orders_by_year()

    # Build unified year list
    years = sorted(set(y for _, y in {**pod_counts, **odoo_counts}.keys()))
    sources = ["CMC", "EMTC"]

    rows = []
    for year in years:
        for source in sources:
            pod = pod_counts.get((source, year), 0)
            odoo = odoo_counts.get((source, year), 0)
            rate = (pod / odoo * 100) if odoo > 0 else 0.0
            rows.append({
                "year": year,
                "source": source,
                "pod_files": pod,
                "odoo_orders": odoo,
                "coverage_rate": round(rate, 2),
            })
    return {"years": years, "sources": sources, "rows": rows}


def build_annual_pod_workbook() -> dict | None:
    data = build_annual_pod_report_data()
    rows = data.get("rows", [])
    if not rows:
        return None

    sheet_rows = [
        ["年份", "来源", "POD 文件数", "Odoo 出库单数", "提供率 (%)"],
    ]
    for r in rows:
        sheet_rows.append([
            r["year"],
            r["source"],
            r["pod_files"],
            r["odoo_orders"],
            r["coverage_rate"],
        ])

    workbook_bytes = build_xlsx_workbook(
        [{"name": "POD 年度提供率", "rows": sheet_rows}],
        title="POD Annual Coverage Report",
    )
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "filename": f"pod_annual_coverage_{stamp}.xlsx",
        "content": workbook_bytes,
    }
