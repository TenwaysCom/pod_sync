from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .odoo_missing_report import load_odoo_missing_snapshot
from .odoo_pod_compare import load_pod_odoo_compare_snapshot
from .pod_sources import POD_SOURCE_CARRIER_IDS, POD_SOURCE_NAME_BY_CARRIER_ID

STOCK_TRACKING_POD_IDS_PATH = Path(__file__).resolve().parents[1] / "runtime" / "stock_tracking_pod_picking_ids.json"


def _load_stock_tracking_pod_picking_ids() -> set[int]:
    """Load picking IDs that have a POD in stock.tracking (via pod_link)."""
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


def _build_other_carrier_rows(rows: list[dict]):
    grouped = defaultdict(lambda: {"carrier_name": "", "count": 0, "sample_picking_names": []})
    for row in rows:
        carrier_name = row.get("matched_odoo_carrier_names") or "-"
        item = grouped[carrier_name]
        item["carrier_name"] = carrier_name
        item["count"] += 1
        picking_names = [
            name.strip()
            for name in str(row.get("matched_odoo_picking_names") or "").split("|")
            if name.strip()
        ]
        for picking_name in picking_names:
            if picking_name not in item["sample_picking_names"] and len(item["sample_picking_names"]) < 5:
                item["sample_picking_names"].append(picking_name)
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["carrier_name"]))


def _parse_datetime(value: str | None):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _float_percent(numerator, denominator):
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100, 2)


def _clean_text(value):
    if isinstance(value, str):
        return value.strip() or None
    return value or None


def _resolve_tracking_number(row):
    return _clean_text(row.get("tracking_number")) or _clean_text(row.get("carrier_tracking_ref"))


def _summarize_rows(rows: list[dict], stock_tracking_ids: set[int]):
    total_count = len(rows)
    done_count = 0
    cancel_count = 0
    with_tracking_count = 0
    with_pod_link_count = 0
    ready_for_push_count = 0

    for row in rows:
        tracking_number = _resolve_tracking_number(row)
        has_pod = _row_has_pod(row, stock_tracking_ids)
        state = row.get("state")
        if state == "done":
            done_count += 1
        elif state == "cancel":
            cancel_count += 1
        if tracking_number:
            with_tracking_count += 1
        if has_pod:
            with_pod_link_count += 1
        if state == "done" and not has_pod:
            ready_for_push_count += 1

    return {
        "total_count": total_count,
        "done_count": done_count,
        "cancel_count": cancel_count,
        "with_tracking_count": with_tracking_count,
        "without_tracking_count": total_count - with_tracking_count,
        "with_pod_link_count": with_pod_link_count,
        "without_pod_link_count": total_count - with_pod_link_count,
        "ready_for_push_count": ready_for_push_count,
    }


