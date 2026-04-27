from collections import defaultdict
from pathlib import Path

from .dashboard import build_dashboard_payload, write_dashboard_artifacts
from .io_utils import ensure_directory, read_json, read_jsonl, write_csv, write_json
from .matching import build_scope_index, match_document, utc_now
from .parsing import infer_document_fields


MATCHED_REPORT_FIELDS = [
    "job_id",
    "document_uid",
    "source_name",
    "source_type",
    "source_path",
    "file_name",
    "file_url",
    "source_modified_at",
    "inferred_picking_name",
    "inferred_tracking_numbers",
    "match_status",
    "match_key_type",
    "match_key",
    "candidate_count",
    "selected_odoo_picking_ref",
    "selected_odoo_picking_id",
    "document_count_for_picking",
    "is_primary",
    "push_status",
    "push_at",
    "push_error_code",
    "push_error_message",
]

UNMATCHED_REPORT_FIELDS = [
    "job_id",
    "document_uid",
    "source_name",
    "source_type",
    "source_path",
    "file_name",
    "file_url",
    "source_modified_at",
    "inferred_picking_name",
    "inferred_tracking_numbers",
    "match_status",
    "reason_code",
    "reason_message",
    "candidate_count",
    "candidate_picking_refs",
    "last_match_attempt_at",
]

SYNC_SOURCE_DEFINITIONS = (
    {"source_type": "onedrive", "config_keys": ("cmc_onedrive", "onedrive"), "default_label": "CMC"},
    {"source_type": "gdrive", "config_keys": ("emtc_gdrive", "gdrive"), "default_label": "EMTC"},
    {"source_type": "westerman_api", "config_keys": ("westerman_api",), "default_label": "Westerman"},
)


def load_config(path):
    config_path = Path(path).resolve()
    config_dir = config_path.parent
    config = read_json(config_path)
    if config.get("output_dir"):
        config["output_dir"] = str(_resolve_config_path(config["output_dir"], config_dir))
    if config.get("odoo_scope_path"):
        config["odoo_scope_path"] = str(_resolve_config_path(config["odoo_scope_path"], config_dir))
    if config.get("sync_config_path"):
        config["sync_config_path"] = str(_resolve_config_path(config["sync_config_path"], config_dir))
    for source in config.get("sources", []):
        if source.get("manifest_path"):
            source["manifest_path"] = str(_resolve_config_path(source["manifest_path"], config_dir))
    if not config.get("sources") and config.get("sync_config_path"):
        config["sources"] = _build_sources_from_sync_config(config["sync_config_path"])
    return config


def _resolve_config_path(raw_path, config_dir):
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (config_dir / path).resolve()


def _build_sources_from_sync_config(sync_config_path):
    sync_config_path = Path(sync_config_path).resolve()
    sync_config_dir = sync_config_path.parent
    sync_config = read_json(sync_config_path)
    download_cfg = sync_config.get("download", {})
    base_dir = Path(download_cfg.get("base_dir", "downloads"))
    if not base_dir.is_absolute():
        base_dir = (sync_config_dir / base_dir).resolve()
    integrity_root = base_dir / "_integrity"

    sources = []
    for definition in SYNC_SOURCE_DEFINITIONS:
        source_type = definition["source_type"]
        source_cfg = {}
        for key in definition["config_keys"]:
            if key in sync_config:
                source_cfg = sync_config[key]
                break
        if not source_cfg or not source_cfg.get("enabled", True):
            continue
        source_name = source_cfg.get("warehouse") or definition["default_label"]
        sources.append(
            {
                "manifest_path": str((integrity_root / source_name / f"{source_type}.jsonl").resolve()),
                "source_name": source_name,
                "source_type": source_type,
            }
        )
    return sources


def load_documents(config):
    documents = []
    source_statuses = []
    for source in config.get("sources", []):
        manifest_path = Path(source["manifest_path"])
        status = {
            "source_name": source.get("source_name"),
            "source_type": source.get("source_type"),
            "manifest_path": str(manifest_path.resolve()),
            "manifest_exists": manifest_path.exists(),
            "record_count": 0,
            "status": "ok" if manifest_path.exists() else "missing_manifest",
        }
        if not manifest_path.exists():
            source_statuses.append(status)
            continue

        records = read_jsonl(manifest_path)
        status["record_count"] = len(records)
        for record in records:
            documents.append(
                infer_document_fields(
                    record,
                    source_name=source.get("source_name"),
                    source_type=source.get("source_type"),
                )
            )
        source_statuses.append(status)
    return documents, source_statuses


