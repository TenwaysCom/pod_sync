#!/usr/bin/env python3
"""Annual POD coverage report from Odoo perspective.

Coverage formula (Odoo angle):
    提供率 = Odoo 中已有 POD link 的出库单数 / Odoo 出库单总数

Also provides monthly breakdown by carrier.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .odoo_missing_report import load_odoo_missing_snapshot
from .xlsx_utils import build_xlsx_workbook


STOCK_TRACKING_POD_IDS_PATH = Path(__file__).resolve().parents[1] / "runtime" / "stock_tracking_pod_picking_ids.json"


def _load_stock_tracking_pod_picking_ids() -> set[int]:
    from .odoo_pull import load_coverage_picking_ids

    return load_coverage_picking_ids()


def _row_has_pod(row: dict, stock_tracking_ids: set[int]) -> bool:
    if row.get("x_studio_pod_link"):
        return True
    picking_id = row.get("odoo_picking_id")
    if picking_id is not None:
        try:
            if int(picking_id) in stock_tracking_ids:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _extract_year(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return None


def _count_odoo_orders_with_pod_by_year() -> dict[tuple[str, str], int]:
    """Count Odoo outgoing orders that already have a POD, by carrier + year.

    Includes both stock.picking.x_studio_pod_link and stock.tracking.pod_link.
    """
    snapshot = load_odoo_missing_snapshot()
    if not snapshot:
        return {}

    carrier_map = {24: "EMTC", 25: "CMC"}
    stock_tracking_ids = _load_stock_tracking_pod_picking_ids()

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in snapshot.get("total_detail_rows") or []:
        cid = row.get("carrier_id")
        source = carrier_map.get(cid)
        if not source:
            continue
        if not _row_has_pod(row, stock_tracking_ids):
            continue
        year = _extract_year(row.get("business_datetime"))
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
    """Build annual POD coverage report data from Odoo snapshot."""
    with_pod_counts = _count_odoo_orders_with_pod_by_year()
    odoo_counts = _count_odoo_orders_by_year()

    years = sorted(set(y for _, y in {**with_pod_counts, **odoo_counts}.keys()))
    sources = ["CMC", "EMTC"]

    rows = []
    for year in years:
        for source in sources:
            with_pod = with_pod_counts.get((source, year), 0)
            odoo = odoo_counts.get((source, year), 0)
            rate = (with_pod / odoo * 100) if odoo > 0 else 0.0
            rows.append(
                {
                    "year": year,
                    "source": source,
                    "with_pod": with_pod,
                    "odoo_orders": odoo,
                    "coverage_rate": round(rate, 2),
                }
            )
    return {"years": years, "sources": sources, "rows": rows}


def build_monthly_pod_report_data() -> dict:
    """Build monthly POD coverage report data from Odoo snapshot (CMC + EMTC only)."""
    snapshot = load_odoo_missing_snapshot()
    if not snapshot:
        return {"months": [], "carriers": [], "rows": []}

    # Only CMC (25) and EMTC (24)
    target_carriers = {24: "EMTC", 25: "CMC"}

    stock_tracking_ids = _load_stock_tracking_pod_picking_ids()
    grouped = defaultdict(lambda: {"total_orders": 0, "with_pod": 0})

    for row in snapshot.get("total_detail_rows") or []:
        cid = row.get("carrier_id")
        if cid not in target_carriers:
            continue
        dt = row.get("business_datetime")
        if not dt:
            continue
        month_key = str(dt)[:7] if len(str(dt)) >= 7 else None
        if not month_key:
            continue
        carrier = target_carriers[cid]
        key = (month_key, carrier)
        grouped[key]["total_orders"] += 1
        if _row_has_pod(row, stock_tracking_ids):
            grouped[key]["with_pod"] += 1

    months = sorted(set(m for m, _ in grouped.keys()), reverse=True)
    carriers = ["CMC", "EMTC"]

    rows = []
    for month_key in months:
        for carrier in carriers:
            counts = grouped.get((month_key, carrier), {"total_orders": 0, "with_pod": 0})
            total = counts["total_orders"]
            with_pod = counts["with_pod"]
            rate = (with_pod / total * 100) if total > 0 else 0.0
            rows.append(
                {
                    "month_key": month_key,
                    "carrier": carrier,
                    "total_orders": total,
                    "with_pod": with_pod,
                    "without_pod": total - with_pod,
                    "coverage_rate": round(rate, 2),
                }
            )
    return {"months": months, "carriers": carriers, "rows": rows}


def build_annual_pod_workbook() -> dict | None:
    data = build_annual_pod_report_data()
    rows = data.get("rows", [])
    if not rows:
        return None

    sheet_rows = [
        ["年份", "来源", "已有 POD 单数", "Odoo 出库单总数", "提供率 (%)"],
    ]
    for r in rows:
        sheet_rows.append(
            [
                r["year"],
                r["source"],
                r["with_pod"],
                r["odoo_orders"],
                r["coverage_rate"],
            ]
        )

    # Monthly sheet
    monthly_data = build_monthly_pod_report_data()
    monthly_sheet_rows = [
        ["月份", "承运商", "出库单总数", "已有 POD", "缺失 POD", "提供率 (%)"],
    ]
    for r in monthly_data.get("rows", []):
        monthly_sheet_rows.append(
            [
                r["month_key"],
                r["carrier"],
                r["total_orders"],
                r["with_pod"],
                r["without_pod"],
                r["coverage_rate"],
            ]
        )

    workbook_bytes = build_xlsx_workbook(
        [
            {"name": "年度 POD 提供率", "rows": sheet_rows},
            {"name": "月度 POD 提供率", "rows": monthly_sheet_rows},
        ],
        title="POD Coverage Report (Odoo Perspective)",
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "filename": f"pod_coverage_{stamp}.xlsx",
        "content": workbook_bytes,
    }