def build_odoo_analysis_context(
    odoo_snapshot: dict | None,
    *,
    stock_tracking_ids: set[int] | None = None,
):
    if not odoo_snapshot:
        return {
            "overview": {},
            "field_rows": [],
            "state_rows": [],
            "carrier_state_rows": [],
        }

    rows = list(odoo_snapshot.get("total_detail_rows") or [])
    if stock_tracking_ids is None:
        stock_tracking_ids = _load_stock_tracking_pod_picking_ids()
    total_count = len(rows)
    with_tracking_count = 0
    with_pod_link_count = 0
    ready_for_push_count = 0
    state_counts = defaultdict(
        lambda: {
            "row_count": 0,
            "with_tracking_count": 0,
            "with_pod_link_count": 0,
        }
    )
    carrier_state_counts = defaultdict(
        lambda: {
            "row_count": 0,
            "with_tracking_count": 0,
            "with_pod_link_count": 0,
        }
    )

    for row in rows:
        tracking_number = _resolve_tracking_number(row)
        has_pod = _row_has_pod(row, stock_tracking_ids)
        state = row.get("state") or "-"
        carrier_id = row.get("carrier_id")
        carrier_name = row.get("carrier_name") or "-"

        if tracking_number:
            with_tracking_count += 1
        if has_pod:
            with_pod_link_count += 1
        if state == "done" and not has_pod:
            ready_for_push_count += 1

        state_counts[state]["row_count"] += 1
        carrier_state_counts[(carrier_id, carrier_name, state)]["row_count"] += 1
        if tracking_number:
            state_counts[state]["with_tracking_count"] += 1
            carrier_state_counts[(carrier_id, carrier_name, state)]["with_tracking_count"] += 1
        if has_pod:
            state_counts[state]["with_pod_link_count"] += 1
            carrier_state_counts[(carrier_id, carrier_name, state)]["with_pod_link_count"] += 1

    without_tracking_count = total_count - with_tracking_count
    without_pod_link_count = total_count - with_pod_link_count

    field_rows = [
        {
            "field_name": "odoo_picking_id",
            "source": "stock_picking.id / QueryDeluxe 64",
            "purpose": "Odoo 主键。后续回写、幂等推送、错误排查都以它为准。",
            "detail": f"{total_count} / {total_count} 非空，覆盖率 100%。",
        },
        {
            "field_name": "picking_name",
            "source": "stock_picking.name / QueryDeluxe 64",
            "purpose": "业务出库单号。CMC 类文件主要依靠它与 POD 识别结果做匹配。",
            "detail": f"{sum(1 for row in rows if _clean_text(row.get('picking_name')))} / {total_count} 非空。",
        },
        {
            "field_name": "carrier_id + carrier_name",
            "source": "stock_picking.carrier_id / 承运商映射",
            "purpose": "区分 Odoo 当前承运商；POD 单池按 24=EMTC、25=CMC、38=Westerman 统计，总表保留全部 outgoing carrier。",
            "detail": f"当前总表共 {len({(row.get('carrier_id'), row.get('carrier_name')) for row in rows})} 个 carrier，其中 POD 相关 carrier_id 为 24 / 25 / 38。",
        },
        {
            "field_name": "state",
            "source": "stock_picking.state",
            "purpose": "区分 done、cancel 等状态，决定是否可推送以及命中已取消单统计。",
            "detail": "当前状态分布已在下方状态明细表展开。",
        },
        {
            "field_name": "business_datetime",
            "source": "COALESCE(date_done, scheduled_date, create_date)",
            "purpose": "统一业务时间，用于排序、月报统计和后续增量边界。",
            "detail": f"当前最早 {rows[-1].get('business_datetime') if rows else '-'}，最新 {rows[0].get('business_datetime') if rows else '-'}。",
        },
        {
            "field_name": "tracking_number",
            "source": "stock_picking.carrier_tracking_ref",
            "purpose": "tracking number。EMTC 类文件主要依靠它与 POD 识别结果做匹配。",
            "detail": f"{with_tracking_count} / {total_count} 非空，缺失 {without_tracking_count}，覆盖率 {_float_percent(with_tracking_count, total_count)}%。",
        },
        {
            "field_name": "pod_coverage",
            "source": "stock.tracking.pod_link + stock.picking.x_studio_pod_link",
            "purpose": "以 stock.tracking.pod_link 为实际写入源，并兼容出库单上的历史/聚合镜像。",
            "detail": f"{with_pod_link_count} / {total_count} 已有 POD，尚无 POD {without_pod_link_count}，done 且待核对 {ready_for_push_count}。是否可写仍以标准推送预检为准。",
        },
    ]

    state_rows = []
    for state, counts in sorted(state_counts.items(), key=lambda item: (-item[1]["row_count"], item[0])):
        state_rows.append(
            {
                "state": state,
                "row_count": counts["row_count"],
                "row_percent": _float_percent(counts["row_count"], total_count),
                "with_tracking_count": counts["with_tracking_count"],
                "tracking_coverage_percent": _float_percent(counts["with_tracking_count"], counts["row_count"]),
                "with_pod_link_count": counts["with_pod_link_count"],
                "pod_link_coverage_percent": _float_percent(counts["with_pod_link_count"], counts["row_count"]),
            }
        )

    carrier_state_rows = []
    for (carrier_id, carrier_name, state), counts in sorted(
        carrier_state_counts.items(),
        key=lambda item: (item[0][0] or 0, item[0][2], -item[1]["row_count"]),
    ):
        carrier_state_rows.append(
            {
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "state": state,
                "row_count": counts["row_count"],
                "with_tracking_count": counts["with_tracking_count"],
                "with_pod_link_count": counts["with_pod_link_count"],
                "tracking_coverage_percent": _float_percent(counts["with_tracking_count"], counts["row_count"]),
                "pod_link_coverage_percent": _float_percent(counts["with_pod_link_count"], counts["row_count"]),
            }
        )

    return {
        "overview": {
            "total_count": total_count,
            "with_tracking_count": with_tracking_count,
            "without_tracking_count": without_tracking_count,
            "with_pod_link_count": with_pod_link_count,
            "without_pod_link_count": without_pod_link_count,
            "ready_for_push_count": ready_for_push_count,
            "data_source": (odoo_snapshot.get("endpoints") or {}).get("total_detail_url"),
        },
        "field_rows": field_rows,
        "state_rows": state_rows,
        "carrier_state_rows": carrier_state_rows,
    }


