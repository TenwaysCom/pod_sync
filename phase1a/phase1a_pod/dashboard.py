import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

from .io_utils import write_json


DASHBOARD_ARTIFACT_LABELS = {
    "parsed_documents_json": "解析结果 JSON",
    "matched_report_csv": "可推送报告 CSV",
    "matched_report_json": "可推送报告 JSON",
    "unmatched_report_csv": "待处理报告 CSV",
    "unmatched_report_json": "待处理报告 JSON",
    "payload_json": "Odoo 回传 Payload",
    "summary_json": "运行摘要 JSON",
    "dashboard_json": "巡检数据 JSON",
    "dashboard_html": "巡检页面 HTML",
}

DASHBOARD_STATUS_LABELS = {
    "matched": "已匹配",
    "unmatched": "未匹配",
    "ready": "可推送",
    "needs_review": "待处理",
}

DASHBOARD_SOURCE_STATUS_LABELS = {
    "ok": "正常",
    "missing_manifest": "缺少 Manifest",
}

DASHBOARD_SOURCE_TYPE_LABELS = {
    "onedrive": "OneDrive",
    "gdrive": "Google Drive",
    "westerman_api": "Westerman API",
}

DASHBOARD_MATCH_KEY_LABELS = {
    "picking_name": "出库单号",
    "tracking_number": "tracking number",
}

DASHBOARD_REASON_LABELS = {
    "NO_PICKING_FOUND": "未找到对应出库单",
    "INVALID_PICKING_KEY": "无法解析出有效出库单号",
    "INVALID_TRACKING_KEY": "无法解析出有效 tracking number",
    "MULTIPLE_PICKING_CANDIDATES": "命中多个候选出库单",
    "MULTIPLE_TRACKING_CANDIDATES": "命中多个候选 tracking number",
    "NO_IDENTIFIER_FOUND": "未解析出可用标识",
    "ODOO_SCOPE_FILTERED_OUT": "被当前 Odoo Scope 过滤",
    "PICKING_NOT_ELIGIBLE": "出库单不满足回填条件",
    "UNKNOWN": "未知原因",
}

DASHBOARD_REASON_MESSAGE_LABELS = {
    "NO_PICKING_FOUND": "未在当前 Odoo Scope 中找到匹配的出库单号或 tracking number。",
    "INVALID_PICKING_KEY": "文件名或路径里没有提取出可用的出库单号。",
    "INVALID_TRACKING_KEY": "文件名或路径里没有提取出可用的 tracking number。",
    "MULTIPLE_PICKING_CANDIDATES": "同一份 POD 文件命中了多个符合条件的 Odoo 出库单。",
    "MULTIPLE_TRACKING_CANDIDATES": "同一份 POD 文件命中了多个 tracking number，当前无法直接判定匹配键。",
    "NO_IDENTIFIER_FOUND": "文件名或路径里没有提取出可直接回传 Odoo 的出库单号或 tracking number。",
    "ODOO_SCOPE_FILTERED_OUT": "有匹配记录，但被当前导入的 Odoo Scope 过滤掉了。",
    "PICKING_NOT_ELIGIBLE": "有匹配记录，但这些出库单当前不满足 POD 回填条件。",
}

DASHBOARD_PARSE_NOTE_LABELS = {
    "picking_name_not_found": "未解析出出库单号",
    "source_url_missing": "缺少源文件链接",
    "multiple_picking_candidates": "文件名中包含多个出库单候选",
}


