#!/usr/bin/env python3
"""Annual POD coverage report: matched POD identifiers vs Odoo outgoing orders by year.

This report computes coverage from the POD→Odoo match results (Phase 1A compare),
using the matched Odoo order's business_datetime as the authoritative year.

Coverage formula:
    提供率 = 已匹配到 Odoo 的 POD 标识数 / Odoo 出库单总数

"Matched" includes: matched (done), matched_cancelled, and matched_other_carrier.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .odoo_missing_report import load_odoo_missing_snapshot
from .odoo_pod_compare import load_pod_odoo_compare_snapshot
from .xlsx_utils import build_xlsx_workbook


def _extract_year(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return None


def _count_matched_identifiers_by_year() -> dict[tuple[str, str], int]:
    """Count matched POD identifiers by source + year from the compare snapshot."""
    snapshot = load_pod_odoo_compare_snapshot()
    if not snapshot:
        return {}

    counts: dict[tuple[str, str], int] = defaultdict(int)

    for key in ("matched_rows", "matched_cancelled_rows", "matched_other_carrier_rows"):
        for row in snapshot.get(key) or []:
            source = row.get("source_name") or "-"
            # Prefer the matched Odoo order's business_datetime
            dt = row.get("matched_odoo_primary_business_datetime")
            year = _extract_year(dt)
            if year:
                counts[(source, year)] += 1
                continue

            # Fallback: if the snapshot was generated before the field existed,
            # try to derive year from the preferred_key for EMTC date-prefixed files.
            if source.upper().startswith("EMTC"):
                file_path = row.get("source_file_path") or ""
                file_name = row.get("sample_file_names") or ""
                for text in (file_path, file_name):
                    # EMTC files like 2026-03-19_3SYZXG8293874.png
                    import re

                    m = re.search(r"\b(20\d{2})-\d{2}-\d{2}", text)
                    if m:
                        year = m.group(1)
                        break
            if year:
                counts[(source, year)] += 1

    return dict(counts)


def _count_odoo_orders_by_year() -> dict[tuple[str, str], int]:
    """Count Odoo outgoing orders by carrier + year."""
    snapshot = load_odoo_missing_snapshot()
    if not snapshot:
        return {}

    carrier_map = {24: "EMTC", 25: "CMC"}

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in snapshot.get("total_detail_rows") or []:
        cid = row.get("carrier_id")
        source = carrier_map.get(cid)
        if not source:
            continue
        year = _extract_year(row.get("business_datetime"))
        if year:
            counts[(source, year)] += 1
    return dict(counts)


def build_annual_pod_report_data() -> dict:
    """Build annual POD coverage report data from Odoo match results."""
    matched_counts = _count_matched_identifiers_by_year()
    odoo_counts = _count_odoo_orders_by_year()

    years = sorted(set(y for _, y in {**matched_counts, **odoo_counts}.keys()))
    sources = ["CMC", "EMTC"]

    rows = []
    for year in years:
        for source in sources:
            matched = matched_counts.get((source, year), 0)
            odoo = odoo_counts.get((source, year), 0)
            rate = (matched / odoo * 100) if odoo > 0 else 0.0
            rows.append(
                {
                    "year": year,
                    "source": source,
                    "matched_identifiers": matched,
                    "odoo_orders": odoo,
                    "coverage_rate": round(rate, 2),
                }
            )
    return {"years": years, "sources": sources, "rows": rows}


def build_annual_pod_workbook() -> dict | None:
    data = build_annual_pod_report_data()
    rows = data.get("rows", [])
    if not rows:
        return None

    sheet_rows = [
        ["年份", "来源", "已匹配 POD 标识数", "Odoo 出库单数", "提供率 (%)"],
    ]
    for r in rows:
        sheet_rows.append(
            [
                r["year"],
                r["source"],
                r["matched_identifiers"],
                r["odoo_orders"],
                r["coverage_rate"],
            ]
        )

    workbook_bytes = build_xlsx_workbook(
        [{"name": "POD 年度提供率", "rows": sheet_rows}],
        title="POD Annual Coverage Report",
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "filename": f"pod_annual_coverage_{stamp}.xlsx",
        "content": workbook_bytes,
    }