def build_pod_scope_context(
    odoo_snapshot: dict | None,
    *,
    stock_tracking_ids: set[int] | None = None,
):
    if not odoo_snapshot:
        return {
            "overview": {},
            "carrier_rows": [],
        }

    if stock_tracking_ids is None:
        stock_tracking_ids = _load_stock_tracking_pod_picking_ids()
    all_rows = list(odoo_snapshot.get("total_detail_rows") or [])
    rows = [row for row in all_rows if row.get("carrier_id") in POD_SOURCE_CARRIER_IDS]
    overview = _summarize_rows(rows, stock_tracking_ids)
    overview["carrier_count"] = len({row.get("carrier_id") for row in rows if row.get("carrier_id") is not None})

    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.get("carrier_id"), row.get("carrier_name") or "-")].append(row)

    carrier_rows = []
    for (carrier_id, carrier_name), carrier_items in sorted(grouped.items(), key=lambda item: item[0][0] or 0):
        summary = _summarize_rows(carrier_items, stock_tracking_ids)
        carrier_rows.append(
            {
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "total_count": summary["total_count"],
                "done_count": summary["done_count"],
                "cancel_count": summary["cancel_count"],
                "with_tracking_count": summary["with_tracking_count"],
                "without_tracking_count": summary["without_tracking_count"],
                "tracking_coverage_percent": _float_percent(summary["with_tracking_count"], summary["total_count"]),
                "with_pod_link_count": summary["with_pod_link_count"],
                "without_pod_link_count": summary["without_pod_link_count"],
                "pod_link_coverage_percent": _float_percent(summary["with_pod_link_count"], summary["total_count"]),
                "ready_for_push_count": summary["ready_for_push_count"],
            }
        )

    return {
        "overview": overview,
        "carrier_rows": carrier_rows,
    }


