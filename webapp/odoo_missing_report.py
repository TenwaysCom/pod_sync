from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / "runtime" / "odoo_missing_tracking"
SNAPSHOT_PATH = RUNTIME_DIR / "latest_snapshot.json"
SUMMARY_CSV_PATH = RUNTIME_DIR / "odoo_missing_tracking_summary.csv"
TOTAL_DETAIL_CSV_PATH = RUNTIME_DIR / "odoo_tracking_total_detail.csv"
MISSING_DETAIL_CSV_PATH = RUNTIME_DIR / "odoo_missing_tracking_detail.csv"
REPORT_MD_PATH = RUNTIME_DIR / "odoo_missing_tracking_report.md"

def _get_default_detail_url(config_path: str = "config.local.json") -> str:
    from webapp.actions import resolve_project_path
    cfg_path = resolve_project_path(config_path, "config.local.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    odoo_url = cfg.get("odoo", {}).get("url", "").rstrip("/")
    query_id = cfg.get("querydeluxe", {}).get("missing_tracking_query_id", 64)
    if not odoo_url:
        raise ValueError("Odoo URL not configured in config")
    return f"{odoo_url}/api/querydeluxe/{query_id}/data"

SUMMARY_FIELDS = [
    "carrier_id",
    "carrier_name",
    "counting_start_datetime",
    "counting_end_datetime",
    "total_done_outgoing",
    "done_count",
    "cancel_count",
    "with_tracking_number",
    "without_tracking_number",
    "missing_rate_percent",
    "detail_row_count",
    "detail_matches_summary",
    "oldest_missing_datetime",
    "latest_missing_datetime",
]

TOTAL_DETAIL_FIELDS = [
    "carrier_id",
    "carrier_name",
    "odoo_picking_id",
    "picking_name",
    "state",
    "business_datetime",
    "tracking_number",
    "x_studio_pod_link",
    "tracking_status",
]


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows, fieldnames):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _parse_datetime(value: str | None):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _float_percent(numerator, denominator):
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100, 2)


def _fetch_querydeluxe_rows(url: str, *, timeout: int = 30, session: requests.Session | None = None):
    client = session or requests.Session()
    response = client.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise ValueError(f"QueryDeluxe endpoint returned success=false: {url}")
    data = payload.get("data") or {}
    return {
        "url": url,
        "header": list(data.get("header") or []),
        "rows": list(data.get("data") or []),
        "model": data.get("model"),
        "ids": list(data.get("ids") or []),
    }


def _detail_sort_key(row):
    return (
        _parse_datetime(row.get("business_datetime")) or datetime.min,
        int(row.get("odoo_picking_id") or 0),
    )


def _tracking_status_label(value: str | None):
    return "with_tracking" if value not in (None, "") else "missing_tracking"


def _clean_text(value):
    if isinstance(value, str):
        return value.strip() or None
    return value or None


def _resolve_tracking_number(row):
    return _clean_text(row.get("tracking_number")) or _clean_text(row.get("carrier_tracking_ref"))