def build_report_row(job_id, doc, decision):
    base = {
        "job_id": job_id,
        "document_uid": doc["document_uid"],
        "source_name": doc["source_name"],
        "source_type": doc["source_type"],
        "source_path": doc["source_path"],
        "file_name": doc["file_name"],
        "file_url": doc["file_url"],
        "source_modified_at": doc["source_modified_at"],
        "inferred_picking_name": ",".join(doc["inferred_picking_names"]),
        "inferred_tracking_numbers": ",".join(doc["inferred_tracking_numbers"]),
        "match_status": decision["match_status"],
    }
    return base


def _document_sort_key(doc):
    return (
        doc.get("source_modified_at") or "",
        doc.get("file_name") or "",
        doc.get("document_uid") or "",
    )


def build_payload_item(picking, documents):
    documents = sorted(documents, key=_document_sort_key, reverse=True)
    primary_document = documents[0]
    payload_documents = []
    for index, document in enumerate(documents):
        payload_documents.append(
            {
                "document_uid": document["document_uid"],
                "document_state": "current",
                "is_current": True,
                "is_primary": index == 0,
                "source_name": document["source_name"],
                "source_type": document["source_type"],
                "source_file_id": document["source_file_id"],
                "source_path": document["source_path"],
                "match_level": "document",
                "match_key_type": document["match_key_type"],
                "match_key": document["match_key"],
                "tracking_number": document["selected_tracking_number"],
                "file_name": document["file_name"],
                "file_url": document["file_url"],
                "mime_type": document["mime_type"],
                "file_ext": document["file_ext"],
                "source_modified_at": document["source_modified_at"],
                "first_seen_at": document["first_seen_at"],
                "last_seen_at": document["last_seen_at"],
            }
        )

    return {
        "odoo_picking_id": picking.odoo_picking_id,
        "picking_name": picking.picking_name,
        "pod_state": "found",
        "pod_sync_status": "ok",
        "document_count": len(payload_documents),
        "primary_document_uid": primary_document["document_uid"],
        "documents": payload_documents,
    }