def build_monthly_tracking_rows(odoo_snapshot: dict | None):
    if not odoo_snapshot:
        return []

    grouped = defaultdict(
        lambda: {
            "total_done_outgoing": 0,
            "done_count": 0,
            "cancel_count": 0,
            "with_tracking_number": 0,
            "without_tracking_number": 0,
        }
    )
    for row in odoo_snapshot.get("total_detail_rows") or []:
        dt = _parse_datetime(row.get("business_datetime"))
        if not dt:
            continue
        month_key = dt.strftime("%Y-%m")
        key = (month_key, row.get("carrier_id"), row.get("carrier_name"))
        grouped[key]["total_done_outgoing"] += 1
        if row.get("state") == "done":
            grouped[key]["done_count"] += 1
        elif row.get("state") == "cancel":
            grouped[key]["cancel_count"] += 1
        if row.get("tracking_status") == "missing_tracking":
            grouped[key]["without_tracking_number"] += 1
        else:
            grouped[key]["with_tracking_number"] += 1

    rows = []
    for (month_key, carrier_id, carrier_name), counts in grouped.items():
        rows.append(
            {
                "month_key": month_key,
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "total_done_outgoing": counts["total_done_outgoing"],
                "done_count": counts["done_count"],
                "cancel_count": counts["cancel_count"],
                "with_tracking_number": counts["with_tracking_number"],
                "without_tracking_number": counts["without_tracking_number"],
                "missing_rate_percent": _float_percent(counts["without_tracking_number"], counts["total_done_outgoing"]),
            }
        )
    rows.sort(key=lambda item: (item["month_key"], item.get("carrier_id") or 0), reverse=True)
    return rows


def build_monthly_tracking_overview(monthly_rows: list[dict] | None, *, limit: int = 6):
    if not monthly_rows:
        return []

    grouped = defaultdict(
        lambda: {
            "total_done_outgoing": 0,
            "done_count": 0,
            "cancel_count": 0,
            "with_tracking_number": 0,
            "without_tracking_number": 0,
            "carrier_names": [],
        }
    )
    for row in monthly_rows:
        month_key = row.get("month_key")
        if not month_key:
            continue
        grouped[month_key]["total_done_outgoing"] += row.get("total_done_outgoing", 0)
        grouped[month_key]["done_count"] += row.get("done_count", 0)
        grouped[month_key]["cancel_count"] += row.get("cancel_count", 0)
        grouped[month_key]["with_tracking_number"] += row.get("with_tracking_number", 0)
        grouped[month_key]["without_tracking_number"] += row.get("without_tracking_number", 0)
        carrier_name = row.get("carrier_name")
        if carrier_name and carrier_name not in grouped[month_key]["carrier_names"]:
            grouped[month_key]["carrier_names"].append(carrier_name)

    overview_rows = []
    for month_key, counts in grouped.items():
        overview_rows.append(
            {
                "month_key": month_key,
                "total_done_outgoing": counts["total_done_outgoing"],
                "done_count": counts["done_count"],
                "cancel_count": counts["cancel_count"],
                "with_tracking_number": counts["with_tracking_number"],
                "without_tracking_number": counts["without_tracking_number"],
                "missing_rate_percent": _float_percent(
                    counts["without_tracking_number"],
                    counts["total_done_outgoing"],
                ),
                "carrier_names": " / ".join(counts["carrier_names"]),
            }
        )
    overview_rows.sort(key=lambda item: item["month_key"], reverse=True)
    return overview_rows[:limit]


def build_all_time_tracking_context(
    odoo_snapshot: dict | None,
    *,
    stock_tracking_ids: set[int] | None = None,
):
    if not odoo_snapshot:
        return {
            "overview": {},
            "carrier_rows": [],
        }

    if stock_tracking_ids is None:
        stock_tracking_ids = _load_stock_tracking_pod_picking_ids()
    rows = list(odoo_snapshot.get("total_detail_rows") or [])

    carrier_grouped = defaultdict(list)
    for row in rows:
        key = (row.get("carrier_id"), row.get("carrier_name"))
        carrier_grouped[key].append(row)

    total_count = len(rows)
    carrier_rows = []
    for (carrier_id, carrier_name), carrier_items in sorted(carrier_grouped.items(), key=lambda item: item[0][0] or 0):
        counts = _summarize_rows(carrier_items, stock_tracking_ids)
        carrier_rows.append(
            {
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "total_done_outgoing": counts["total_count"],
                "done_count": counts["done_count"],
                "cancel_count": counts["cancel_count"],
                "with_tracking_number": counts["with_tracking_count"],
                "without_tracking_number": counts["without_tracking_count"],
                "with_pod_link_count": counts["with_pod_link_count"],
                "without_pod_link_count": counts["without_pod_link_count"],
                "missing_rate_percent": _float_percent(counts["without_tracking_count"], counts["total_count"]),
                "pod_link_coverage_percent": _float_percent(counts["with_pod_link_count"], counts["total_count"]),
            }
        )

    summary = _summarize_rows(rows, stock_tracking_ids)
    return {
        "overview": {
            "total_count": total_count,
            "with_tracking_count": summary["with_tracking_count"],
            "without_tracking_count": summary["without_tracking_count"],
            "with_pod_link_count": summary["with_pod_link_count"],
            "without_pod_link_count": summary["without_pod_link_count"],
            "ready_for_push_count": summary["ready_for_push_count"],
        },
        "carrier_rows": carrier_rows,
    }


