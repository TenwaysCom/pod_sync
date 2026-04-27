import json
from collections import Counter, defaultdict
from pathlib import Path

from .io_utils import write_json


def build_dashboard_payload(*, summary, source_statuses, parsed_documents, matched_rows, unmatched_rows, payload):
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
        source_totals[source_key]["total_documents"] += 1
        if document.get("match_status") == "matched":
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
                "file_url": document.get("file_url"),
                "local_path": document.get("local_path"),
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
        matched_pickings.append(
            {
                "picking_name": item["picking_name"],
                "odoo_picking_id": item.get("odoo_picking_id"),
                "document_count": item["document_count"],
                "primary_document_uid": item["primary_document_uid"],
                "primary_file_url": item["documents"][0].get("file_url") if item.get("documents") else None,
                "sources": sorted({doc["source_name"] for doc in item.get("documents", [])}),
            }
        )
    matched_pickings.sort(key=lambda row: (-row["document_count"], row["picking_name"]))

    artifacts = []
    for key, absolute_path in summary.get("artifacts", {}).items():
        path = Path(absolute_path)
        artifacts.append(
            {
                "key": key,
                "label": key.replace("_", " "),
                "file_name": path.name,
                "href": path.name,
            }
        )

    return {
        "job_id": summary["job_id"],
        "generated_at": summary["generated_at"],
        "output_dir": summary["output_dir"],
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
        "sources": source_cards,
        "unmatched_reason_counts": [{"name": name, "count": count} for name, count in reason_counts.most_common()],
        "parse_note_counts": [{"name": name, "count": count} for name, count in parse_note_counts.most_common()],
        "matched_pickings": matched_pickings,
        "recent_documents": recent_documents[:200],
        "matched_documents": matched_rows[:200],
        "unmatched_documents": unmatched_rows[:200],
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


def build_dashboard_html(payload):
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    badge_classes = json.dumps(
        {
            "matched": "badge-success",
            "unmatched": "badge-warn",
            "ok": "badge-success",
            "missing_manifest": "badge-neutral",
        },
        ensure_ascii=False,
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>POD Phase 1A Inspection Deck</title>
  <style>
    :root {{
      --bg: #f4efe4;
      --bg-strong: #efe2c8;
      --ink: #1f2a26;
      --muted: #5f6c67;
      --card: rgba(255, 252, 247, 0.86);
      --line: rgba(31, 42, 38, 0.12);
      --accent: #174b3b;
      --accent-soft: #d8efe1;
      --warn: #9b4c22;
      --warn-soft: #f7dccd;
      --shadow: 0 24px 70px rgba(38, 44, 36, 0.12);
      --radius: 24px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      min-height: 100%;
      background: radial-gradient(circle at top left, rgba(255,255,255,0.75), transparent 34%),
        linear-gradient(180deg, #f7f0e4 0%, #f0e8d8 48%, #ebe0cc 100%);
      color: var(--ink);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }}
    body {{ padding: 32px 20px 56px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1380px; margin: 0 auto; }}
    .hero {{
      display: grid;
      gap: 18px;
      padding: 28px;
      border: 1px solid rgba(23, 75, 59, 0.12);
      background: linear-gradient(135deg, rgba(255,255,255,0.88), rgba(231, 245, 236, 0.82));
      border-radius: 30px;
      box-shadow: var(--shadow);
      animation: rise .6s ease-out both;
    }}
    .eyebrow {{
      font-size: 12px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--accent);
      font-weight: 700;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(2.3rem, 4vw, 4.4rem);
      line-height: 0.95;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      font-weight: 700;
    }}
    .hero p {{ margin: 0; max-width: 860px; color: var(--muted); font-size: 1.02rem; }}
    .meta-row, .card-grid, .source-grid, .reason-grid, .payload-grid, .table-wrap {{ animation: rise .7s ease-out both; }}
    .meta-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}
    .meta-pill, .artifact-link {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .card-grid {{
      display: grid;
      gap: 16px;
      margin-top: 22px;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    }}
    .card {{
      padding: 20px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }}
    .card .label {{ font-size: 0.83rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
    .card .value {{
      margin-top: 8px;
      font-size: clamp(2rem, 2vw, 2.6rem);
      font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
      line-height: 1;
      font-weight: 700;
    }}
    .section {{ margin-top: 22px; display: grid; gap: 14px; }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 1.55rem;
      font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
    }}
    .section-head p {{ margin: 4px 0 0; color: var(--muted); }}
    .toolbar {{
      display: grid;
      gap: 12px;
      grid-template-columns: 1.6fr 1fr 1fr 1fr;
      padding: 18px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }}
    .toolbar label {{
      display: grid;
      gap: 8px;
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .toolbar input, .toolbar select {{
      width: 100%;
      border: 1px solid rgba(23,75,59,0.16);
      border-radius: 14px;
      background: rgba(255,255,255,0.88);
      padding: 12px 14px;
      font-size: 0.98rem;
      color: var(--ink);
    }}
    .source-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }}
    .source-card {{
      padding: 18px;
      border-radius: var(--radius);
      background: var(--card);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .source-card::after {{
      content: "";
      position: absolute;
      inset: auto -20% -45% auto;
      width: 160px;
      height: 160px;
      background: radial-gradient(circle, rgba(23,75,59,0.15), transparent 68%);
      pointer-events: none;
    }}
    .source-top {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
      position: relative;
      z-index: 1;
    }}
    .source-card h3 {{ margin: 0; font-size: 1.15rem; }}
    .source-card code {{ color: var(--muted); font-size: 0.86rem; word-break: break-all; }}
    .source-stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
      position: relative;
      z-index: 1;
    }}
    .mini {{
      padding: 12px;
      border-radius: 16px;
      background: rgba(255,255,255,0.74);
      border: 1px solid rgba(23,75,59,0.08);
    }}
    .mini .k {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
    .mini .v {{ margin-top: 6px; font-size: 1.2rem; font-weight: 700; }}
    .reason-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .stack {{
      padding: 18px;
      border-radius: var(--radius);
      background: var(--card);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }}
    .stack h3 {{ margin: 0 0 12px; font-size: 1.05rem; }}
    .bars {{ display: grid; gap: 10px; }}
    .bar-row {{ display: grid; gap: 6px; }}
    .bar-label {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    .bar {{ height: 10px; border-radius: 999px; background: rgba(23,75,59,0.08); overflow: hidden; }}
    .bar > span {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #174b3b, #3b7a62);
    }}
    .table-wrap {{
      padding: 18px;
      border-radius: var(--radius);
      background: var(--card);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.94rem; }}
    th, td {{ text-align: left; padding: 14px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
    tbody tr {{ transition: background .18s ease; }}
    tbody tr:hover {{ background: rgba(23,75,59,0.05); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 700;
    }}
    .badge-success {{ background: var(--accent-soft); color: var(--accent); }}
    .badge-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .badge-neutral {{ background: rgba(31,42,38,0.08); color: var(--ink); }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.84rem; word-break: break-all; }}
    .payload-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
    .payload-card {{
      padding: 18px;
      border-radius: var(--radius);
      background: linear-gradient(180deg, rgba(255,255,255,0.94), rgba(244,249,245,0.88));
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }}
    .payload-card h3 {{ margin: 0; font-size: 1.08rem; }}
    .payload-card .meta {{ margin-top: 6px; color: var(--muted); font-size: 0.92rem; }}
    .artifact-strip {{ display: flex; flex-wrap: wrap; gap: 12px; }}
    .empty {{
      padding: 24px;
      border-radius: var(--radius);
      background: rgba(255,255,255,0.75);
      border: 1px dashed var(--line);
      color: var(--muted);
    }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (max-width: 980px) {{
      .toolbar {{ grid-template-columns: 1fr; }}
      body {{ padding: 20px 14px 42px; }}
      .hero, .card, .table-wrap, .stack, .payload-card, .source-card {{ border-radius: 22px; }}
      th:nth-child(5), td:nth-child(5) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">POD Phase 1A</div>
      <h1>Inspection Deck</h1>
      <p>Use this page to inspect current matched and unmatched POD files, source manifest health, payload readiness, and parse anomalies before sending results into Odoo.</p>
      <div class="meta-row" id="heroMeta"></div>
    </section>

    <section class="card-grid" id="summaryCards"></section>

    <section class="section">
      <div class="section-head">
        <div>
          <h2>Artifacts</h2>
          <p>Open the exported reports and payload files from the current run.</p>
        </div>
      </div>
      <div class="artifact-strip" id="artifactStrip"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div>
          <h2>Source Health</h2>
          <p>Manifest visibility, document volume, parse issues, and URL completeness by source.</p>
        </div>
      </div>
      <div class="source-grid" id="sourceGrid"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div>
          <h2>Anomalies</h2>
          <p>Unmatched reason distribution and parser warnings from the current run.</p>
        </div>
      </div>
      <div class="reason-grid">
        <div class="stack">
          <h3>Unmatched Reasons</h3>
          <div class="bars" id="unmatchedReasonBars"></div>
        </div>
        <div class="stack">
          <h3>Parse Notes</h3>
          <div class="bars" id="parseNoteBars"></div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <div>
          <h2>Matched Payload Groups</h2>
          <p>Each card represents one Odoo picking payload item that is ready to push.</p>
        </div>
      </div>
      <div class="payload-grid" id="payloadGrid"></div>
    </section>

    <section class="section">
      <div class="section-head">
        <div>
          <h2>Document Explorer</h2>
          <p>Filter documents by status, source, and unmatched reason. Search by file name, match key, or picking reference.</p>
        </div>
      </div>
      <div class="toolbar">
        <label>
          Search
          <input id="searchInput" type="search" placeholder="EU22910, WHOUT15504, 3SHOHR...">
        </label>
        <label>
          Status
          <select id="statusFilter"></select>
        </label>
        <label>
          Source
          <select id="sourceFilter"></select>
        </label>
        <label>
          Reason
          <select id="reasonFilter"></select>
        </label>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Document</th>
              <th>Status</th>
              <th>Source</th>
              <th>Picking / Reason</th>
              <th>Match Key</th>
              <th>Paths</th>
            </tr>
          </thead>
          <tbody id="documentTableBody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <script id="dashboard-data" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("dashboard-data").textContent);
    const badgeClasses = {badge_classes};

    function badgeClass(name) {{
      return badgeClasses[name] || "badge-neutral";
    }}

    function formatNumber(value) {{
      if (value === null || value === undefined || value === "") return "-";
      return Number(value).toLocaleString();
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
        `Job: ${{escapeHtml(payload.job_id)}}`,
        `Generated: ${{escapeHtml(payload.generated_at)}}`,
        `Output: ${{escapeHtml(payload.output_dir)}}`,
        payload.config.odoo_scope_path ? `Odoo scope: ${{escapeHtml(payload.config.odoo_scope_path)}}` : null,
        payload.config.sync_config_path ? `Sync config: ${{escapeHtml(payload.config.sync_config_path)}}` : null,
      ].filter(Boolean);
      document.getElementById("heroMeta").innerHTML = meta.map((item) => `<div class="meta-pill">${{item}}</div>`).join("");
    }}

    function renderSummaryCards() {{
      const cards = [
        ["Documents", payload.totals.documents],
        ["Matched Docs", payload.totals.matched_documents],
        ["Unmatched Docs", payload.totals.unmatched_documents],
        ["Matched Pickings", payload.totals.matched_pickings],
        ["Sources", payload.totals.sources],
        ["Missing Manifests", payload.totals.missing_manifest_sources],
      ];
      document.getElementById("summaryCards").innerHTML = cards.map(([label, value]) => `
        <article class="card">
          <div class="label">${{escapeHtml(label)}}</div>
          <div class="value">${{formatNumber(value)}}</div>
        </article>
      `).join("");
    }}

    function renderArtifacts() {{
      document.getElementById("artifactStrip").innerHTML = payload.artifacts.map((item) => `
        <a class="artifact-link" href="${{escapeHtml(item.href)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.file_name)}}</a>
      `).join("");
    }}

    function renderSources() {{
      const root = document.getElementById("sourceGrid");
      root.innerHTML = payload.sources.map((source) => `
        <article class="source-card">
          <div class="source-top">
            <div>
              <h3>${{escapeHtml(source.source_name)}}</h3>
              <div class="muted">${{escapeHtml(source.source_type)}} • <span class="badge ${{badgeClass(source.status)}}">${{escapeHtml(source.status)}}</span></div>
            </div>
            <code>${{escapeHtml(source.manifest_path)}}</code>
          </div>
          <div class="source-stats">
            <div class="mini"><div class="k">Manifest Rows</div><div class="v">${{formatNumber(source.record_count)}}</div></div>
            <div class="mini"><div class="k">Documents</div><div class="v">${{formatNumber(source.total_documents)}}</div></div>
            <div class="mini"><div class="k">Matched</div><div class="v">${{formatNumber(source.matched_documents)}}</div></div>
            <div class="mini"><div class="k">Unmatched</div><div class="v">${{formatNumber(source.unmatched_documents)}}</div></div>
            <div class="mini"><div class="k">Parse Issues</div><div class="v">${{formatNumber(source.parse_issue_documents)}}</div></div>
            <div class="mini"><div class="k">Missing URL</div><div class="v">${{formatNumber(source.missing_url_documents)}}</div></div>
          </div>
        </article>
      `).join("");
    }}

    function renderBars(rootId, items) {{
      const root = document.getElementById(rootId);
      if (!items.length) {{
        root.innerHTML = `<div class="empty">No records in this section.</div>`;
        return;
      }}
      const max = Math.max(...items.map((item) => item.count || 0), 1);
      root.innerHTML = items.map((item) => `
        <div class="bar-row">
          <div class="bar-label"><span>${{escapeHtml(item.name)}}</span><strong>${{formatNumber(item.count)}}</strong></div>
          <div class="bar"><span style="width:${{(item.count / max) * 100}}%"></span></div>
        </div>
      `).join("");
    }}

    function renderPayloadGrid() {{
      const root = document.getElementById("payloadGrid");
      if (!payload.matched_pickings.length) {{
        root.innerHTML = `<div class="empty">No matched payload groups yet.</div>`;
        return;
      }}
      root.innerHTML = payload.matched_pickings.map((item) => `
        <article class="payload-card">
          <h3>${{escapeHtml(item.picking_name)}}</h3>
          <div class="meta">Odoo ID: ${{escapeHtml(item.odoo_picking_id || "-")}}</div>
          <div class="meta">Documents: ${{formatNumber(item.document_count)}}</div>
          <div class="meta">Sources: ${{escapeHtml(item.sources.join(", "))}}</div>
          <div class="meta mono">Primary UID: ${{escapeHtml(item.primary_document_uid)}}</div>
          ${{item.primary_file_url ? `<div class="meta"><a href="${{escapeHtml(item.primary_file_url)}}" target="_blank" rel="noreferrer">Open primary file</a></div>` : ""}}
        </article>
      `).join("");
    }}

    function buildFilterOptions(values, placeholder) {{
      return [`<option value="">${{escapeHtml(placeholder)}}</option>`]
        .concat(values.map((value) => `<option value="${{escapeHtml(value)}}">${{escapeHtml(value)}}</option>`))
        .join("");
    }}

    function initFilters() {{
      const statusValues = [...new Set(payload.recent_documents.map((item) => item.match_status).filter(Boolean))].sort();
      const sourceValues = [...new Set(payload.recent_documents.map((item) => item.source_name).filter(Boolean))].sort();
      const reasonValues = [...new Set(payload.recent_documents.map((item) => item.reason_code).filter(Boolean))].sort();
      document.getElementById("statusFilter").innerHTML = buildFilterOptions(statusValues, "All statuses");
      document.getElementById("sourceFilter").innerHTML = buildFilterOptions(sourceValues, "All sources");
      document.getElementById("reasonFilter").innerHTML = buildFilterOptions(reasonValues, "All reasons");
      ["searchInput", "statusFilter", "sourceFilter", "reasonFilter"].forEach((id) => {{
        document.getElementById(id).addEventListener("input", renderDocuments);
        document.getElementById(id).addEventListener("change", renderDocuments);
      }});
    }}

    function renderDocuments() {{
      const query = document.getElementById("searchInput").value.trim().toLowerCase();
      const status = document.getElementById("statusFilter").value;
      const source = document.getElementById("sourceFilter").value;
      const reason = document.getElementById("reasonFilter").value;

      const rows = payload.recent_documents.filter((item) => {{
        const haystack = [
          item.file_name,
          item.document_uid,
          item.source_path,
          item.selected_odoo_picking_ref,
          item.reason_code,
          item.match_key,
        ].filter(Boolean).join(" ").toLowerCase();

        if (query && !haystack.includes(query)) return false;
        if (status && item.match_status !== status) return false;
        if (source && item.source_name !== source) return false;
        if (reason && (item.reason_code || "") !== reason) return false;
        return true;
      }});

      const body = document.getElementById("documentTableBody");
      if (!rows.length) {{
        body.innerHTML = `<tr><td colspan="6"><div class="empty">No documents matched the current filter.</div></td></tr>`;
        return;
      }}

      body.innerHTML = rows.map((item) => {{
        const noteText = (item.parse_notes || []).join(", ");
        const pickingOrReason = item.match_status === "matched"
          ? `<div>${{escapeHtml(item.selected_odoo_picking_ref || "-")}}</div>`
          : `<div>${{escapeHtml(item.reason_code || "-")}}</div><div class="muted">${{escapeHtml(item.reason_message || "")}}</div>`;
        const link = item.file_url ? `<a href="${{escapeHtml(item.file_url)}}" target="_blank" rel="noreferrer">Open source</a>` : '<span class="muted">No source URL</span>';
        return `
          <tr>
            <td>
              <div><strong>${{escapeHtml(item.file_name)}}</strong></div>
              <div class="muted">${{escapeHtml(item.source_modified_at || "-")}}</div>
              <div class="mono">${{escapeHtml(item.document_uid)}}</div>
              ${{noteText ? `<div class="muted">Parse: ${{escapeHtml(noteText)}}</div>` : ""}}
            </td>
            <td><span class="badge ${{badgeClass(item.match_status)}}">${{escapeHtml(item.match_status || "-")}}</span></td>
            <td>
              <div>${{escapeHtml(item.source_name)}}</div>
              <div class="muted">${{escapeHtml(item.source_type)}}</div>
            </td>
            <td>${{pickingOrReason}}</td>
            <td>
              <div>${{escapeHtml(item.match_key_type || "-")}}</div>
              <div class="mono">${{escapeHtml(item.match_key || "-")}}</div>
            </td>
            <td>
              <div class="mono">${{escapeHtml(item.source_path)}}</div>
              <div>${{link}}</div>
            </td>
          </tr>
        `;
      }}).join("");
    }}

    renderHero();
    renderSummaryCards();
    renderArtifacts();
    renderSources();
    renderBars("unmatchedReasonBars", payload.unmatched_reason_counts);
    renderBars("parseNoteBars", payload.parse_note_counts);
    renderPayloadGrid();
    initFilters();
    renderDocuments();
  </script>
</body>
</html>
"""
