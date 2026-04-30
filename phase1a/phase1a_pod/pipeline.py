import re
from collections import defaultdict
from datetime import datetime
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

DIRECT_READY_REPORT_FIELDS = [
    "job_id",
    "document_uid",
    "source_name",
    "source_type",
    "source_path",
    "file_name",
    "file_url",
    "local_path",
    "source_modified_at",
    "primary_picking_name",
    "primary_tracking_number",
    "candidate_picking_names",
    "candidate_tracking_numbers",
    "preferred_key_type",
    "preferred_key",
    "status",
    "reason_code",
    "reason_message",
]

DIRECT_REVIEW_REPORT_FIELDS = [
    "job_id",
    "document_uid",
    "source_name",
    "source_type",
    "source_path",
    "file_name",
    "file_url",
    "local_path",
    "source_modified_at",
    "primary_picking_name",
    "primary_tracking_number",
    "candidate_picking_names",
    "candidate_tracking_numbers",
    "preferred_key_type",
    "preferred_key",
    "status",
    "reason_code",
    "reason_message",
]

SYNC_SOURCE_DEFINITIONS = (
    {"source_type": "onedrive", "config_keys": ("cmc_onedrive", "onedrive"), "default_label": "CMC"},
    {"source_type": "gdrive", "config_keys": ("emtc_gdrive", "gdrive"), "default_label": "EMTC"},
    {"source_type": "westerman_api", "config_keys": ("westerman_api",), "default_label": "Westerman"},
)

RUN_NAME_PATTERN = re.compile(r"^(\d{8})(\d{4})$")


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


def _record_identity(record):
    return (
        record.get("source"),
        record.get("warehouse"),
        record.get("source_file_id") or record.get("source_path") or record.get("local_path") or record.get("file_name"),
    )


def _record_recency_key(record):
    return (
        record.get("downloaded_at") or "",
        record.get("source_modified_time") or "",
        record.get("_line_number") or 0,
    )


def _dedupe_manifest_records(records):
    latest_by_identity = {}
    for record in records:
        identity = _record_identity(record)
        current = latest_by_identity.get(identity)
        if current is None or _record_recency_key(record) >= _record_recency_key(current):
            latest_by_identity[identity] = record
    deduped = list(latest_by_identity.values())
    deduped.sort(key=lambda row: (row.get("downloaded_at") or "", row.get("source_modified_time") or "", row.get("_line_number") or 0))
    return deduped


def _next_run_name(output_root):
    local_date = datetime.now().astimezone().strftime("%Y%m%d")
    next_sequence = 1
    output_root = Path(output_root)
    if output_root.exists():
        for child in output_root.iterdir():
            if not child.is_dir():
                continue
            match = RUN_NAME_PATTERN.fullmatch(child.name)
            if not match or match.group(1) != local_date:
                continue
            next_sequence = max(next_sequence, int(match.group(2)) + 1)
    return f"{local_date}{next_sequence:04d}"


def resolve_run_context(config):
    base_output_dir = Path(config["output_dir"])
    if config.get("auto_run_dir"):
        output_root = ensure_directory(base_output_dir)
        run_name = _next_run_name(output_root)
        output_dir = ensure_directory(output_root / run_name)
        return run_name, output_dir

    output_dir = ensure_directory(base_output_dir)
    return output_dir.resolve().name, output_dir


def resolve_processing_mode(config):
    mode = config.get("processing_mode")
    if mode:
        if mode not in {"parse_only", "scope_match"}:
            raise SystemExit(f"Unsupported processing_mode: {mode}")
        return mode
    if config.get("odoo_scope_path"):
        return "scope_match"
    return "parse_only"


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
            "unique_record_count": 0,
            "duplicate_record_count": 0,
            "status": "ok" if manifest_path.exists() else "missing_manifest",
        }
        if not manifest_path.exists():
            source_statuses.append(status)
            continue

        records = read_jsonl(manifest_path)
        status["record_count"] = len(records)
        deduped_records = _dedupe_manifest_records(records)
        status["unique_record_count"] = len(deduped_records)
        status["duplicate_record_count"] = len(records) - len(deduped_records)
        for record in deduped_records:
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