def build_compare_analysis_context(compare_snapshot: dict | None):
    if not compare_snapshot:
        return {
            "overview": {},
            "status_rows": [],
            "other_carrier_rows": [],
        }

    totals = compare_snapshot.get("totals") or {}
    status_rows = [
        {
            "status": "matched",
            "label": "同承运商命中",
            "count": totals.get("matched_count", 0),
            "description": "POD 匹配键直接在当前承运商范围内命中 Odoo。",
            "action": "可以作为最稳定的自动推送对象。",
        },
        {
            "status": "matched_cancelled",
            "label": "命中已取消单",
            "count": totals.get("matched_cancelled_count", 0),
            "description": "Odoo 有单，但状态已经是 cancel。",
            "action": "通常不应自动推送，需要业务确认。",
        },
        {
            "status": "matched_other_carrier",
            "label": "命中其他承运商",
            "count": totals.get("matched_other_carrier_count", 0),
            "description": "POD 匹配键没有命中当前来源对应的承运商，但在其他 Odoo carrier 下命中 done 单据。",
            "action": "先核对承运商归属或来源分类，不建议直接自动推送。",
        },
        {
            "status": "odoo_not_found",
            "label": "按匹配键未命中 Odoo",
            "count": totals.get("not_found_count", 0),
            "description": "POD 已识别出出库单号或 tracking number，但按当前来源对应的 carrier + 匹配键，在 Odoo 总明细中没有命中。",
            "action": "重点排查 Odoo 缺单、承运商口径、命名差异或识别规则遗漏。",
        },
        {
            "status": "multiple_odoo_matches",
            "label": "Odoo 命中多条",
            "count": totals.get("ambiguous_count", 0),
            "description": "同一个匹配键在 Odoo 中对应多条记录。",
            "action": "需要补充约束或人工判定。",
        },
    ]

    return {
        "overview": {
            "unique_identifier_count": totals.get("unique_identifier_count", 0),
            "match_rate_percent": totals.get("match_rate_percent", 0),
            "matched_any_count": (
                totals.get("matched_count", 0)
                + totals.get("matched_cancelled_count", 0)
                + totals.get("matched_other_carrier_count", 0)
            ),
            "review_document_count": totals.get("review_document_count", 0),
            "stock_tracking_pod_count": totals.get("stock_tracking_pod_count", 0),
        },
        "status_rows": status_rows,
        "other_carrier_rows": _build_other_carrier_rows(compare_snapshot.get("matched_other_carrier_rows") or []),
    }