def build_dashboard_payload(*, summary, source_statuses, parsed_documents, matched_rows, unmatched_rows, payload):
    processing_mode = summary.get("processing_mode") or "scope_match"
    storage_root = _resolve_storage_root(summary.get("sync_config_path"))
    status_labels = _mode_status_labels(processing_mode)
    reason_labels = _mode_reason_labels(processing_mode)
    reason_message_labels = _mode_reason_message_labels(processing_mode)
    ui = _dashboard_ui(processing_mode)
    reason_counts = Counter(row.get("reason_code") or "UNKNOWN" for row in unmatched_rows)
    parse_note_counts = Counter()
    source_totals = defaultdict(
        lambda: {
            "total_documents": 0,
            "matched_documents": 0,
            "unmatched_documents": 0,
            "missing_url_documents": 0,
            "parse_issue_documents": 0,
        }
    )

    recent_documents = []
    for document in sorted(parsed_documents, key=lambda row: (row.get("source_modified_at") or "", row.get("file_name") or ""), reverse=True):
        source_key = _source_key(document["source_name"], document["source_type"])
        local_file_context = _local_file_context(document.get("local_path"), storage_root)
        source_totals[source_key]["total_documents"] += 1
        if document.get("match_status") in {"matched", "ready"}:
            source_totals[source_key]["matched_documents"] += 1
        else:
            source_totals[source_key]["unmatched_documents"] += 1
        if not document.get("file_url"):
            source_totals[source_key]["missing_url_documents"] += 1
        if document.get("parse_notes"):
            source_totals[source_key]["parse_issue_documents"] += 1
            for note in document["parse_notes"]:
                parse_note_counts[note] += 1

        recent_documents.append(
            {
                "document_uid": document["document_uid"],
                "source_name": document["source_name"],
                "source_type": document["source_type"],
                "file_name": document["file_name"],
                "source_path": document["source_path"],
                "source_modified_at": document.get("source_modified_at"),
                "match_status": document.get("match_status"),
                "selected_odoo_picking_ref": document.get("selected_odoo_picking_ref"),
                "reason_code": document.get("reason_code"),
                "reason_message": document.get("reason_message"),
                "match_key_type": document.get("match_key_type"),
                "match_key": document.get("match_key"),
                "primary_picking_name": document.get("primary_picking_name"),
                "primary_tracking_number": document.get("primary_tracking_number"),
                "file_url": document.get("file_url"),
                "local_path": document.get("local_path"),
                "local_relative_path": local_file_context["local_relative_path"],
                "local_file_href": local_file_context["local_file_href"],
                "parse_notes": document.get("parse_notes") or [],
            }
        )

    source_cards = []
    for status in source_statuses:
        merged = {
            **status,
            **source_totals[_source_key(status["source_name"], status["source_type"])],
        }
        source_cards.append(merged)

    source_cards.sort(key=lambda row: (row["status"] != "ok", row["source_name"], row["source_type"]))
    matched_pickings = []
    for item in payload.get("items", []):
        primary_document = item["documents"][0] if item.get("documents") else {}
        label = (
            item.get("display_label")
            or item.get("picking_name")
            or item.get("tracking_number")
            or item.get("primary_picking_name")
            or item.get("primary_tracking_number")
            or item.get("preferred_key")
            or item.get("payload_item_id")
            or "-"
        )
        matched_pickings.append(
            {
                "picking_name": label,
                "odoo_picking_id": item.get("odoo_picking_id"),
                "document_count": item.get("document_count", len(item.get("documents", []))),
                "primary_document_uid": item.get("primary_document_uid"),
                "primary_file_url": primary_document.get("file_url"),
                "sources": sorted({doc["source_name"] for doc in item.get("documents", [])}),
                "preferred_key_type": item.get("preferred_key_type"),
                "preferred_key": item.get("preferred_key"),
                "primary_picking_name": item.get("primary_picking_name"),
                "primary_tracking_number": item.get("primary_tracking_number"),
            }
        )
    matched_pickings.sort(key=lambda row: (-row["document_count"], row["picking_name"]))

    artifacts = []
    for key, absolute_path in summary.get("artifacts", {}).items():
        path = Path(absolute_path)
        artifacts.append(
            {
                "key": key,
                "label": DASHBOARD_ARTIFACT_LABELS.get(key, key.replace("_", " ")),
                "file_name": path.name,
                "href": path.name,
            }
        )

    return {
        "job_id": summary["job_id"],
        "run_name": summary.get("run_name"),
        "generated_at": summary["generated_at"],
        "output_dir": summary["output_dir"],
        "processing_mode": processing_mode,
        "totals": {
            "documents": summary["document_count"],
            "matched_documents": summary["matched_document_count"],
            "unmatched_documents": summary["unmatched_document_count"],
            "matched_pickings": summary["matched_picking_count"],
            "sources": summary["source_count"],
            "missing_manifest_sources": sum(1 for row in source_cards if row["status"] == "missing_manifest"),
        },
        "artifacts": artifacts,
        "config": {
            "odoo_scope_path": summary.get("odoo_scope_path"),
            "sync_config_path": summary.get("sync_config_path"),
        },
        "ui": ui,
        "status_labels": status_labels,
        "reason_labels": reason_labels,
        "reason_message_labels": reason_message_labels,
        "sources": source_cards,
        "unmatched_reason_counts": [{"name": name, "count": count} for name, count in reason_counts.most_common()],
        "parse_note_counts": [{"name": name, "count": count} for name, count in parse_note_counts.most_common()],
        "matched_pickings": matched_pickings,
        "recent_documents": recent_documents,
        "matched_documents": matched_rows,
        "unmatched_documents": unmatched_rows,
    }


def write_dashboard_artifacts(output_dir, dashboard_payload):
    output_dir = Path(output_dir)
    data_path = output_dir / "phase1a_dashboard_data.json"
    html_path = output_dir / "phase1a_dashboard.html"
    write_json(data_path, dashboard_payload)
    html_path.write_text(build_dashboard_html(dashboard_payload), encoding="utf-8")
    return {
        "dashboard_json": str(data_path.resolve()),
        "dashboard_html": str(html_path.resolve()),
    }


def _source_key(source_name, source_type):
    return f"{source_name}::{source_type}"