def run_phase1a(config_path):
    config = load_config(config_path)
    job_id = config.get("job_id") or f"phase1a-{utc_now()}"
    output_dir = ensure_directory(config["output_dir"])
    scope_index = build_scope_index(config["odoo_scope_path"])
    parsed_documents, source_statuses = load_documents(config)

    matched_rows = []
    unmatched_rows = []
    matched_groups = {}
    parsed_output = []

    for doc in parsed_documents:
        decision = match_document(doc, scope_index)
        selected = decision["selected"]
        selected_tracking_number = None
        if decision["match_key_type"] == "tracking_number":
            selected_tracking_number = decision["match_key"]
        normalized = {
            **doc,
            "match_status": decision["match_status"],
            "match_key_type": decision["match_key_type"],
            "match_key": decision["match_key"],
            "candidate_count": decision["candidate_count"],
            "candidate_picking_refs": decision["candidate_picking_refs"],
            "reason_code": decision["reason_code"],
            "reason_message": decision["reason_message"],
            "last_match_attempt_at": decision["last_match_attempt_at"],
            "selected_tracking_number": selected_tracking_number,
            "selected_odoo_picking_ref": selected.ref if selected else None,
            "selected_odoo_picking_id": selected.odoo_picking_id if selected else None,
            "selected_picking_name": selected.picking_name if selected else None,
        }
        parsed_output.append(normalized)
        if selected:
            group_key = (selected.picking_name, selected.odoo_picking_id)
            if group_key not in matched_groups:
                matched_groups[group_key] = {
                    "picking": selected,
                    "documents": [],
                }
            matched_groups[group_key]["documents"].append(normalized)
        else:
            row = {
                **build_report_row(job_id, doc, decision),
                "reason_code": decision["reason_code"],
                "reason_message": decision["reason_message"],
                "candidate_count": decision["candidate_count"],
                "candidate_picking_refs": ",".join(decision["candidate_picking_refs"]),
                "last_match_attempt_at": decision["last_match_attempt_at"],
            }
            unmatched_rows.append(row)

    payload_items = []
    for group in matched_groups.values():
        picking = group["picking"]
        documents = group["documents"]
        documents = sorted(documents, key=_document_sort_key, reverse=True)
        payload_items.append(build_payload_item(picking, documents))
        for index, document in enumerate(documents):
            doc = document
            row = {
                **build_report_row(job_id, doc, doc),
                "match_key_type": doc["match_key_type"],
                "match_key": doc["match_key"],
                "candidate_count": doc["candidate_count"],
                "selected_odoo_picking_ref": doc["selected_odoo_picking_ref"],
                "selected_odoo_picking_id": doc["selected_odoo_picking_id"],
                "document_count_for_picking": len(documents),
                "is_primary": index == 0,
                "push_status": "pending",
                "push_at": None,
                "push_error_code": None,
                "push_error_message": None,
            }
            matched_rows.append(row)

    matched_rows.sort(key=lambda row: (row["selected_odoo_picking_ref"] or "", row["file_name"] or ""))
    unmatched_rows.sort(key=lambda row: (row["reason_code"] or "", row["file_name"] or ""))
    payload_items.sort(key=lambda item: item["picking_name"])

    payload = {
        "job_id": job_id,
        "generated_at": utc_now(),
        "items": payload_items,
    }

    summary = {
        "job_id": job_id,
        "generated_at": payload["generated_at"],
        "output_dir": str(Path(output_dir).resolve()),
        "document_count": len(parsed_documents),
        "matched_document_count": len(matched_rows),
        "unmatched_document_count": len(unmatched_rows),
        "matched_picking_count": len(payload_items),
        "source_count": len(config.get("sources", [])),
        "odoo_scope_path": str(Path(config["odoo_scope_path"]).resolve()),
        "sync_config_path": str(Path(config["sync_config_path"]).resolve()) if config.get("sync_config_path") else None,
        "artifacts": {
            "parsed_documents_json": str((Path(output_dir) / "parsed_documents.json").resolve()),
            "matched_report_csv": str((Path(output_dir) / "pod_backfill_matched_report.csv").resolve()),
            "matched_report_json": str((Path(output_dir) / "pod_backfill_matched_report.json").resolve()),
            "unmatched_report_csv": str((Path(output_dir) / "pod_backfill_unmatched_report.csv").resolve()),
            "unmatched_report_json": str((Path(output_dir) / "pod_backfill_unmatched_report.json").resolve()),
            "payload_json": str((Path(output_dir) / "odoo_backfill_payload.json").resolve()),
            "summary_json": str((Path(output_dir) / "phase1a_summary.json").resolve()),
            "dashboard_json": str((Path(output_dir) / "phase1a_dashboard_data.json").resolve()),
            "dashboard_html": str((Path(output_dir) / "phase1a_dashboard.html").resolve()),
        },
    }

    write_json(Path(output_dir) / "parsed_documents.json", parsed_output)
    write_json(Path(output_dir) / "pod_backfill_matched_report.json", matched_rows)
    write_json(Path(output_dir) / "pod_backfill_unmatched_report.json", unmatched_rows)
    write_json(Path(output_dir) / "odoo_backfill_payload.json", payload)
    write_csv(Path(output_dir) / "pod_backfill_matched_report.csv", _ordered_rows(matched_rows, MATCHED_REPORT_FIELDS))
    write_csv(Path(output_dir) / "pod_backfill_unmatched_report.csv", _ordered_rows(unmatched_rows, UNMATCHED_REPORT_FIELDS))
    dashboard_payload = build_dashboard_payload(
        summary=summary,
        source_statuses=source_statuses,
        parsed_documents=parsed_output,
        matched_rows=matched_rows,
        unmatched_rows=unmatched_rows,
        payload=payload,
    )
    write_dashboard_artifacts(output_dir, dashboard_payload)
    write_json(Path(output_dir) / "phase1a_summary.json", summary)

    return summary


def _ordered_rows(rows, field_order):
    ordered = []
    for row in rows:
        new_row = {}
        for field in field_order:
            new_row[field] = row.get(field)
        ordered.append(new_row)
    return ordered