def build_monthly_pod_coverage_rows(
    odoo_snapshot: dict | None,
    *,
    stock_tracking_ids: set[int] | None = None,
):
    """Monthly POD coverage from the Odoo perspective for all POD sources.

    Counts a row as "with POD" if either:
      - stock.picking.x_studio_pod_link is set, OR
      - a stock.tracking record exists for this picking with pod_link set.
    """
    if not odoo_snapshot:
        return []

    if stock_tracking_ids is None:
        stock_tracking_ids = _load_stock_tracking_pod_picking_ids()
    grouped = defaultdict(lambda: {"total_orders": 0, "with_pod": 0, "without_pod": 0})

    for row in odoo_snapshot.get("total_detail_rows") or []:
        cid = row.get("carrier_id")
        carrier_name = POD_SOURCE_NAME_BY_CARRIER_ID.get(cid)
        if carrier_name is None:
            continue
        dt = _parse_datetime(row.get("business_datetime"))
        if not dt:
            continue
        month_key = dt.strftime("%Y-%m")
        key = (month_key, cid, carrier_name)
        grouped[key]["total_orders"] += 1
        if _row_has_pod(row, stock_tracking_ids):
            grouped[key]["with_pod"] += 1
        else:
            grouped[key]["without_pod"] += 1

    rows = []
    for (month_key, carrier_id, carrier_name), counts in grouped.items():
        rows.append(
            {
                "month_key": month_key,
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "total_orders": counts["total_orders"],
                "with_pod": counts["with_pod"],
                "without_pod": counts["without_pod"],
                "pod_coverage_rate": _float_percent(counts["with_pod"], counts["total_orders"]),
            }
        )
    rows.sort(key=lambda item: (item["month_key"], item.get("carrier_id") or 0), reverse=True)
    return rows


def build_monthly_pod_coverage_overview(monthly_pod_rows: list[dict] | None, *, limit: int = 6):
    """Aggregate monthly POD coverage across all carriers."""
    if not monthly_pod_rows:
        return []

    grouped = defaultdict(lambda: {"total_orders": 0, "with_pod": 0, "without_pod": 0})
    for row in monthly_pod_rows:
        month_key = row.get("month_key")
        if not month_key:
            continue
        grouped[month_key]["total_orders"] += row.get("total_orders", 0)
        grouped[month_key]["with_pod"] += row.get("with_pod", 0)
        grouped[month_key]["without_pod"] += row.get("without_pod", 0)

    overview_rows = []
    for month_key, counts in grouped.items():
        overview_rows.append(
            {
                "month_key": month_key,
                "total_orders": counts["total_orders"],
                "with_pod": counts["with_pod"],
                "without_pod": counts["without_pod"],
                "pod_coverage_rate": _float_percent(counts["with_pod"], counts["total_orders"]),
            }
        )
    overview_rows.sort(key=lambda item: item["month_key"], reverse=True)
    return overview_rows[:limit]


def build_report_context():
    odoo_snapshot = load_odoo_missing_snapshot()
    compare_snapshot = load_pod_odoo_compare_snapshot()
    stock_tracking_ids = _load_stock_tracking_pod_picking_ids() if odoo_snapshot else set()
    monthly_rows = build_monthly_tracking_rows(odoo_snapshot)
    monthly_overview_rows = build_monthly_tracking_overview(monthly_rows)
    monthly_pod_rows = build_monthly_pod_coverage_rows(
        odoo_snapshot,
        stock_tracking_ids=stock_tracking_ids,
    )
    monthly_pod_overview_rows = build_monthly_pod_coverage_overview(monthly_pod_rows)
    odoo_analysis = build_odoo_analysis_context(
        odoo_snapshot,
        stock_tracking_ids=stock_tracking_ids,
    )
    pod_scope_analysis = build_pod_scope_context(
        odoo_snapshot,
        stock_tracking_ids=stock_tracking_ids,
    )
    all_time_analysis = build_all_time_tracking_context(
        odoo_snapshot,
        stock_tracking_ids=stock_tracking_ids,
    )
    compare_analysis = build_compare_analysis_context(compare_snapshot)
    return {
        "odoo_snapshot": odoo_snapshot,
        "compare_snapshot": compare_snapshot,
        "monthly_rows": monthly_rows,
        "monthly_overview_rows": monthly_overview_rows,
        "monthly_pod_rows": monthly_pod_rows,
        "monthly_pod_overview_rows": monthly_pod_overview_rows,
        "odoo_analysis": odoo_analysis,
        "pod_scope_analysis": pod_scope_analysis,
        "all_time_analysis": all_time_analysis,
        "compare_analysis": compare_analysis,
    }