def _preferred_identifier(doc):
    primary_picking_name = _resolve_primary_picking_name(doc["inferred_picking_names"])
    primary_tracking_number = doc["inferred_tracking_numbers"][0] if len(doc["inferred_tracking_numbers"]) == 1 else None

    if primary_picking_name:
        return {
            "primary_picking_name": primary_picking_name,
            "primary_tracking_number": primary_tracking_number,
            "preferred_key_type": "picking_name",
            "preferred_key": primary_picking_name,
        }
    if primary_tracking_number:
        return {
            "primary_picking_name": primary_picking_name,
            "primary_tracking_number": primary_tracking_number,
            "preferred_key_type": "tracking_number",
            "preferred_key": primary_tracking_number,
        }
    if doc["inferred_picking_names"]:
        return {
            "primary_picking_name": None,
            "primary_tracking_number": primary_tracking_number,
            "preferred_key_type": "picking_name",
            "preferred_key": doc["inferred_picking_names"][0],
        }
    if doc["inferred_tracking_numbers"]:
        return {
            "primary_picking_name": primary_picking_name,
            "primary_tracking_number": None,
            "preferred_key_type": "tracking_number",
            "preferred_key": doc["inferred_tracking_numbers"][0],
        }
    return {
        "primary_picking_name": None,
        "primary_tracking_number": None,
        "preferred_key_type": None,
        "preferred_key": None,
    }


def _picking_alias_suffix(value):
    if not value:
        return None
    if value.startswith("WCB2BOUT"):
        return value[len("WCB2BOUT"):]
    if value.startswith("WCB2COUT"):
        return value[len("WCB2COUT"):]
    if value.startswith("WBB2BRET"):
        return value[len("WBB2BRET"):]
    if value.startswith("OTHER"):
        return value[len("OTHER"):]
    if value.startswith("EU"):
        return value[len("EU"):]
    if value.startswith("HK"):
        return value[len("HK"):]
    return None


def _resolve_primary_picking_name(candidates):
    if len(candidates) == 1:
        return candidates[0]
    suffixes = [_picking_alias_suffix(candidate) for candidate in candidates]
    alias_suffixes = {suffix for suffix in suffixes if suffix is not None}
    alias_suffixes.discard(None)
    if len(alias_suffixes) == 1 and all(suffix is not None for suffix in suffixes):
        for candidate in candidates:
            if candidate.startswith("WCB2BOUT") or candidate.startswith("WCB2COUT"):
                return candidate
        return candidates[0]
    return None


def _canonical_direct_split_picking(candidate):
    if not candidate:
        return None

    # Parse-only mode can safely fan out CMC multi-order files such as
    # `EU-22246-47.pdf` into multiple payload items. The parser already emits
    # both restored WCB2B keys and shorthand aliases, so we collapse them into
    # a single canonical picking key here.
    if candidate.startswith("WBB2BRET") or candidate.startswith("OTHER"):
        return candidate
    if candidate.startswith("WCB2COUT"):
        return candidate
    suffix = _picking_alias_suffix(candidate)
    if suffix is not None:
        if candidate.startswith("HK"):
            return f"WCB2COUT{suffix}"
        return f"WCB2BOUT{suffix}"
    if candidate.startswith("WHOUT"):
        return candidate
    return None


def _resolve_direct_split_pickings(doc):
    if doc["inferred_tracking_numbers"]:
        return []
    if len(doc["inferred_picking_names"]) <= 1:
        return []

    split_pickings = []
    for candidate in doc["inferred_picking_names"]:
        canonical = _canonical_direct_split_picking(candidate)
        if canonical and canonical not in split_pickings:
            split_pickings.append(canonical)
    return split_pickings if len(split_pickings) > 1 else []


def build_direct_push_decision(doc):
    identifier_info = _preferred_identifier(doc)
    direct_split_pickings = _resolve_direct_split_pickings(doc)
    inferred_picking_names = doc["inferred_picking_names"]
    inferred_tracking_numbers = doc["inferred_tracking_numbers"]
    has_any_identifier = bool(inferred_picking_names or inferred_tracking_numbers)
    has_primary_picking = identifier_info["primary_picking_name"] is not None or bool(direct_split_pickings)
    has_primary_tracking = identifier_info["primary_tracking_number"] is not None

    reason_code = None
    reason_message = None
    if not has_any_identifier:
        reason_code = "NO_IDENTIFIER_FOUND"
        reason_message = "The POD file name/path did not yield a usable picking_name or tracking_number."
    elif len(inferred_picking_names) > 1 and not direct_split_pickings and not has_primary_picking and not has_primary_tracking:
        reason_code = "MULTIPLE_PICKING_CANDIDATES"
        reason_message = "The POD file yielded multiple picking_name candidates and no single tracking_number fallback."
    elif len(inferred_tracking_numbers) > 1 and not has_primary_tracking and not has_primary_picking:
        reason_code = "MULTIPLE_TRACKING_CANDIDATES"
        reason_message = "The POD file yielded multiple tracking_number candidates and no single picking_name fallback."

    status = "ready" if reason_code is None else "needs_review"
    return {
        "status": status,
        "reason_code": reason_code,
        "reason_message": reason_message,
        "preferred_key_type": identifier_info["preferred_key_type"],
        "preferred_key": identifier_info["preferred_key"],
        "primary_picking_name": identifier_info["primary_picking_name"],
        "primary_tracking_number": identifier_info["primary_tracking_number"],
        "direct_split_pickings": direct_split_pickings,
        "last_match_attempt_at": utc_now(),
    }


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