def build_odoo_missing_snapshot(*, total_detail_rows, detail_url: str):
    total_detail_rows = [dict(row) for row in total_detail_rows]
    for row in total_detail_rows:
        tracking_number = _resolve_tracking_number(row)
        row["tracking_number"] = tracking_number
        # Keep the legacy key populated for backward compatibility with older snapshots and compare code.
        row["carrier_tracking_ref"] = tracking_number
        row["x_studio_pod_link"] = _clean_text(row.get("x_studio_pod_link"))
        row["tracking_status"] = _tracking_status_label(tracking_number)
    total_detail_rows.sort(key=_detail_sort_key, reverse=True)

    carrier_rows = defaultdict(list)
    for row in total_detail_rows:
        carrier_rows[(row.get("carrier_id"), row.get("carrier_name"))].append(row)

    enriched_summary_rows = []
    for (carrier_id, carrier_name), rows in sorted(carrier_rows.items(), key=lambda item: (item[0][0] or 0, item[0][1] or "")):
        missing_rows = [row for row in rows if row.get("tracking_status") == "missing_tracking"]
        done_rows = [row for row in rows if row.get("state") == "done"]
        cancel_rows = [row for row in rows if row.get("state") == "cancel"]
        datetimes = [row.get("business_datetime") for row in rows if row.get("business_datetime")]
        missing_datetimes = [row.get("business_datetime") for row in missing_rows if row.get("business_datetime")]
        total_count = len(rows)
        missing_count = len(missing_rows)
        with_count = total_count - missing_count
        enriched_summary_rows.append(
            {
                "carrier_id": carrier_id,
                "carrier_name": carrier_name,
                "counting_start_datetime": datetimes[-1] if datetimes else None,
                "counting_end_datetime": datetimes[0] if datetimes else None,
                "total_done_outgoing": total_count,
                "done_count": len(done_rows),
                "cancel_count": len(cancel_rows),
                "with_tracking_number": with_count,
                "without_tracking_number": missing_count,
                "missing_rate_percent": _float_percent(missing_count, total_count),
                "detail_row_count": missing_count,
                "detail_matches_summary": True,
                "oldest_missing_datetime": missing_datetimes[-1] if missing_datetimes else None,
                "latest_missing_datetime": missing_datetimes[0] if missing_datetimes else None,
            }
        )

    missing_detail_rows = [row for row in total_detail_rows if row.get("tracking_status") == "missing_tracking"]

    total_done_outgoing = len(total_detail_rows)
    total_without_tracking = len(missing_detail_rows)
    total_with_tracking = total_done_outgoing - total_without_tracking
    done_count = sum(1 for row in total_detail_rows if row.get("state") == "done")
    cancel_count = sum(1 for row in total_detail_rows if row.get("state") == "cancel")

    snapshot = {
        "generated_at": _utc_now(),
        "endpoints": {
            "total_detail_url": detail_url,
        },
        "totals": {
            "carrier_count": len(enriched_summary_rows),
            "total_done_outgoing": total_done_outgoing,
            "done_count": done_count,
            "cancel_count": cancel_count,
            "with_tracking_number": total_with_tracking,
            "without_tracking_number": total_without_tracking,
            "detail_row_count": len(missing_detail_rows),
            "missing_rate_percent": _float_percent(total_without_tracking, total_done_outgoing),
            "detail_matches_summary": True,
        },
        "summary_rows": enriched_summary_rows,
        "total_detail_rows": total_detail_rows,
        "missing_detail_rows": missing_detail_rows,
        "status_counts": dict(Counter(row.get("tracking_status") for row in total_detail_rows)),
    }
    return snapshot