def _resolve_storage_root(sync_config_path):
    if not sync_config_path:
        return None
    config_path = Path(sync_config_path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    base_dir = payload.get("download", {}).get("base_dir")
    if not base_dir:
        return None
    base_path = Path(base_dir)
    if base_path.is_absolute():
        return base_path.resolve()
    return (config_path.resolve().parent / base_path).resolve()


def _local_file_context(local_path, storage_root):
    if not local_path:
        return {"local_relative_path": None, "local_file_href": None}
    path = Path(local_path).resolve()
    if storage_root:
        try:
            relative = path.relative_to(storage_root).as_posix()
            return {
                "local_relative_path": relative,
                "local_file_href": f"/storage/files/{quote(relative, safe='/')}",
            }
        except ValueError:
            pass
    return {"local_relative_path": path.name, "local_file_href": None}


def _mode_status_labels(processing_mode):
    if processing_mode == "parse_only":
        return {
            "ready": "可推送",
            "needs_review": "待处理",
            "matched": "已匹配",
            "unmatched": "未匹配",
        }
    return DASHBOARD_STATUS_LABELS


def _mode_reason_labels(processing_mode):
    labels = dict(DASHBOARD_REASON_LABELS)
    if processing_mode == "parse_only":
        labels.update(
            {
                "NO_PICKING_FOUND": "待 Odoo 侧处理",
                "INVALID_PICKING_KEY": "未解析出有效出库单号",
            }
        )
    return labels


def _mode_reason_message_labels(processing_mode):
    labels = dict(DASHBOARD_REASON_MESSAGE_LABELS)
    if processing_mode == "parse_only":
        labels.update(
            {
                "NO_PICKING_FOUND": "当前模式不会在 POD 侧做 Odoo 匹配，这类文档会直接交给 Odoo 侧处理。",
            }
        )
    return labels


def _dashboard_ui(processing_mode):
    if processing_mode == "parse_only":
        return {
            "title": "POD 识别巡检看板",
            "hero_title": "POD 识别巡检看板",
            "intro_lede": "用于检查本次 POD 文档解析结果、来源健康度、异常原因分布，以及准备直接推送到 Odoo 的清洗后数据。当前规则是 `EMTC` 提取 tracking number，`CMC` 会把 `EUxxxxx` 还原为 `WCB2B/OUT/xxxxx`，POD 侧不做 Odoo 匹配。",
            "hint_pills": ["EMTC 取 tracking number", "CMC 还原出库单号", "先看来源健康度", "再看待处理原因", "最后确认待推送数据"],
            "reason_title": "待处理原因",
            "reason_desc": "集中查看无法直接推送的原因和文件解析提示，方便快速定位问题来源。",
            "table_desc": "按状态、来源和待处理原因筛选文档；支持按文件名、匹配键和路径搜索。",
            "table_col_target": "匹配键 / 原因",
            "summary_labels": ["文档总数", "可推送文档", "待处理文档", "Payload 条数", "来源数", "缺少 Manifest"],
            "source_labels": {"matched": "可推送", "unmatched": "待处理"},
            "artifact_subtitle": "打开本次运行生成的报告、Payload 和巡检文件。",
        }
    return {
        "title": "POD 识别巡检看板",
        "hero_title": "Phase 1A 巡检看板",
        "intro_lede": "用于检查本次 Phase 1A 的文档匹配结果、来源健康度、异常原因分布，以及准备推送到 Odoo 的 payload 分组。当前规则是 `EMTC` 按 tracking number 匹配，`CMC` 会把 `EUxxxxx` 还原为 `WCB2B/OUT/xxxxx` 后匹配 Odoo。",
        "hint_pills": ["EMTC 取 tracking number", "CMC 还原出库单号", "先看来源健康度", "再看未匹配原因", "最后确认推送分组"],
        "reason_title": "未匹配原因",
        "reason_desc": "集中查看未匹配原因和文件解析提示，方便快速定位问题来源。",
        "table_desc": "按状态、来源和未匹配原因筛选文档；支持按文件名、匹配键和出库单号搜索。",
        "table_col_target": "出库单 / 原因",
        "summary_labels": ["文档总数", "已匹配文档", "未匹配文档", "已匹配出库单", "来源数", "缺少 Manifest"],
        "source_labels": {"matched": "已匹配", "unmatched": "未匹配"},
        "artifact_subtitle": "打开本次运行生成的报告、Payload 和巡检文件。",
    }


def build_dashboard_html(payload):
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    ui = payload.get("ui") or _dashboard_ui(payload.get("processing_mode") or "scope_match")
    badge_classes = json.dumps(
        {
            "matched": "badge-success",
            "unmatched": "badge-warn",
            "ready": "badge-success",
            "needs_review": "badge-warn",
            "ok": "badge-success",
            "missing_manifest": "badge-neutral",
        },
        ensure_ascii=False,
    )
    status_labels = json.dumps(payload.get("status_labels") or DASHBOARD_STATUS_LABELS, ensure_ascii=False)
    source_status_labels = json.dumps(DASHBOARD_SOURCE_STATUS_LABELS, ensure_ascii=False)
    source_type_labels = json.dumps(DASHBOARD_SOURCE_TYPE_LABELS, ensure_ascii=False)
    match_key_labels = json.dumps(DASHBOARD_MATCH_KEY_LABELS, ensure_ascii=False)
    reason_labels = json.dumps(payload.get("reason_labels") or DASHBOARD_REASON_LABELS, ensure_ascii=False)
    reason_message_labels = json.dumps(payload.get("reason_message_labels") or DASHBOARD_REASON_MESSAGE_LABELS, ensure_ascii=False)
    parse_note_labels = json.dumps(DASHBOARD_PARSE_NOTE_LABELS, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{ui["title"]}</title>
  <style>
    :root {{
      --bg: #f8f9fa;
      --panel: #ffffff;
      --panel-soft: #fbfbfc;
      --ink: #212529;
      --muted: #6c757d;
      --line: #dee2e6;
      --line-strong: #ced4da;
      --topbar: #212529;
      --topbar-ink: #f8f9fa;
      --accent: #0d6efd;
      --accent-soft: #e7f1ff;
      --success-soft: #d1e7dd;
      --warn: #fd7e14;
      --warn-soft: #ffe5d0;
      --shadow-sm: 0 1px 2px rgba(33, 37, 41, 0.06);
      --shadow-md: 0 10px 24px rgba(33, 37, 41, 0.08);
      --radius-sm: 8px;
      --radius: 12px;
      --radius-lg: 16px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      min-height: 100%;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    body {{ padding: 0 0 40px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code, .mono {{ font-family: "SFMono-Regular", Consolas, monospace; }}
    .shell {{ width: min(1380px, calc(100% - 32px)); margin: 0 auto; }}
    .topbar {{
      background: var(--topbar);
      color: var(--topbar-ink);
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
    }}
    .topbar-inner {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      padding: 18px 0;
    }}
    .eyebrow {{
      margin: 0;
      font-size: 0.76rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
      color: rgba(248, 249, 250, 0.72);
    }}
    .topbar h1 {{
      margin: 4px 0 0;
      font-size: 1.38rem;
      color: var(--topbar-ink);
    }}
    .meta-row, .artifact-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .meta-pill, .artifact-link {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      background: var(--panel-soft);
      color: var(--ink);
      font-size: 0.88rem;
    }}
    .artifact-link {{
      align-items: flex-start;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .topbar .meta-pill {{
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.16);
      color: var(--topbar-ink);
    }}
    .intro-panel {{
      margin-top: 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      padding: 20px 22px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-sm);
    }}
    .lede {{
      margin: 8px 0 0;
      max-width: 860px;
      color: var(--muted);
      line-height: 1.6;
    }}
    .hint-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .hint-pill {{
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .card-grid {{
      display: grid;
      gap: 16px;
      margin-top: 20px;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    }}
    .workspace {{
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 18px;
      margin-top: 20px;
      align-items: start;
    }}
    .sidebar-stack,
    .main-stack {{
      display: grid;
      gap: 18px;
    }}
    .workspace .section {{
      margin-top: 0;
    }}
    .sidebar-col {{
      position: sticky;
      top: 18px;
      align-self: start;
    }}
    .card, .source-card, .stack, .payload-card, .table-wrap, .toolbar {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }}
    .card {{
      padding: 20px;
      position: relative;
      overflow: hidden;
    }}
    .card::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: var(--accent);
    }}
    .card .label {{
      margin: 0;
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .card .value {{
      margin-top: 10px;
      font-size: clamp(1.8rem, 2vw, 2.4rem);
      font-weight: 700;
      line-height: 1;
    }}
    .section {{
      margin-top: 20px;
      display: grid;
      gap: 14px;
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 12px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 1.28rem;
    }}
    .section-head p {{
      margin: 4px 0 0;
      color: var(--muted);
    }}
    .toolbar {{
      display: grid;
      gap: 12px;
      grid-template-columns: 1.6fr 1fr 1fr 1fr;
      padding: 18px;
    }}
    .toolbar label {{
      display: grid;
      gap: 6px;
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--ink);
    }}
    .toolbar input,
    .toolbar select {{
      width: 100%;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-sm);
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    .source-grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1fr;
    }}
    .source-card {{
      padding: 18px;
      transition: box-shadow 0.2s ease, transform 0.2s ease;
    }}
    .source-card:hover, .payload-card:hover {{
      box-shadow: var(--shadow-md);
      transform: translateY(-1px);
    }}
    .source-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }}
    .source-card h3, .payload-card h3, .stack h3 {{
      margin: 0;
      font-size: 1.05rem;
    }}
    .source-card code {{
      color: var(--muted);
      font-size: 0.82rem;
      word-break: break-all;
    }}
    .source-stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .mini {{
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel-soft);
    }}
    .mini .k {{
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .mini .v {{
      margin-top: 6px;
      font-size: 1.18rem;
      font-weight: 700;
    }}
    .reason-grid, .payload-grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    .artifact-strip {{
      display: grid;
      gap: 10px;
    }}
    .artifact-link {{
      justify-content: space-between;
      width: 100%;
    }}
    .stack, .payload-card {{
      padding: 18px;
    }}
    .bars {{
      display: grid;
      gap: 10px;
    }}
    .bar-row {{
      display: grid;
      gap: 6px;
    }}
    .bar-label {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .bar {{
      height: 10px;
      border-radius: 999px;
      background: #e9ecef;
      overflow: hidden;
    }}
    .bar > span {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #0d6efd, #6ea8fe);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 0.78rem;
      font-weight: 700;
    }}
    .badge-success {{
      background: var(--success-soft);
      border-color: #badbcc;
      color: #0f5132;
    }}
    .badge-warn {{
      background: var(--warn-soft);
      border-color: #ffcf9e;
      color: #8a4b08;
    }}
    .badge-neutral {{
      background: #e9ecef;
      border-color: #dee2e6;
      color: #495057;
    }}
    .muted {{
      color: var(--muted);
    }}
    .payload-card .meta {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .table-wrap {{
      overflow: hidden;
    }}
    .table-shell {{
      display: grid;
      gap: 14px;
    }}
    .table-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 0 4px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .pager-controls {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .pager-controls label {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 600;
    }}
    .pager-controls select {{
      min-height: 38px;
      padding: 8px 10px;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-sm);
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    .pager-button {{
      min-height: 38px;
      padding: 8px 12px;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-sm);
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    .pager-button:hover:not(:disabled) {{
      background: #f8f9fa;
    }}
    .pager-button:disabled {{
      cursor: not-allowed;
      opacity: 0.55;
    }}
    .page-indicator {{
      color: var(--ink);
      font-weight: 600;
      white-space: nowrap;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}
    th, td {{
      text-align: left;
      padding: 14px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    th {{
      background: #f8f9fa;
      color: var(--muted);
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    tbody tr:hover {{
      background: #f8fbff;
    }}
    .empty {{
      padding: 18px;
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius-sm);
      background: var(--panel-soft);
      color: var(--muted);
    }}
    .wrap-anywhere {{
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    @media (max-width: 1120px) {{
      .workspace {{
        grid-template-columns: 1fr;
      }}
      .sidebar-col {{
        position: static;
      }}
      .source-grid {{
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      }}
    }}
	    @media (max-width: 980px) {{
	      .topbar-inner, .intro-panel, .source-top, .section-head {{
	        flex-direction: column;
	        align-items: stretch;
	      }}
	      .toolbar {{
	        grid-template-columns: 1fr;
	      }}
        .table-meta {{
          flex-direction: column;
          align-items: stretch;
        }}
        .pager-controls {{
          justify-content: space-between;
        }}
	      th:nth-child(5), td:nth-child(5) {{
	        display: none;
	      }}
	    }}
    @media (max-width: 720px) {{
      .shell {{
        width: min(100% - 20px, 1380px);
      }}
      .intro-panel, .card, .stack, .payload-card, .source-card {{
        padding: 16px;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="shell topbar-inner">
      <div>
        <p class="eyebrow">TENWAYS POD 运维台</p>
        <h1>{ui["hero_title"]}</h1>
      </div>
      <div class="meta-row" id="heroMeta"></div>
    </div>
  </header>

  <div class="shell">
    <section class="intro-panel">
      <div>
        <p class="eyebrow" style="color:#6c757d;">巡检说明</p>
        <p class="lede">{ui["intro_lede"]}</p>
      </div>
      <div class="hint-row">
        {"".join(f'<span class="hint-pill">{item}</span>' for item in ui["hint_pills"])}
      </div>
    </section>

    <section class="card-grid" id="summaryCards"></section>

    <div class="workspace">
      <aside class="sidebar-col">
        <div class="sidebar-stack">
          <section class="section">
            <div class="section-head">
              <div>
                <h2>产物文件</h2>
                <p>{ui["artifact_subtitle"]}</p>
              </div>
            </div>
            <div class="artifact-strip" id="artifactStrip"></div>
          </section>

          <section class="section">
            <div class="section-head">
              <div>
                <h2>来源健康度</h2>
                <p>按来源查看 Manifest 可见性、文档量、解析问题和文件链接完整性。</p>
              </div>
            </div>
            <div class="source-grid" id="sourceGrid"></div>
          </section>

          <section class="section">
            <div class="section-head">
              <div>
                <h2>异常分布</h2>
                <p>{ui["reason_desc"]}</p>
              </div>
            </div>
            <div class="reason-grid">
              <div class="stack">
                <h3>{ui["reason_title"]}</h3>
                <div class="bars" id="unmatchedReasonBars"></div>
              </div>
              <div class="stack">
                <h3>解析提示</h3>
                <div class="bars" id="parseNoteBars"></div>
              </div>
            </div>
          </section>
        </div>
      </aside>

      <main class="main-col">
        <div class="main-stack">
	          <section class="section">
	            <div class="section-head">
	              <div>
	                <h2>文档巡检表</h2>
                <p>{ui["table_desc"]}</p>
              </div>
            </div>
            <div class="table-shell">
              <div class="toolbar">
                <label>
                  搜索
                  <input id="searchInput" type="search" placeholder="EU22910、WHOUT15504、3SHOHR...">
                </label>
                <label>
                  状态
                  <select id="statusFilter"></select>
                </label>
                <label>
                  来源
                  <select id="sourceFilter"></select>
                </label>
                <label>
                  原因
                  <select id="reasonFilter"></select>
                </label>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
	                    <tr>
	                      <th>文档</th>
	                      <th>状态</th>
	                      <th>来源</th>
	                      <th>{ui["table_col_target"]}</th>
	                      <th>路径与链接</th>
	                    </tr>
                  </thead>
                  <tbody id="documentTableBody"></tbody>
                </table>
              </div>
              <div class="table-meta">
                <div id="documentTableMeta">等待加载文档列表。</div>
                <div class="pager-controls">
                  <label>
                    每页条数
                    <select id="pageSizeSelect">
                      <option value="25" selected>25</option>
                      <option value="50">50</option>
                      <option value="100">100</option>
                    </select>
                  </label>
                  <button id="documentPrevPage" type="button" class="pager-button">上一页</button>
                  <span id="documentPageIndicator" class="page-indicator">第 1 / 1 页</span>
                  <button id="documentNextPage" type="button" class="pager-button">下一页</button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>

  <script id="dashboard-data" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("dashboard-data").textContent);
    const badgeClasses = {badge_classes};
    const statusLabels = {status_labels};
    const sourceStatusLabels = {source_status_labels};
    const sourceTypeLabels = {source_type_labels};
    const matchKeyLabels = {match_key_labels};
    const reasonLabels = {reason_labels};
    const reasonMessageLabels = {reason_message_labels};
    const parseNoteLabels = {parse_note_labels};

    function badgeClass(name) {{
      return badgeClasses[name] || "badge-neutral";
    }}

    function labelFrom(map, value) {{
      if (!value) return "-";
      return map[value] || value;
    }}

    function statusLabel(value) {{
      return labelFrom(statusLabels, value);
    }}

    function sourceStatusLabel(value) {{
      return labelFrom(sourceStatusLabels, value);
    }}

    function sourceTypeLabel(value) {{
      return labelFrom(sourceTypeLabels, value);
    }}

    function matchKeyLabel(value) {{
      return labelFrom(matchKeyLabels, value);
    }}

    function reasonLabel(value) {{
      return labelFrom(reasonLabels, value);
    }}

    function reasonMessage(value, fallback) {{
      if (value && reasonMessageLabels[value]) return reasonMessageLabels[value];
      return fallback || "-";
    }}

    function parseNoteLabel(value) {{
      return labelFrom(parseNoteLabels, value);
    }}

    function formatNumber(value) {{
      if (value === null || value === undefined || value === "") return "-";
      return Number(value).toLocaleString("zh-CN");
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    function renderHero() {{
      const meta = [
        `任务：${{escapeHtml(payload.job_id)}}`,
        `生成时间：${{escapeHtml(payload.generated_at)}}`,
        `输出目录：${{escapeHtml(payload.output_dir)}}`,
        payload.config.odoo_scope_path ? `Odoo Scope：${{escapeHtml(payload.config.odoo_scope_path)}}` : null,
        payload.config.sync_config_path ? `同步配置：${{escapeHtml(payload.config.sync_config_path)}}` : null,
      ].filter(Boolean);
      document.getElementById("heroMeta").innerHTML = meta.map((item) => `<div class="meta-pill">${{item}}</div>`).join("");
    }}

    function renderSummaryCards() {{
      const labels = payload.ui.summary_labels || ["文档总数", "已匹配文档", "未匹配文档", "已匹配出库单", "来源数", "缺少 Manifest"];
      const cards = [
        [labels[0], payload.totals.documents],
        [labels[1], payload.totals.matched_documents],
        [labels[2], payload.totals.unmatched_documents],
        [labels[3], payload.totals.matched_pickings],
        [labels[4], payload.totals.sources],
        [labels[5], payload.totals.missing_manifest_sources],
      ];
      document.getElementById("summaryCards").innerHTML = cards.map(([label, value]) => `
        <article class="card">
          <div class="label">${{escapeHtml(label)}}</div>
          <div class="value">${{formatNumber(value)}}</div>
        </article>
      `).join("");
    }}

    function renderArtifacts() {{
      const root = document.getElementById("artifactStrip");
      if (!payload.artifacts.length) {{
        root.innerHTML = `<div class="empty">当前运行没有产物文件。</div>`;
        return;
      }}
      const artifactHref = (item) => {{
        if (window.location.protocol !== "file:" && payload.run_name && window.location.pathname.includes("/phase1a/runs/")) {{
          return `/phase1a/runs/${{encodeURIComponent(payload.run_name)}}/artifacts/${{encodeURIComponent(item.key)}}`;
        }}
        return item.href || item.file_name || "#";
      }};
      root.innerHTML = payload.artifacts.map((item) => `
        <a class="artifact-link" href="${{escapeHtml(artifactHref(item))}}" target="_blank" rel="noreferrer">${{escapeHtml(item.label)}} · ${{escapeHtml(item.file_name)}}</a>
      `).join("");
    }}

    function renderSources() {{
      const root = document.getElementById("sourceGrid");
      if (!payload.sources.length) {{
        root.innerHTML = `<div class="empty">当前没有来源记录。</div>`;
        return;
      }}
      root.innerHTML = payload.sources.map((source) => `
        <article class="source-card">
          <div class="source-top">
            <div>
              <h3>${{escapeHtml(source.source_name)}}</h3>
              <div class="muted">${{escapeHtml(sourceTypeLabel(source.source_type))}} · <span class="badge ${{badgeClass(source.status)}}">${{escapeHtml(sourceStatusLabel(source.status))}}</span></div>
            </div>
            <code>${{escapeHtml(source.manifest_path)}}</code>
          </div>
          <div class="source-stats">
            <div class="mini"><div class="k">Manifest 记录</div><div class="v">${{formatNumber(source.record_count)}}</div></div>
            <div class="mini"><div class="k">唯一文档</div><div class="v">${{formatNumber(source.unique_record_count || source.total_documents)}}</div></div>
            <div class="mini"><div class="k">重复记录</div><div class="v">${{formatNumber(source.duplicate_record_count || 0)}}</div></div>
            <div class="mini"><div class="k">文档总数</div><div class="v">${{formatNumber(source.total_documents)}}</div></div>
            <div class="mini"><div class="k">${{escapeHtml(payload.ui.source_labels.matched)}}</div><div class="v">${{formatNumber(source.matched_documents)}}</div></div>
            <div class="mini"><div class="k">${{escapeHtml(payload.ui.source_labels.unmatched)}}</div><div class="v">${{formatNumber(source.unmatched_documents)}}</div></div>
            <div class="mini"><div class="k">解析提示</div><div class="v">${{formatNumber(source.parse_issue_documents)}}</div></div>
            <div class="mini"><div class="k">缺少链接</div><div class="v">${{formatNumber(source.missing_url_documents)}}</div></div>
          </div>
        </article>
      `).join("");
    }}

    function renderBars(rootId, items, labelFn) {{
      const root = document.getElementById(rootId);
      if (!items.length) {{
        root.innerHTML = `<div class="empty">这一项当前没有记录。</div>`;
        return;
      }}
      const max = Math.max(...items.map((item) => item.count || 0), 1);
      root.innerHTML = items.map((item) => `
        <div class="bar-row">
          <div class="bar-label"><span>${{escapeHtml(labelFn(item.name))}}</span><strong>${{formatNumber(item.count)}}</strong></div>
          <div class="bar"><span style="width:${{(item.count / max) * 100}}%"></span></div>
        </div>
      `).join("");
    }}

	    function buildFilterOptions(values, placeholder, labelFn) {{
	      return [`<option value="">${{escapeHtml(placeholder)}}</option>`]
	        .concat(values.map((value) => `<option value="${{escapeHtml(value)}}">${{escapeHtml(labelFn(value))}}</option>`))
        .join("");
    }}

    function initFilters() {{
      const statusValues = [...new Set(payload.recent_documents.map((item) => item.match_status).filter(Boolean))].sort();
      const sourceValues = [...new Set(payload.recent_documents.map((item) => item.source_name).filter(Boolean))].sort();
      const reasonValues = [...new Set(payload.recent_documents.map((item) => item.reason_code).filter(Boolean))].sort();
      document.getElementById("statusFilter").innerHTML = buildFilterOptions(statusValues, "全部状态", statusLabel);
      document.getElementById("sourceFilter").innerHTML = buildFilterOptions(sourceValues, "全部来源", (value) => value);
      document.getElementById("reasonFilter").innerHTML = buildFilterOptions(reasonValues, "全部原因", reasonLabel);
      ["searchInput", "statusFilter", "sourceFilter", "reasonFilter"].forEach((id) => {{
        document.getElementById(id).addEventListener("input", () => {{
          currentPage = 1;
          renderDocuments();
        }});
        document.getElementById(id).addEventListener("change", () => {{
          currentPage = 1;
          renderDocuments();
        }});
      }});
      document.getElementById("pageSizeSelect").addEventListener("change", () => {{
        currentPage = 1;
        renderDocuments();
      }});
      document.getElementById("documentPrevPage").addEventListener("click", () => {{
        if (currentPage > 1) {{
          currentPage -= 1;
          renderDocuments();
        }}
      }});
      document.getElementById("documentNextPage").addEventListener("click", () => {{
        currentPage += 1;
        renderDocuments();
      }});
    }}

    let currentPage = 1;

    function renderPager(totalRows, totalPages, pageStart, pageEnd) {{
      const meta = document.getElementById("documentTableMeta");
      const indicator = document.getElementById("documentPageIndicator");
      const prevButton = document.getElementById("documentPrevPage");
      const nextButton = document.getElementById("documentNextPage");
      if (!totalRows) {{
        meta.textContent = "当前筛选没有命中文档。";
        indicator.textContent = "第 0 / 0 页";
        prevButton.disabled = true;
        nextButton.disabled = true;
        return;
      }}
      meta.textContent = `当前筛选命中 ${{formatNumber(totalRows)}} 条，显示第 ${{formatNumber(pageStart)}} - ${{formatNumber(pageEnd)}} 条`;
      indicator.textContent = `第 ${{formatNumber(currentPage)}} / ${{formatNumber(totalPages)}} 页`;
      prevButton.disabled = currentPage <= 1;
      nextButton.disabled = currentPage >= totalPages;
    }}

    function renderDocuments() {{
      const query = document.getElementById("searchInput").value.trim().toLowerCase();
      const status = document.getElementById("statusFilter").value;
      const source = document.getElementById("sourceFilter").value;
      const reason = document.getElementById("reasonFilter").value;
      const pageSize = Number(document.getElementById("pageSizeSelect").value || 25);

	      const rows = payload.recent_documents.filter((item) => {{
        const haystack = [
          item.file_name,
          item.document_uid,
          item.source_path,
          item.local_path,
          item.selected_odoo_picking_ref,
          item.reason_code,
          item.match_key,
          item.primary_picking_name,
          item.primary_tracking_number,
        ].filter(Boolean).join(" ").toLowerCase();

        if (query && !haystack.includes(query)) return false;
        if (status && item.match_status !== status) return false;
        if (source && item.source_name !== source) return false;
        if (reason && (item.reason_code || "") !== reason) return false;
        return true;
      }});

	      const body = document.getElementById("documentTableBody");
	      if (!rows.length) {{
          renderPager(0, 0, 0, 0);
	        body.innerHTML = `<tr><td colspan="5"><div class="empty">没有文档符合当前筛选条件。</div></td></tr>`;
	        return;
	      }}

        const totalPages = Math.max(Math.ceil(rows.length / pageSize), 1);
        currentPage = Math.min(Math.max(currentPage, 1), totalPages);
        const startIndex = (currentPage - 1) * pageSize;
        const pageRows = rows.slice(startIndex, startIndex + pageSize);
        renderPager(rows.length, totalPages, startIndex + 1, startIndex + pageRows.length);

	      body.innerHTML = pageRows.map((item) => {{
	        const noteText = (item.parse_notes || []).map((note) => parseNoteLabel(note)).join("、");
	        const primaryLabel = item.primary_picking_name || item.primary_tracking_number || item.selected_odoo_picking_ref;
	        const keyTypeLabel = item.match_key ? matchKeyLabel(item.match_key_type || "-") : "";
	        const pickingOrReason = ["matched", "ready"].includes(item.match_status)
	          ? `<div class="mono wrap-anywhere">${{escapeHtml(primaryLabel || "-")}}</div>${{keyTypeLabel ? `<div class="muted">${{escapeHtml(keyTypeLabel)}}</div>` : ""}}`
	          : `<div>${{escapeHtml(reasonLabel(item.reason_code || "-"))}}</div><div class="muted">${{escapeHtml(reasonMessage(item.reason_code, item.reason_message))}}</div>${{item.match_key ? `<div class="muted">键：${{escapeHtml(keyTypeLabel)}} · <span class="mono wrap-anywhere">${{escapeHtml(item.match_key)}}</span></div>` : ""}}`;
	        const linkParts = [];
	        if (item.local_file_href) {{
	          linkParts.push(`<a href="${{escapeHtml(item.local_file_href)}}" target="_blank" rel="noreferrer">打开本地文件</a>`);
	        }}
	        if (item.file_url) {{
	          linkParts.push(`<a href="${{escapeHtml(item.file_url)}}" target="_blank" rel="noreferrer">打开源文件</a>`);
	        }}
	        const sourceLinks = linkParts.length ? linkParts.join(' · ') : '<span class="muted">无可用链接</span>';
	        const compactPath = item.local_relative_path || item.source_path || "-";
	        return `
	          <tr>
	            <td>
	              <div><strong class="wrap-anywhere">${{escapeHtml(item.file_name)}}</strong></div>
	              <div class="muted">${{escapeHtml(item.source_modified_at || "-")}}</div>
	              ${{noteText ? `<div class="muted">解析提示：${{escapeHtml(noteText)}}</div>` : ""}}
	            </td>
	            <td><span class="badge ${{badgeClass(item.match_status)}}">${{escapeHtml(statusLabel(item.match_status || "-"))}}</span></td>
	            <td>
	              <div>${{escapeHtml(item.source_name)}}</div>
	              <div class="muted">${{escapeHtml(sourceTypeLabel(item.source_type))}}</div>
	            </td>
	            <td>${{pickingOrReason}}</td>
	            <td>
	              <div class="mono wrap-anywhere">${{escapeHtml(compactPath)}}</div>
	              <div>${{sourceLinks}}</div>
	            </td>
	          </tr>
	        `;
	      }}).join("");
    }}

    renderHero();
    renderSummaryCards();
	    renderArtifacts();
	    renderSources();
	    renderBars("unmatchedReasonBars", payload.unmatched_reason_counts, reasonLabel);
	    renderBars("parseNoteBars", payload.parse_note_counts, parseNoteLabel);
	    initFilters();
	    renderDocuments();
	  </script>
</body>
</html>
"""