def build_direct_payload_item(
    document,
    *,
    primary_picking_name=None,
    primary_tracking_number=None,
    preferred_key_type=None,
    preferred_key=None,
):
    primary_picking_name = primary_picking_name if primary_picking_name is not None else document.get("primary_picking_name")
    primary_tracking_number = (
        primary_tracking_number if primary_tracking_number is not None else document.get("primary_tracking_number")
    )
    preferred_key_type = preferred_key_type if preferred_key_type is not None else document["preferred_key_type"]
    preferred_key = preferred_key if preferred_key is not None else document["preferred_key"]
    primary_label = primary_picking_name or primary_tracking_number or document["document_uid"]
    payload_document = {
        "document_uid": document["document_uid"],
        "document_state": "current",
        "is_current": True,
        "is_primary": True,
        "source_name": document["source_name"],
        "source_type": document["source_type"],
        "source_file_id": document["source_file_id"],
        "source_path": document["source_path"],
        "preferred_key_type": preferred_key_type,
        "preferred_key": preferred_key,
        "primary_picking_name": primary_picking_name,
        "primary_tracking_number": primary_tracking_number,
        "candidate_picking_names": document["inferred_picking_names"],
        "candidate_tracking_numbers": document["inferred_tracking_numbers"],
        "file_name": document["file_name"],
        "file_url": document["file_url"],
        "local_path": document["local_path"],
        "mime_type": document["mime_type"],
        "file_ext": document["file_ext"],
        "source_modified_at": document["source_modified_at"],
        "first_seen_at": document["first_seen_at"],
        "last_seen_at": document["last_seen_at"],
    }
    payload_item_id = document["document_uid"] if not preferred_key else f"{document['document_uid']}::{preferred_key}"
    return {
        "payload_item_id": payload_item_id,
        "identifier_status": "ready",
        "preferred_key_type": preferred_key_type,
        "preferred_key": preferred_key,
        "primary_picking_name": primary_picking_name,
        "primary_tracking_number": primary_tracking_number,
        "candidate_picking_names": document["inferred_picking_names"],
        "candidate_tracking_numbers": document["inferred_tracking_numbers"],
        "display_label": primary_label,
        "picking_name": primary_picking_name,
        "tracking_number": primary_tracking_number,
        "source_name": document["source_name"],
        "source_type": document["source_type"],
        "source_file_id": document["source_file_id"],
        "source_path": document["source_path"],
        "file_name": document["file_name"],
        "file_url": document["file_url"],
        "local_path": document["local_path"],
        "mime_type": document["mime_type"],
        "file_ext": document["file_ext"],
        "source_modified_at": document["source_modified_at"],
        "first_seen_at": document["first_seen_at"],
        "last_seen_at": document["last_seen_at"],
        "document_count": 1,
        "primary_document_uid": document["document_uid"],
        "documents": [payload_document],
    }