def render_odoo_missing_markdown(snapshot):
    totals = snapshot.get("totals") or {}
    summary_rows = snapshot.get("summary_rows") or []
    lines = [
        "# Odoo 缺失 tracking number 报告",
        "",
        f"- 生成时间：{snapshot.get('generated_at') or '-'}",
        f"- 总明细接口：`{snapshot.get('endpoints', {}).get('total_detail_url') or '-'}`",
        "",
        "## 总览",
        "",
        f"- 总出库：{totals.get('total_done_outgoing', 0)}",
        f"- done：{totals.get('done_count', 0)}",
        f"- cancel：{totals.get('cancel_count', 0)}",
        f"- 已有 tracking number：{totals.get('with_tracking_number', 0)}",
        f"- 缺失 tracking number：{totals.get('without_tracking_number', 0)}",
        f"- 缺失率：{totals.get('missing_rate_percent', 0)}%",
        f"- 明细行数与汇总是否一致：{'是' if totals.get('detail_matches_summary') else '否'}",
        "",
        "## 承运商汇总",
        "",
        "| 承运商 | 起算时间 | 截止时间 | 总出库 | done | cancel | 有 tracking number | 缺失 tracking number | 缺失率 | 明细校验 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary_rows:
        lines.append(
            "| {carrier_name} | {counting_start_datetime} | {counting_end_datetime} | {total_done_outgoing} | "
            "{done_count} | {cancel_count} | {with_tracking_number} | {without_tracking_number} | {missing_rate_percent}% | {detail_ok} |".format(
                carrier_name=row.get("carrier_name") or "-",
                counting_start_datetime=row.get("counting_start_datetime") or "-",
                counting_end_datetime=row.get("counting_end_datetime") or "-",
                total_done_outgoing=row.get("total_done_outgoing") or 0,
                done_count=row.get("done_count") or 0,
                cancel_count=row.get("cancel_count") or 0,
                with_tracking_number=row.get("with_tracking_number") or 0,
                without_tracking_number=row.get("without_tracking_number") or 0,
                missing_rate_percent=row.get("missing_rate_percent") or 0,
                detail_ok="一致" if row.get("detail_matches_summary") else "不一致",
            )
        )
    lines.extend(
        [
            "",
            "## 下载文件",
            "",
            "- `odoo_tracking_total_detail.csv`",
            "- `odoo_missing_tracking_summary.csv`",
            "- `odoo_missing_tracking_detail.csv`",
            "- `latest_snapshot.json`",
            "",
        ]
    )
    return "\n".join(lines)


def save_odoo_missing_snapshot(snapshot):
    _ensure_directory(RUNTIME_DIR)
    _write_json(SNAPSHOT_PATH, snapshot)
    _write_csv(SUMMARY_CSV_PATH, snapshot.get("summary_rows") or [], SUMMARY_FIELDS)
    _write_csv(TOTAL_DETAIL_CSV_PATH, snapshot.get("total_detail_rows") or [], TOTAL_DETAIL_FIELDS)
    _write_csv(MISSING_DETAIL_CSV_PATH, snapshot.get("missing_detail_rows") or [], TOTAL_DETAIL_FIELDS)
    REPORT_MD_PATH.write_text(render_odoo_missing_markdown(snapshot), encoding="utf-8")
    return {
        "snapshot_json": SNAPSHOT_PATH,
        "summary_csv": SUMMARY_CSV_PATH,
        "total_detail_csv": TOTAL_DETAIL_CSV_PATH,
        "missing_detail_csv": MISSING_DETAIL_CSV_PATH,
        "report_md": REPORT_MD_PATH,
    }


def refresh_odoo_missing_snapshot(
    *,
    detail_url: str | None = None,
    timeout: int = 30,
    session: requests.Session | None = None,
):
    if detail_url is None:
        detail_url = _get_default_detail_url()
    detail_payload = _fetch_querydeluxe_rows(detail_url, timeout=timeout, session=session)
    snapshot = build_odoo_missing_snapshot(total_detail_rows=detail_payload["rows"], detail_url=detail_url)
    save_odoo_missing_snapshot(snapshot)
    return snapshot


def load_odoo_missing_snapshot():
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def resolve_odoo_missing_artifact(artifact_key: str):
    mapping = {
        "snapshot_json": SNAPSHOT_PATH,
        "summary_csv": SUMMARY_CSV_PATH,
        "total_detail_csv": TOTAL_DETAIL_CSV_PATH,
        "missing_detail_csv": MISSING_DETAIL_CSV_PATH,
        "report_md": REPORT_MD_PATH,
    }
    path = mapping.get(artifact_key)
    if not path or not path.exists():
        return None
    return path


def build_odoo_missing_listing(
    *,
    query: str = "",
    carrier: str = "",
    tracking_status: str = "missing_tracking",
    state: str = "",
    pod_link_status: str = "",
    page: int = 1,
    page_size: int = 25,
    snapshot: dict | None = None,
):
    snapshot = snapshot or load_odoo_missing_snapshot()
    if not snapshot:
        return None

    rows = list(snapshot.get("total_detail_rows") or [])
    query_text = (query or "").strip().lower()
    filtered = []
    carrier_options = {}
    state_options = {}
    for row in rows:
        carrier_key = str(row.get("carrier_id") or "")
        carrier_label = f"{row.get('carrier_name') or '-'} ({carrier_key})"
        carrier_options[carrier_key] = carrier_label
        state_value = str(row.get("state") or "")
        if state_value:
            state_options[state_value] = state_value
        search_blob = " ".join(
            str(value or "")
            for value in (
                row.get("carrier_id"),
                row.get("carrier_name"),
                row.get("odoo_picking_id"),
                row.get("picking_name"),
                row.get("state"),
                row.get("business_datetime"),
                row.get("tracking_number"),
                row.get("x_studio_pod_link"),
            )
        ).lower()
        if query_text and query_text not in search_blob:
            continue
        if carrier and carrier_key != str(carrier):
            continue
        if tracking_status and row.get("tracking_status") != tracking_status:
            continue
        if state and state_value != state:
            continue
        has_pod_link = bool(_clean_text(row.get("x_studio_pod_link")))
        if pod_link_status == "with_pod_link" and not has_pod_link:
            continue
        if pod_link_status == "missing_pod_link" and has_pod_link:
            continue
        filtered.append(row)

    safe_page_size = max(1, min(int(page_size or 25), 200))
    safe_page = max(1, int(page or 1))
    total = len(filtered)
    total_pages = max(1, math.ceil(total / safe_page_size))
    if safe_page > total_pages:
        safe_page = total_pages
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size

    return {
        "generated_at": snapshot.get("generated_at"),
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "page_start": start + 1 if total else 0,
        "page_end": min(end, total),
        "items": filtered[start:end],
        "filters": {
            "query": query,
            "carrier": carrier,
            "tracking_status": tracking_status,
            "state": state,
            "pod_link_status": pod_link_status,
        },
        "totals": snapshot.get("totals") or {},
        "tracking_status_options": [
            {"value": "", "label": "全部状态"},
            {"value": "missing_tracking", "label": "缺失 tracking number"},
            {"value": "with_tracking", "label": "已有 tracking number"},
        ],
        "pod_link_status_options": [
            {"value": "", "label": "全部 POD 链接状态"},
            {"value": "with_pod_link", "label": "已有 POD 链接"},
            {"value": "missing_pod_link", "label": "待写入 POD 链接"},
        ],
        "state_options": [{"value": "", "label": "全部单据状态"}]
        + [{"value": value, "label": value} for value in sorted(state_options)],
        "carrier_options": [{"value": "", "label": "全部承运商"}]
        + [{"value": value, "label": label} for value, label in sorted(carrier_options.items(), key=lambda item: item[0])],
    }