def run_phase1a(config_path):
    config = load_config(config_path)
    run_name, output_dir = resolve_run_context(config)
    job_id = config.get("job_id") or f"pod-recognition-{run_name}"
    processing_mode = resolve_processing_mode(config)
    scope_index = build_scope_index(config["odoo_scope_path"]) if processing_mode == "scope_match" else None
    parsed_documents, source_statuses = load_documents(config)

    matched_rows = []
    unmatched_rows = []
    matched_groups = {}
    parsed_output = []
    payload_items = []

    for doc in parsed_documents:
        if processing_mode == "scope_match":
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
                "primary_picking_name": selected.picking_name if selected else None,
                "primary_tracking_number": selected_tracking_number,
                "preferred_key_type": decision["match_key_type"],
                "preferred_key": decision["match_key"],
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
        else:
            decision = build_direct_push_decision(doc)
            normalized = {
                **doc,
                "match_status": decision["status"],
                "match_key_type": decision["preferred_key_type"],
                "match_key": decision["preferred_key"],
                "candidate_count": len(doc["inferred_picking_names"]) + len(doc["inferred_tracking_numbers"]),
                "candidate_picking_refs": [],
                "reason_code": decision["reason_code"],
                "reason_message": decision["reason_message"],
                "last_match_attempt_at": decision["last_match_attempt_at"],
                "selected_tracking_number": decision["primary_tracking_number"],
                "selected_odoo_picking_ref": None,
                "selected_odoo_picking_id": None,
                "selected_picking_name": decision["primary_picking_name"],
                "primary_picking_name": decision["primary_picking_name"],
                "primary_tracking_number": decision["primary_tracking_number"],
                "preferred_key_type": decision["preferred_key_type"],
                "preferred_key": decision["preferred_key"],
            }
            parsed_output.append(normalized)

            direct_split_pickings = decision["direct_split_pickings"]
            if normalized["match_status"] == "ready" and direct_split_pickings:
                for split_picking in direct_split_pickings:
                    row = {
                        "job_id": job_id,
                        "document_uid": normalized["document_uid"],
                        "source_name": normalized["source_name"],
                        "source_type": normalized["source_type"],
                        "source_path": normalized["source_path"],
                        "file_name": normalized["file_name"],
                        "file_url": normalized["file_url"],
                        "local_path": normalized["local_path"],
                        "source_modified_at": normalized["source_modified_at"],
                        "primary_picking_name": split_picking,
                        "primary_tracking_number": None,
                        "candidate_picking_names": ",".join(normalized["inferred_picking_names"]),
                        "candidate_tracking_numbers": ",".join(normalized["inferred_tracking_numbers"]),
                        "preferred_key_type": "picking_name",
                        "preferred_key": split_picking,
                        "status": normalized["match_status"],
                        "reason_code": None,
                        "reason_message": None,
                    }
                    matched_rows.append(row)
                    payload_items.append(
                        build_direct_payload_item(
                            normalized,
                            primary_picking_name=split_picking,
                            primary_tracking_number=None,
                            preferred_key_type="picking_name",
                            preferred_key=split_picking,
                        )
                    )
            else:
                row = {
                    "job_id": job_id,
                    "document_uid": normalized["document_uid"],
                    "source_name": normalized["source_name"],
                    "source_type": normalized["source_type"],
                    "source_path": normalized["source_path"],
                    "file_name": normalized["file_name"],
                    "file_url": normalized["file_url"],
                    "local_path": normalized["local_path"],
                    "source_modified_at": normalized["source_modified_at"],
                    "primary_picking_name": normalized["primary_picking_name"],
                    "primary_tracking_number": normalized["primary_tracking_number"],
                    "candidate_picking_names": ",".join(normalized["inferred_picking_names"]),
                    "candidate_tracking_numbers": ",".join(normalized["inferred_tracking_numbers"]),
                    "preferred_key_type": normalized["preferred_key_type"],
                    "preferred_key": normalized["preferred_key"],
                    "status": normalized["match_status"],
                    "reason_code": normalized["reason_code"],
                    "reason_message": normalized["reason_message"],
                }
                if normalized["match_status"] == "ready":
                    matched_rows.append(row)
                    payload_items.append(build_direct_payload_item(normalized))
                else:
                    unmatched_rows.append(row)

    if processing_mode == "scope_match":
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
    else:
        payload_items.sort(key=lambda item: (item["preferred_key"] or "", item["file_name"] or ""))
        matched_rows.sort(key=lambda row: (row["preferred_key"] or "", row["file_name"] or ""))
        unmatched_rows.sort(key=lambda row: (row["reason_code"] or "", row["file_name"] or ""))

    payload = {
        "job_id": job_id,
        "generated_at": utc_now(),
        "processing_mode": processing_mode,
        "items": payload_items,
    }

    summary = {
        "job_id": job_id,
        "run_name": run_name,
        "generated_at": payload["generated_at"],
        "output_dir": str(Path(output_dir).resolve()),
        "document_count": len(parsed_documents),
        "processing_mode": processing_mode,
        "ready_document_count": len(matched_rows),
        "review_document_count": len(unmatched_rows),
        "payload_item_count": len(payload_items),
        "matched_document_count": len(matched_rows),
        "unmatched_document_count": len(unmatched_rows),
        "matched_picking_count": len(payload_items),
        "source_count": len(config.get("sources", [])),
        "odoo_scope_path": str(Path(config["odoo_scope_path"]).resolve()) if config.get("odoo_scope_path") else None,
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
    if processing_mode == "scope_match":
        matched_field_order = MATCHED_REPORT_FIELDS
        unmatched_field_order = UNMATCHED_REPORT_FIELDS
    else:
        matched_field_order = DIRECT_READY_REPORT_FIELDS
        unmatched_field_order = DIRECT_REVIEW_REPORT_FIELDS
    write_csv(Path(output_dir) / "pod_backfill_matched_report.csv", _ordered_rows(matched_rows, matched_field_order))
    write_csv(Path(output_dir) / "pod_backfill_unmatched_report.csv", _ordered_rows(unmatched_rows, unmatched_field_order))
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
